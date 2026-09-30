"""Build local task packs, in Hone's exact format, from small recipes.

A recipe directory holds:

    recipe.json   name, task_type, task_kind, language, instruction,
                  working_directory / result_tree_path, and the checks
    repo/         clean source (repository tasks); ``break.diff`` injects the
                  defect or removes the feature, and its reverse is the reference
    environment/  the terminal environment (terminal tasks), with
                  ``reference.sh`` as the known-good script
    checks/       one Python file per check, inlined as ``python3 -c`` like
                  Hone's own checks; ``inputs/`` holds optional stdin files

Gold outputs are produced by running every check in the grading image on the
reference answer, with the same container request the grader makes. A pack is
then validated with rlvr's own grader: the reference must pass and an empty
answer must fail, and the gold must be identical across two builds.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import zstandard
from rlvr.policy import RELEASE_POLICY
from rlvr.v3.identity import RepositoryTaskIdentity, TerminalScriptTaskIdentity, compute_task_id

from honeminer.patch import build_patch

RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"
AUTHORING_VERSION = "honeminer-pack-v1"
VERIFIER_POLICY = "command-gold-digest-v1"
CHECK_OUTPUT_BYTES = 65_536


class RecipeError(ValueError):
    pass


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    source: str
    timeout_s: int = 90
    cwd: str = "."
    stdin_file: str | None = None

    def argv(self) -> tuple[str, ...]:
        return ("/usr/bin/python3", "-c", self.source)


@dataclass(frozen=True)
class Recipe:
    root: Path
    name: str
    task_type: str
    task_kind: str | None
    language: str
    instruction: str
    working_directory: str
    result_tree_path: str
    checks: tuple[CheckSpec, ...]

    @property
    def is_terminal(self) -> bool:
        return self.task_type == "terminal_script_v1"


def load_recipe(path: str | Path) -> Recipe:
    root = Path(path)
    try:
        raw = json.loads((root / "recipe.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecipeError(f"{root.name}: cannot read recipe.json ({exc})") from None
    task_type = raw.get("task_type", "repository_patch_v1")
    checks = []
    for item in raw.get("checks", []):
        source_path = root / "checks" / f"{item['id']}.py"
        if not source_path.is_file():
            raise RecipeError(f"{root.name}: missing checks/{item['id']}.py")
        stdin_file = item.get("stdin")
        if stdin_file and not (root / "inputs" / stdin_file).is_file():
            raise RecipeError(f"{root.name}: missing inputs/{stdin_file}")
        checks.append(CheckSpec(item["id"], source_path.read_text(encoding="utf-8"),
                                int(item.get("timeout_s", 90)), item.get("cwd", "."), stdin_file))
    if not checks:
        raise RecipeError(f"{root.name}: a recipe needs at least one check")
    recipe = Recipe(
        root=root, name=raw.get("name", root.name), task_type=task_type,
        task_kind=raw.get("task_kind", "bug_fix") if task_type == "repository_patch_v1" else None,
        language=raw.get("language", "bash"), instruction=raw["instruction"],
        working_directory=raw.get("working_directory", "."),
        result_tree_path=raw.get("result_tree_path", "."), checks=tuple(checks),
    )
    needed = ("environment", "reference.sh") if recipe.is_terminal else ("repo", "break.diff")
    for name in needed:
        if not (root / name).exists():
            raise RecipeError(f"{root.name}: missing {name}")
    return recipe


def write_archive(source: Path, destination: Path) -> dict:
    """Deterministic tar.zst of ``source``: sorted, regular files and directories only."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(source.rglob("*"), key=lambda p: p.relative_to(source).as_posix()):
            name = path.relative_to(source).as_posix()
            if path.is_symlink():
                raise RecipeError(f"{name}: symlinks are not allowed in task archives")
            info = tarfile.TarInfo(name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            if path.is_dir():
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
                archive.addfile(info, io.BytesIO(data))
    payload = buffer.getvalue()
    compressed = zstandard.ZstdCompressor(level=19, write_content_size=True).compress(payload)
    destination.write_bytes(compressed)
    return {
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "compressed_size_bytes": len(compressed),
        "expanded_size_bytes": len(payload),
        "artifact_format": "tar_zst_v1",
    }


@dataclass(frozen=True)
class RunOutput:
    exit_code: int
    stdout: bytes


# (root to mount at /work, argv, cwd inside the container, stdin, timeout) -> output
Runner = Callable[[Path, tuple[str, ...], str, bytes, int, Path | None], RunOutput]


def docker_runner(image: str = "") -> Runner:
    """Run exactly as rlvr grading does (untrusted request, /work mount)."""

    from rlvr.v3.supervisor import ContainerRequest, Mount, run_container

    from honeminer.grade import grading_policy

    policy = grading_policy(image)
    counter = iter(range(1_000_000))

    def run(root: Path, argv: tuple[str, ...], cwd: str, stdin: bytes, timeout_s: int,
            submission: Path | None = None) -> RunOutput:
        mounts = [Mount(root, "/work", False)]
        if submission is not None:
            mounts.append(Mount(submission, "/submission", True))
        request = ContainerRequest(
            name=f"honeminer-pack-{next(counter)}", argv=argv, cwd=cwd, mounts=tuple(mounts), stdin=stdin,
            timeout_s=timeout_s, max_stdout_bytes=CHECK_OUTPUT_BYTES, max_stderr_bytes=CHECK_OUTPUT_BYTES,
            trusted=False,
        )
        result = run_container(request, policy.supervisor, policy.docker_binary)
        if result.oom_killed or result.timed_out or result.stdout_overflow:
            raise RecipeError(f"check {argv[:2]} did not finish cleanly while producing gold")
        return RunOutput(result.exit_code, result.stdout)

    return run


def _work_cwd(relative: str) -> str:
    return "/work" if relative in ("", ".") else f"/work/{relative}"


def _gold_outputs(recipe: Recipe, answer_tree: Path, runner: Runner) -> dict[str, RunOutput]:
    outputs = {}
    # rlvr runs terminal checks from /work (not the result tree), repository
    # checks from /work/<working_directory>; each check's cwd is relative to that.
    base = "." if recipe.is_terminal else recipe.working_directory
    for check in recipe.checks:
        stdin = (recipe.root / "inputs" / check.stdin_file).read_bytes() if check.stdin_file else b""
        cwd = _work_cwd(posixpath.normpath(posixpath.join(base, check.cwd)))
        with tempfile.TemporaryDirectory(prefix="honeminer-gold-") as scratch:
            work = Path(scratch) / "work"
            shutil.copytree(answer_tree, work, symlinks=True)
            outputs[check.check_id] = runner(work, check.argv(), cwd, stdin, check.timeout_s, None)
    return outputs


def _answer_tree(recipe: Recipe, workspace: Path, scratch: Path, runner: Runner) -> Path:
    """The tree the reference answer produces, which the checks run against."""

    if not recipe.is_terminal:
        return recipe.root / "repo"
    answer = scratch / "answer"
    shutil.copytree(workspace, answer, symlinks=True)
    submission = scratch / "submission"
    submission.mkdir()
    shutil.copy2(recipe.root / "reference.sh", submission / "script.sh")
    argv = ("/usr/bin/bash", "--noprofile", "--norc", "/submission/script.sh")
    result = runner(answer, argv, _work_cwd(recipe.result_tree_path), b"", 300, submission)
    if result.exit_code != 0:
        raise RecipeError(f"{recipe.name}: reference.sh exited {result.exit_code}")
    return answer


def build_pack(recipe: Recipe, output: Path, runner: Runner) -> Path:
    """Write a complete pack for ``recipe`` into ``output`` (which must not exist)."""

    if output.exists():
        raise RecipeError(f"{output} already exists")
    with tempfile.TemporaryDirectory(prefix="honeminer-pack-") as temporary:
        scratch = Path(temporary)
        workspace = scratch / "workspace"
        if recipe.is_terminal:
            shutil.copytree(recipe.root / "environment", workspace)
        else:
            shutil.copytree(recipe.root / "repo", workspace)
            broken = subprocess.run(["git", "apply", "-p1", str((recipe.root / "break.diff").resolve())],
                                    cwd=workspace, capture_output=True)
            if broken.returncode != 0:
                raise RecipeError(f"{recipe.name}: break.diff does not apply: {broken.stderr.decode()}")

        answer = _answer_tree(recipe, workspace, scratch, runner)
        gold = _gold_outputs(recipe, answer, runner)

        verifier = scratch / "verifier"
        for sub in ("checks", "gold"):
            (verifier / sub).mkdir(parents=True)
        checks = []
        for check in recipe.checks:
            (verifier / "checks" / f"{check.check_id}.py").write_text(check.source)
            (verifier / "gold" / f"{check.check_id}.txt").write_bytes(gold[check.check_id].stdout)
            stdin_ref = None
            if check.stdin_file:
                (verifier / "inputs").mkdir(exist_ok=True)
                name = check.stdin_file
                shutil.copy2(recipe.root / "inputs" / name, verifier / "inputs" / name)
                stdin_ref = f"inputs/{check.stdin_file}"
            checks.append({
                "check_id": check.check_id, "kind": "invocation", "argv": list(check.argv()),
                "timeout_s": check.timeout_s, "max_stdout_bytes": CHECK_OUTPUT_BYTES,
                "max_stderr_bytes": CHECK_OUTPUT_BYTES,
                "expect": {"exit_code": gold[check.check_id].exit_code,
                           "stdout": f"gold/{check.check_id}.txt", "stderr": None},
                "cwd": check.cwd, "stdin": stdin_ref,
            })
        manifest = {"manifest_version": 1, "task_type": recipe.task_type, "setup": None, "checks": checks}
        (verifier / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

        output.mkdir(parents=True)
        role = "terminal_environment" if recipe.is_terminal else "workspace"
        refs = {
            role: {"artifact_role": role, **write_archive(workspace, output / f"{role}.tar.zst")},
            "verifier": {"artifact_role": "verifier", **write_archive(verifier, output / "verifier.tar.zst")},
        }
        common = dict(
            instruction=recipe.instruction, verifier_sha256=refs["verifier"]["sha256"],
            execution_profile_id=RELEASE_POLICY.v3_execution_profile_id,
            verifier_policy=VERIFIER_POLICY, authoring_version=AUTHORING_VERSION,
        )
        if recipe.is_terminal:
            identity = TerminalScriptTaskIdentity(
                environment_sha256=refs[role]["sha256"], result_tree_path=recipe.result_tree_path, **common)
            shutil.copy2(recipe.root / "reference.sh", output / "reference.sh")
        else:
            identity = RepositoryTaskIdentity(
                task_kind=recipe.task_kind, primary_language=recipe.language,
                workspace_sha256=refs[role]["sha256"], working_directory=recipe.working_directory, **common)
            reference = build_patch(workspace, recipe.root / "repo")
            if not reference.ok or not reference.diff:
                raise RecipeError(f"{recipe.name}: reference diff is invalid: {reference.rejection}")
            (output / "reference.diff").write_bytes(reference.diff)
        (output / "identity.json").write_text(json.dumps(identity.model_dump(mode="json"), indent=2) + "\n")
        (output / "artifact_refs.json").write_text(json.dumps(refs, indent=2) + "\n")
        (output / "task_id.txt").write_text(compute_task_id(identity) + "\n")
    return output


def gold_fingerprint(pack_dir: Path) -> str:
    """Hash of the verifier archive: equal across builds iff the gold is deterministic."""

    return json.loads((pack_dir / "artifact_refs.json").read_text())["verifier"]["sha256"]


@dataclass(frozen=True)
class Validation:
    reference: str
    empty: str
    deterministic: bool

    @property
    def ok(self) -> bool:
        return self.reference == "passed" and self.empty in ("failed", "rejected") and self.deterministic


def build_and_validate(recipe: Recipe, output: Path, image: str = "") -> Validation:
    """Build with Docker, then prove the pack: reference passes, empty fails, gold is stable."""

    from honeminer.grade import grade_submission
    from honeminer.tasks import load_pack

    runner = docker_runner(image)
    build_pack(recipe, output, runner)
    with tempfile.TemporaryDirectory(prefix="honeminer-rebuild-") as again:
        rebuilt = build_pack(recipe, Path(again) / "pack", runner)
        deterministic = gold_fingerprint(rebuilt) == gold_fingerprint(output)
    pack = load_pack(output)
    reference = grade_submission(pack, pack.reference() or b"", image=image)
    empty = grade_submission(pack, b"", image=image)
    return Validation(reference.status, empty.status, deterministic)
