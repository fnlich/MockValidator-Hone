"""Build the submission diff from a working tree, the way the grader needs it.

The diff is taken against the pristine baseline with a private git index, so the
workspace itself never needs (or gets) a ``.git`` directory. Before diffing,
everything the grader would refuse or that the answer must not touch is left
at its baseline state, and every such path is reported so Claude can be told:

- protected paths: task build files (``.rlvr/``, ``.prebuilt/``) and existing tests;
- build-owned paths: files the task's own build rewrites (when the caller knows them);
- new build output and caches (``__pycache__``, ``target/``, ``node_modules`` ...);
- binaries, symlinks, non-UTF-8 files (added, changed or deleted), and new files over the size limit.

Files are listed by walking the tree (skipping every ``.git``), not by ``git add``: a nested repository
(``git init`` or ``cargo new`` inside the task) would otherwise hide its files or stop the build.

The result is checked with rlvr's own ``static_rejection``.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rlvr.policy import RELEASE_POLICY
from rlvr.v3.patch import PatchLimits, static_rejection

from honeminer.config import host_path

NEW_FILE_MAX_BYTES = 256 * 1024
DEFAULT_PROTECTED = (".rlvr/**", ".prebuilt/**")
BUILD_OUTPUT = (
    "**/__pycache__/**", "*.pyc", "**/*.pyc", "**/*.o", "*.o", "**/*.a", "**/*.so",
    "target/**", "**/target/**", "node_modules/**", "**/node_modules/**",
    "**/*.class", ".pytest_cache/**", "**/.pytest_cache/**", ".gradle/**", "build/**",
)
PATCH_LIMITS = PatchLimits(max_patch_bytes=RELEASE_POLICY.v3_patch_bytes, git_timeout_s=30)


class PatchBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class PatchResult:
    diff: bytes
    changed: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...] = field(default=())
    rejection: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None

    def drop_report(self) -> str:
        return "\n".join(f"- {path}: {reason}" for path, reason in self.dropped)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    # An exact path matches itself even when it contains glob characters like "[".
    return any(path == pattern or fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


class _Index:
    """A throwaway git repository whose work tree can point at any directory."""

    def __init__(self, git_dir: Path) -> None:
        self.git_dir = git_dir
        self.env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": str(git_dir),
            "LC_ALL": "C",
            "PATH": host_path(),
            "GIT_AUTHOR_NAME": "honeminer", "GIT_AUTHOR_EMAIL": "honeminer@localhost",
            "GIT_COMMITTER_NAME": "honeminer", "GIT_COMMITTER_EMAIL": "honeminer@localhost",
        }

    def run(self, work_tree: Path, *args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={work_tree}", *args],
            input=stdin, capture_output=True, env=self.env, check=False,
        )
        if result.returncode != 0:
            raise PatchBuildError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout


def _baseline_index(baseline: Path, git_dir: Path) -> _Index:
    index = _Index(git_dir)
    init = ["git", "init", "-q", "--bare", str(git_dir)]
    subprocess.run(init, check=True, env=index.env, capture_output=True)
    for key, value in (("core.autocrlf", "false"), ("core.filemode", "false"), ("core.symlinks", "true"),
                       ("core.quotepath", "false"), ("diff.renames", "false")):
        index.run(baseline, "config", key, value)
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* -text -eol -filter\n")
    (info / "exclude").write_text("")
    index.run(baseline, "add", "-A", "-f", ".")
    index.run(baseline, "commit", "-q", "--allow-empty", "--no-verify", "-m", "baseline")
    return index


def _text_problem(data: bytes) -> str | None:
    if b"\x00" in data:
        return "binary file"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8 text"
    return None


def _classify(
    work: Path, baseline: Path, path: str, status: str, protected: tuple[str, ...], build_owned: set[str]
) -> str | None:
    """Why a changed path must stay at baseline, or None to keep the change."""

    if path in build_owned:
        return "rewritten by the task's build"
    if _matches(path, protected):
        return "protected (task build files and existing tests are read-only)"
    if status == "A" and _matches(path, BUILD_OUTPUT):
        return "build output or cache"
    if status == "D":
        # git writes "Binary files differ" (or non-UTF-8 text) for such a deletion; the grader refuses it.
        try:
            problem = _text_problem((baseline / path).read_bytes())
        except OSError:
            return None
        return None if problem is None else f"deleting a {problem} cannot be expressed in the patch"
    target = work / path
    if target.is_symlink():
        return "symlink (the grader accepts regular files only)"
    try:
        data = target.read_bytes()
    except OSError:
        return "unreadable"
    problem = _text_problem(data)
    if problem is not None:
        return problem
    if status == "A" and len(data) > NEW_FILE_MAX_BYTES:
        return f"new file over {NEW_FILE_MAX_BYTES // 1024} KiB"
    return None


def _tree_paths(root: Path) -> set[bytes]:
    """Every file and symlink under ``root``, skipping any ``.git`` (a nested repository's own data)."""

    found: set[bytes] = set()
    for directory, dirnames, filenames in os.walk(root):
        relative = Path(directory).relative_to(root)
        for name in list(dirnames):
            if name == ".git":
                dirnames.remove(name)
            elif os.path.islink(os.path.join(directory, name)):
                dirnames.remove(name)
                found.add(os.fsencode(relative / name))
        found.update(os.fsencode(relative / name) for name in filenames if name != ".git")
    return found


def _stage(index: _Index, work: Path) -> None:
    """Make the index hold exactly the work tree's files (baseline paths that are gone become deletions)."""

    tracked = {p for p in index.run(work, "ls-files", "-z").split(b"\0") if p}
    paths = sorted(tracked | _tree_paths(work))
    if paths:
        index.run(work, "update-index", "--add", "--remove", "--replace", "-z", "--stdin",
                  stdin=b"\0".join(paths) + b"\0")


def build_patch(
    baseline: Path,
    work: Path,
    *,
    protected: Iterable[str] = (),
    build_owned: Iterable[str] = (),
) -> PatchResult:
    """Diff ``work`` against ``baseline`` (both workspace roots)."""

    protected_patterns = (*DEFAULT_PROTECTED, *protected)
    owned = set(build_owned)
    with tempfile.TemporaryDirectory(prefix="honeminer-patch-") as scratch:
        index = _baseline_index(Path(baseline), Path(scratch) / "git")
        _stage(index, Path(work))
        listing = index.run(work, "diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD")
        fields = listing.decode("utf-8", "surrogateescape").split("\0")
        entries = [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]

        dropped: list[tuple[str, str]] = []
        kept: list[str] = []
        for status, path in entries:
            reason = _classify(Path(work), Path(baseline), path, status[0], protected_patterns, owned)
            if reason is None:
                kept.append(path)
            else:
                dropped.append((path, reason))
                index.run(work, "reset", "-q", "HEAD", "--", path)

        diff = index.run(
            work, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", "--full-index", "HEAD",
        ) if kept else b""
    rejection = static_rejection(diff, PATCH_LIMITS)
    return PatchResult(
        diff=diff,
        changed=tuple(kept),
        dropped=tuple(dropped),
        rejection=None if rejection is None else rejection.reason,
    )
