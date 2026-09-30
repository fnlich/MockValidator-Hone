"""Task packs: a V4 task on disk, in the same layout as hone-subnet's fixture.

A pack directory holds ``identity.json``, ``artifact_refs.json``, the archives
named by the refs (``workspace.tar.zst`` or ``terminal_environment.tar.zst``,
plus ``verifier.tar.zst``), optionally ``task_id.txt`` and a reference answer
(``reference.diff`` or ``reference.sh``). Everything is checked against the
identity's digests before use, with rlvr's own models and extractor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rlvr.policy import RELEASE_POLICY
from rlvr.v3.archive import ArchiveLimits, extract_archive
from rlvr.v3.artifacts import ArtifactRef
from rlvr.v3.identity import RepositoryTaskIdentity, TerminalScriptTaskIdentity, compute_task_id

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"

# The validator's limits (rlvr.v3.release.round_policy), built without its
# service-user and Docker checks, which only matter when grading.
WORKSPACE_LIMITS = ArchiveLimits(
    max_compressed_bytes=RELEASE_POLICY.v3_workspace_compressed_bytes,
    max_expanded_bytes=RELEASE_POLICY.v3_workspace_bytes,
    max_file_bytes=RELEASE_POLICY.v3_max_file_bytes,
    max_entries=200_000,
    max_path_bytes=4_096,
    max_zstd_window_bytes=128 * 1024**2,
)
VERIFIER_LIMITS = ArchiveLimits(
    max_compressed_bytes=RELEASE_POLICY.v3_verifier_compressed_bytes,
    max_expanded_bytes=RELEASE_POLICY.v3_verifier_expanded_bytes,
    max_file_bytes=512 * 1024**2,
    max_entries=20_000,
    max_path_bytes=4_096,
    max_zstd_window_bytes=128 * 1024**2,
)


class PackError(ValueError):
    """A task pack is missing a file or does not match its identity."""


@dataclass(frozen=True)
class TaskPack:
    root: Path
    identity: RepositoryTaskIdentity | TerminalScriptTaskIdentity
    refs: dict[str, ArtifactRef]
    task_id: str

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def is_terminal(self) -> bool:
        return isinstance(self.identity, TerminalScriptTaskIdentity)

    @property
    def environment_role(self) -> str:
        """The archive Claude works in: the repository or the terminal environment."""

        return "terminal_environment" if self.is_terminal else "workspace"

    @property
    def language(self) -> str:
        return getattr(self.identity, "primary_language", "bash")

    def reference(self) -> bytes | None:
        """The pack's known-good answer, when it ships one."""

        path = self.root / ("reference.sh" if self.is_terminal else "reference.diff")
        return path.read_bytes() if path.is_file() else None

    def extract(self, role: str, destination: Path, scratch: Path) -> Path:
        """Extract one archive with the validator's limits; the digest is verified."""

        if role not in self.refs:
            raise PackError(f"{self.name}: no {role} archive")
        limits = VERIFIER_LIMITS if role == "verifier" else WORKSPACE_LIMITS
        archive = self.root / f"{role}.tar.zst"
        extract_archive(archive, self.refs[role], destination, limits, scratch_dir=scratch)
        return destination


def offer_pack(task, root: Path) -> TaskPack:
    """A pack for a live offer: its identity plus the downloaded environment archive (no verifier, no reference).

    ``root`` must hold ``<role>.tar.zst`` as downloaded and digest-checked by rlvr's ``download_artifact``.
    """

    identity = task.identity
    role = "terminal_environment" if isinstance(identity, TerminalScriptTaskIdentity) else "workspace"
    if task.workspace.artifact_role != role or not (root / f"{role}.tar.zst").is_file():
        raise PackError(f"offer {task.challenge_id}: no {role} archive")
    return TaskPack(root=root, identity=identity, refs={role: task.workspace}, task_id=task.task_id)


def resolve_pack_path(name_or_path: str | Path) -> Path:
    """Accept a pack directory, or the name of one under ``packs/``."""

    path = Path(name_or_path)
    if path.is_dir():
        return path
    candidates = sorted(PACKS_DIR.glob(f"{name_or_path}*")) if PACKS_DIR.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PackError(f"no task pack named {name_or_path!r}")
    raise PackError(f"{name_or_path!r} matches several packs: {', '.join(c.name for c in candidates)}")


def load_pack(name_or_path: str | Path) -> TaskPack:
    root = resolve_pack_path(name_or_path)
    try:
        raw_identity = json.loads((root / "identity.json").read_text(encoding="utf-8"))
        raw_refs = json.loads((root / "artifact_refs.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackError(f"{root.name}: missing {Path(exc.filename).name}") from None
    except ValueError as exc:
        raise PackError(f"{root.name}: invalid JSON ({exc})") from None

    task_type = raw_identity.get("task_type")
    models = {"repository_patch_v1": RepositoryTaskIdentity, "terminal_script_v1": TerminalScriptTaskIdentity}
    model = models.get(task_type)
    if model is None:
        raise PackError(f"{root.name}: unknown task_type {task_type!r}")
    identity = model.model_validate(raw_identity)
    refs = {name: ArtifactRef.model_validate(value) for name, value in raw_refs.items()}

    environment_role = "terminal_environment" if task_type == "terminal_script_v1" else "workspace"
    environment_sha = (
        identity.environment_sha256 if task_type == "terminal_script_v1" else identity.workspace_sha256
    )
    for role, expected in ((environment_role, environment_sha), ("verifier", identity.verifier_sha256)):
        ref = refs.get(role)
        if ref is None:
            raise PackError(f"{root.name}: artifact_refs.json has no {role}")
        if ref.artifact_role != role:
            raise PackError(f"{root.name}: ref {role} has role {ref.artifact_role}")
        if ref.sha256 != expected:
            raise PackError(f"{root.name}: {role} digest does not match the identity")
        if not (root / f"{role}.tar.zst").is_file():
            raise PackError(f"{root.name}: missing {role}.tar.zst")

    task_id = compute_task_id(identity)
    recorded = root / "task_id.txt"
    if recorded.is_file() and recorded.read_text().strip() != task_id:
        raise PackError(f"{root.name}: task_id.txt does not match the identity")
    return TaskPack(root=root, identity=identity, refs=refs, task_id=task_id)


def list_packs(directory: Path = PACKS_DIR) -> list[Path]:
    return sorted(path for path in directory.iterdir() if (path / "identity.json").is_file())
