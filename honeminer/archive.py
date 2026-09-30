"""Per-run folders and one summary line per solve (local only; never committed)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

SUMMARY_FIELDS = (
    "time", "run", "task", "kind", "language", "prompt_version", "model", "effort", "outcome", "grade",
    "reason", "gate_rounds", "seconds", "budget_s", "input_tokens", "output_tokens", "rate_limited",
    "authorization", "trajectory_bytes", "trajectory_level", "trajectory_ok",
)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "task"


@dataclass
class RunArchive:
    root: Path
    secrets: tuple[str, ...] = ()

    @classmethod
    def create(cls, runs_dir: Path, task: str, secrets: tuple[str, ...] = ()) -> RunArchive:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = Path(runs_dir) / f"{stamp}-{_slug(task)}"
        path, n = base, 1
        while path.exists():
            n += 1
            path = base.with_name(f"{base.name}-{n}")
        path.mkdir(parents=True)
        return cls(path, tuple(s for s in secrets if s))

    def redact(self, data: bytes) -> bytes:
        for secret in self.secrets:
            data = data.replace(secret.encode(), b"***redacted***")
        return data

    def write(self, name: str, content: bytes | str | dict | list) -> Path:
        if isinstance(content, (dict, list)):
            content = json.dumps(content, indent=2, sort_keys=True, default=str) + "\n"
        data = content.encode() if isinstance(content, str) else content
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.redact(data))
        return path

    def summary(self, index: Path, **values: object) -> dict:
        """Append one summary line; its fields and order are fixed by SUMMARY_FIELDS."""

        unknown = set(values) - set(SUMMARY_FIELDS)
        if unknown:
            raise ValueError(f"unknown summary fields: {sorted(unknown)}")
        line = {key: values.get(key) for key in SUMMARY_FIELDS}
        line["time"] = line["time"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line["run"] = self.root.name
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a", encoding="utf-8") as handle:
            handle.write(self.redact(json.dumps(line, default=str).encode()).decode() + "\n")
        return line


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def prune_runs(runs_dir: Path, max_bytes: int, keep: Path | None = None) -> list[Path]:
    """Delete the oldest run folders until ``runs_dir`` fits ``max_bytes`` (0 = no cap); returns what was removed.

    Only run folders are removed, never the summary files (``index.jsonl``, ``offers.jsonl``, ``notices.jsonl``)
    and never ``keep`` (the run in progress).
    """

    import shutil

    runs_dir = Path(runs_dir)
    if max_bytes <= 0 or not runs_dir.is_dir():
        return []
    folders = sorted((p for p in runs_dir.iterdir() if p.is_dir() and not p.is_symlink()),
                     key=lambda p: p.stat().st_mtime)
    sizes = {p: _size(p) for p in folders}
    total = sum(sizes.values()) + sum(p.stat().st_size for p in runs_dir.iterdir() if p.is_file())
    removed = []
    for folder in folders:
        if total <= max_bytes:
            break
        if keep is not None and folder.resolve() == Path(keep).resolve():
            continue
        shutil.rmtree(folder, ignore_errors=True)
        total -= sizes[folder]
        removed.append(folder)
    return removed
