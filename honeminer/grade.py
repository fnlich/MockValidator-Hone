"""Grade a patch or script exactly as a validator would, with rlvr's own code.

A thin wrapper over ``rlvr.v3.grading.evaluate_repository`` /
``evaluate_terminal`` (the path ``scripts/try_task.py`` uses). Exit codes for
the CLI: 0 passed, 1 failed or rejected, 2 could not grade (environment).
"""

from __future__ import annotations

import dataclasses
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rlvr.policy import RELEASE_POLICY
from rlvr.v3.grading import EvaluationResult, evaluate_repository, evaluate_terminal
from rlvr.v3.manifest import load_manifest
from rlvr.v3.release import round_policy

from honeminer.tasks import TaskPack


class GradeEnvironmentError(RuntimeError):
    """Grading could not run here (root user, no Docker, missing image)."""


@dataclass(frozen=True)
class Grade:
    status: str  # passed | failed | rejected | abandoned
    reason: str
    reason_code: str | None
    checks: tuple[tuple[str, str], ...]
    failed_check: str | None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def exit_code(self) -> int:
        if self.status == "passed":
            return 0
        return 2 if self.status == "abandoned" else 1

    def render(self) -> str:
        lines = [f"status: {self.status}" + (f" ({self.reason_code})" if self.reason_code else "")]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        lines.extend(f"  {check_id}: {outcome}" for check_id, outcome in self.checks)
        if self.failed_check:
            lines.append("failed check:")
            lines.extend(f"  {line}" for line in self.failed_check.splitlines())
        return "\n".join(lines)


def _grade(result: EvaluationResult) -> Grade:
    code = getattr(result.reason_code, "value", result.reason_code)
    return Grade(
        status=result.status,
        reason=result.reason,
        reason_code=code,
        checks=tuple((check.check_id, check.outcome) for check in result.checks),
        failed_check=result.failed_check,
    )


def grading_policy(image: str = ""):
    """The validator's round policy, optionally with another digest-pinned image."""

    try:
        policy = round_policy(RELEASE_POLICY, dispatch_concurrency=1)
    except ValueError as exc:
        raise GradeEnvironmentError(str(exc)) from None
    if image:
        policy = dataclasses.replace(policy, supervisor=dataclasses.replace(policy.supervisor, image=image))
    return policy


def grade_submission(pack: TaskPack, submission: bytes, *, image: str = "") -> Grade:
    policy = grading_policy(image)
    limit = RELEASE_POLICY.v3_script_bytes if pack.is_terminal else RELEASE_POLICY.v3_patch_bytes
    if len(submission) > limit:
        return Grade("rejected", f"submission exceeds the {limit} byte limit", None, (), None)
    with tempfile.TemporaryDirectory(prefix="honeminer-grade-") as temporary:
        root = Path(temporary)
        scratch = root / "scratch"
        scratch.mkdir()
        environment = pack.extract(pack.environment_role, root / "work", scratch)
        verifier = pack.extract("verifier", root / "verifier", scratch)
        manifest = load_manifest(verifier, task_type=pack.identity.task_type)
        if pack.is_terminal:
            result = evaluate_terminal(
                environment, submission, pack.identity, manifest, verifier,
                policy.supervisor, policy.tree, policy.script, policy.docker_binary, "honeminer",
            )
        else:
            result = evaluate_repository(
                environment, submission, pack.identity, manifest, verifier,
                policy.supervisor, policy.tree, policy.patch, policy.docker_binary, "honeminer",
            )
    return _grade(result)
