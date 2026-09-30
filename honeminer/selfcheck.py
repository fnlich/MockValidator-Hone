"""The gate: re-check Claude's work on clean copies before it may stop.

Repository tasks:
  G0/G1  build the patch (build-cache aware) and apply rlvr's static rules
  G2     apply it to a fresh copy of the original, in the grading image
  G3     run the task's build on the patched copy
  G4     every /task/checks/NN-*.sh must exit 0 on the patched copy and fail on
         the original (bug fix: the original must build and the check exit 1;
         feature: any non-zero exit). A check that passes on both proves nothing.
  G5     existing tests that passed on the original must still pass
Terminal tasks: validate the script, run it on a fresh copy exactly as the
grader does, then the same before/after rule for the checks.

A Docker or tool failure is an ``env_error`` verdict, never "your patch is wrong".
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rlvr.policy import RELEASE_POLICY
from rlvr.v3.tree import TreeLimits

from honeminer.answer import Rank
from honeminer.facts import SANDBOX_ENV, Facts
from honeminer.patch import PatchBuildError, build_patch

CHECK_NAME = re.compile(r"^\d\d-[A-Za-z0-9._-]+\.sh$")
OUTPUT_LINES = 40
CHECK_TIMEOUT_S = 300
CHECK_OUTPUT_BYTES = 262_144
# The release round policy's tree limits (rlvr.v3.release.round_policy), for the result-tree check.
RESULT_TREE_LIMITS = TreeLimits(max_entries=200_000, max_file_bytes=RELEASE_POLICY.v3_max_file_bytes,
                                max_total_file_bytes=RELEASE_POLICY.v3_workspace_bytes, max_path_bytes=4_096)


class EnvironmentFailure(RuntimeError):
    """The gate itself could not run (Docker, git, image)."""


@dataclass(frozen=True)
class Exec:
    exit_code: int
    output: str
    timed_out: bool = False
    oom_killed: bool = False
    overflow: bool = False  # stdout or stderr went over the limit (the grader fails a script for it)


class GateRunner(Protocol):
    def apply(self, root: Path, diff: bytes) -> tuple[bool, str]: ...

    def run(self, root: Path, argv: tuple[str, ...], cwd: str, *, checks: Path | None = None,
            submission: Path | None = None, timeout_s: int = CHECK_TIMEOUT_S,
            max_output_bytes: int = CHECK_OUTPUT_BYTES) -> Exec: ...


@dataclass(frozen=True)
class GateContext:
    baseline: Path
    work: Path
    checks: Path
    facts: Facts
    submission: Path | None = None  # terminal tasks: host dir holding script.sh
    build_owned: frozenset[str] = frozenset()
    baseline_tests_passed: bool = False
    time_left: Callable[[], float] | None = None  # seconds this check may still use (from the clock)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    rank: Rank
    stage: str
    command: str = ""
    output: str = ""
    hint: str = ""
    content: bytes = b""
    dropped: tuple[tuple[str, str], ...] = ()
    env_error: bool = False
    check_results: tuple[tuple[str, str], ...] = field(default=())

    def message(self, minutes_left: int | None = None) -> str:
        if self.passed:
            return ""
        lines = [f"Not finished: {self.stage} failed on a clean copy."]
        if self.command:
            lines.append(f"$ {self.command}")
        if self.output:
            lines.append("\n".join(self.output.rstrip().splitlines()[-OUTPUT_LINES:]))
        if self.hint:
            lines.append(self.hint)
        if self.dropped:
            dropped = "; ".join(f"{path} ({reason})" for path, reason in self.dropped[:10])
            lines.append(f"Left out of your diff: {dropped}.")
        if minutes_left is not None:
            lines.append(f"About {minutes_left} minutes left.")
        return "\n".join(lines)


def _tail(text: str) -> str:
    return "\n".join(text.rstrip().splitlines()[-OUTPUT_LINES:])


def _shell(command: str) -> tuple[str, ...]:
    exports = " ".join(f"{key}={value}" for key, value in SANDBOX_ENV.items())
    return ("/usr/bin/bash", "--noprofile", "--norc", "-c", f"mkdir -p /tmp/home; export {exports}; {command}")


def _work_cwd(relative: str) -> str:
    return "/work" if relative in ("", ".") else f"/work/{relative}"


def list_checks(checks: Path) -> list[str]:
    if not checks.is_dir():
        return []
    return sorted(p.name for p in checks.iterdir() if p.is_file() and CHECK_NAME.match(p.name))


def _copy(source: Path, scratch: Path, name: str) -> Path:
    target = scratch / name
    shutil.copytree(source, target, symlinks=True)
    return target


def _timeout(ctx: GateContext, own_s: int = CHECK_TIMEOUT_S) -> int:
    """A container's timeout: its own limit, but never past what the clock leaves this check."""

    if ctx.time_left is None:
        return own_s
    left = ctx.time_left()
    if left < 1:
        raise EnvironmentFailure("out of time for this check")
    return int(max(1, min(own_s, left)))


def _run_checks(runner: GateRunner, root: Path, ctx: GateContext, names: list[str], cwd: str) -> dict[str, Exec]:
    return {
        name: runner.run(root, ("/usr/bin/bash", f"/task/checks/{name}"), cwd, checks=ctx.checks,
                         timeout_s=_timeout(ctx))
        for name in names
    }


def evaluate(ctx: GateContext, runner: GateRunner) -> Verdict:
    try:
        if ctx.facts.is_terminal:
            return _evaluate_terminal(ctx, runner)
        return _evaluate_repository(ctx, runner)
    except (EnvironmentFailure, PatchBuildError) as exc:
        return Verdict(False, Rank.EMPTY, "environment", output=str(exc), env_error=True,
                       hint="This is a problem with the checking environment, not with your work.")


def _evaluate_repository(ctx: GateContext, runner: GateRunner) -> Verdict:
    facts = ctx.facts
    patch = build_patch(ctx.baseline, ctx.work, protected=facts.protected, build_owned=ctx.build_owned)
    if not patch.diff:
        return Verdict(False, Rank.EMPTY, "patch", hint="There are no source changes yet.", dropped=patch.dropped)
    if not patch.ok:
        return Verdict(False, Rank.EMPTY, "patch", output=patch.rejection or "", content=patch.diff,
                       hint="The grader would reject this diff before running anything.", dropped=patch.dropped)
    names = list_checks(ctx.checks)
    base_cwd = _work_cwd(facts.working_directory)
    with tempfile.TemporaryDirectory(prefix="honeminer-gate-", dir=ctx.work.parent) as scratch_name:
        scratch = Path(scratch_name)
        patched = _copy(ctx.baseline, scratch, "patched")
        applied, error = runner.apply(patched, patch.diff)
        if not applied:
            return Verdict(False, Rank.STATIC_OK, "apply", "git apply --check -p1", _tail(error), content=patch.diff,
                           hint="Your diff does not apply to the original files.", dropped=patch.dropped)
        rank = Rank.APPLIES
        if facts.build_cmd:
            built = runner.run(patched, _shell(facts.build_cmd), base_cwd, timeout_s=_timeout(ctx))
            if built.exit_code != 0:
                return Verdict(False, rank, "build", facts.build_cmd, _tail(built.output), content=patch.diff,
                               dropped=patch.dropped)
            rank = Rank.BUILDS
        if not names:
            return Verdict(False, rank, "checks", content=patch.diff, dropped=patch.dropped,
                           hint="Add at least one check in /task/checks/NN-name.sh that fails on the original code.")
        after = _run_checks(runner, patched, ctx, names, base_cwd)
        for name, result in after.items():
            if result.exit_code != 0:
                verdict = "fails" if result.exit_code == 1 else f"is invalid (exit {result.exit_code})"
                return Verdict(False, rank, "checks", f"bash /task/checks/{name}", _tail(result.output),
                               hint=f"{name} {verdict} on your version.", content=patch.diff, dropped=patch.dropped)

        original = _copy(ctx.baseline, scratch, "original")
        if facts.build_cmd:
            runner.run(original, _shell(facts.build_cmd), base_cwd, timeout_s=_timeout(ctx))
        before = _run_checks(runner, original, ctx, names, base_cwd)
        results = []
        for name in names:
            code = before[name].exit_code
            if code == 0:
                return Verdict(False, rank, "checks", f"bash /task/checks/{name}", content=patch.diff,
                               hint=f"{name} also passes on the original code, so it tests nothing. "
                                    "Make it fail on the original code, or delete it.", dropped=patch.dropped)
            if facts.task_kind != "feature" and code != 1:
                return Verdict(False, rank, "checks", f"bash /task/checks/{name}", _tail(before[name].output),
                               hint=f"{name} exits {code} on the original code; it must build there and exit 1 "
                                    "(assertion failed).", content=patch.diff, dropped=patch.dropped)
            results.append((name, "fail->pass"))

        if ctx.baseline_tests_passed and facts.test_cmd:
            tests = runner.run(patched, _shell(facts.test_cmd), base_cwd, timeout_s=_timeout(ctx))
            if tests.exit_code != 0:
                return Verdict(False, rank, "existing tests", facts.test_cmd, _tail(tests.output), content=patch.diff,
                               hint="These tests passed on the original code.", dropped=patch.dropped)
    return Verdict(True, Rank.CHECKED, "passed", content=patch.diff, dropped=patch.dropped,
                   check_results=tuple(results))


def _evaluate_terminal(ctx: GateContext, runner: GateRunner) -> Verdict:
    from rlvr.policy import RELEASE_POLICY
    from rlvr.v3.grading import SCRIPT_OUTPUT_BYTES, SCRIPT_TIMEOUT_S
    from rlvr.v3.script import ScriptLimits, validate_script
    from rlvr.v3.tree import TreeError, inspect_tree

    script_path = (ctx.submission or Path("/nonexistent")) / "script.sh"
    if not script_path.is_file():
        return Verdict(False, Rank.EMPTY, "script", hint="Write your solution to /submission/script.sh.")
    script = script_path.read_bytes()
    checked = validate_script(script, ScriptLimits(max_script_bytes=RELEASE_POLICY.v3_script_bytes))
    if checked.status == "rejected":
        return Verdict(False, Rank.EMPTY, "script", output=checked.reason, content=script)
    names = list_checks(ctx.checks)
    tree_cwd = _work_cwd(ctx.facts.result_tree_path)
    with tempfile.TemporaryDirectory(prefix="honeminer-gate-", dir=ctx.work.parent) as scratch_name:
        scratch = Path(scratch_name)
        scripted = _copy(ctx.baseline, scratch, "scripted")
        # Run exactly as the grader does: its timeout and its 1 MiB output limit (overflow fails the script).
        ran = runner.run(scripted, ("/usr/bin/bash", "--noprofile", "--norc", "/submission/script.sh"), tree_cwd,
                         submission=ctx.submission, timeout_s=_timeout(ctx, SCRIPT_TIMEOUT_S),
                         max_output_bytes=SCRIPT_OUTPUT_BYTES)
        if ran.timed_out or ran.oom_killed or ran.overflow:
            limit = "output limit (1 MiB of stdout or stderr)" if ran.overflow else "time or memory limit"
            return Verdict(False, Rank.STATIC_OK, "script", "bash --noprofile --norc /submission/script.sh",
                           _tail(ran.output), hint=f"The script hit the {limit}.", content=script)
        # The grader refuses a result tree with symlinks, hard links or .git before any check runs.
        tree = scripted if ctx.facts.result_tree_path in ("", ".") else scripted / ctx.facts.result_tree_path
        try:
            inspect_tree(tree, limits=RESULT_TREE_LIMITS, normalize_modes=False)
        except TreeError as exc:
            return Verdict(False, Rank.STATIC_OK, "result tree", output=str(exc), content=script,
                           hint="After your script, the result tree must hold only regular files and directories "
                                "(no symlinks, hard links or .git).")
        rank = Rank.BUILDS
        if not names:
            return Verdict(False, rank, "checks", content=script,
                           hint="Add at least one check in /task/checks/NN-name.sh that fails before the script runs.")
        after = _run_checks(runner, scripted, ctx, names, "/work")
        for name, result in after.items():
            if result.exit_code != 0:
                return Verdict(False, rank, "checks", f"bash /task/checks/{name}", _tail(result.output),
                               hint=f"{name} fails after your script ran (exit {result.exit_code}).", content=script)
        untouched = _copy(ctx.baseline, scratch, "untouched")
        before = _run_checks(runner, untouched, ctx, names, "/work")
        for name in names:
            if before[name].exit_code == 0:
                return Verdict(False, rank, "checks", f"bash /task/checks/{name}", content=script,
                               hint=f"{name} also passes without your script, so it tests nothing.")
    return Verdict(True, Rank.CHECKED, "passed", content=script,
                   check_results=tuple((name, "fail->pass") for name in names))


class DockerGateRunner:
    """Runs gate steps in fresh, grading-identical containers via rlvr's supervisor."""

    def __init__(self, image: str = "") -> None:
        from honeminer.grade import GradeEnvironmentError, grading_policy

        try:
            self.policy = grading_policy(image)
        except GradeEnvironmentError as exc:
            raise EnvironmentFailure(str(exc)) from None
        self._counter = 0

    def _name(self, what: str) -> str:
        self._counter += 1
        return f"honeminer-gate-{what}-{self._counter}"

    def apply(self, root: Path, diff: bytes) -> tuple[bool, str]:
        from rlvr.v3.patch import PatchToolError, apply_patch_in_container
        from rlvr.v3.supervisor import SupervisorError

        try:
            result = apply_patch_in_container(root, diff, self.policy.patch, supervisor_policy=self.policy.supervisor,
                                              docker_binary=self.policy.docker_binary, run_prefix=self._name("patch"))
        except (PatchToolError, SupervisorError, OSError) as exc:
            raise EnvironmentFailure(f"could not apply the patch in the sandbox: {exc}") from None
        return result.status == "applied", result.reason

    def run(self, root: Path, argv: tuple[str, ...], cwd: str, *, checks: Path | None = None,
            submission: Path | None = None, timeout_s: int = CHECK_TIMEOUT_S,
            max_output_bytes: int = CHECK_OUTPUT_BYTES) -> Exec:
        from rlvr.v3.supervisor import ContainerRequest, Mount, SupervisorError, run_container

        mounts = [Mount(root, "/work", False)]
        if checks is not None:
            mounts.append(Mount(checks, "/task/checks", True))
        if submission is not None:
            mounts.append(Mount(submission, "/submission", True))
        request = ContainerRequest(name=self._name("run"), argv=argv, cwd=cwd, mounts=tuple(mounts), stdin=b"",
                                   timeout_s=timeout_s, max_stdout_bytes=max_output_bytes,
                                   max_stderr_bytes=max_output_bytes,
                                   trusted=False)
        try:
            result = run_container(request, self.policy.supervisor, self.policy.docker_binary)
        except (SupervisorError, OSError, ValueError) as exc:
            raise EnvironmentFailure(f"sandbox container failed: {exc}") from None
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        return Exec(result.exit_code, output, result.timed_out, result.oom_killed,
                    result.stdout_overflow or result.stderr_overflow)
