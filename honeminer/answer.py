"""The answer keeper: something shippable is always held, and never downgraded.

Every candidate answer (a snapshot of the patch or script) is ranked by the
strongest evidence it has earned. A new candidate replaces the held one only
if it ranks at least as high, so an empty output or a half-finished edit can
never displace a checked answer. Among equals the latest wins. Verdicts are
remembered by content hash, so an unchanged answer is never checked twice.
"""

from __future__ import annotations

import enum
import hashlib
import threading
from dataclasses import dataclass


class Rank(enum.IntEnum):
    EMPTY = 0
    STATIC_OK = 1  # passes rlvr static_rejection / validate_script
    APPLIES = 2  # applies cleanly to a pristine copy (patches) / runs (scripts)
    BUILDS = 3  # the task's own build succeeds
    CHECKED = 4  # the gate passed: each check fails on the original and passes now


@dataclass(frozen=True)
class Candidate:
    content: bytes
    rank: Rank
    note: str = ""
    sequence: int = 0

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class AnswerKeeper:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Candidate | None = None
        self._verdicts: dict[str, tuple[Rank, str]] = {}
        self._sequence = 0

    def offer(self, content: bytes, rank: Rank, note: str = "") -> bool:
        """Record a verdict and hold the candidate if it is at least as good."""

        if not content:
            rank = Rank.EMPTY
        with self._lock:
            self._sequence += 1
            candidate = Candidate(content, rank, note, self._sequence)
            known = self._verdicts.get(candidate.sha256)
            if known is None or rank >= known[0]:
                self._verdicts[candidate.sha256] = (rank, note)
            if self._held is None or (content and rank >= self._held.rank):
                self._held = candidate
                return True
            return False

    def verdict(self, content: bytes) -> tuple[Rank, str] | None:
        """The best verdict already earned by byte-identical content."""

        with self._lock:
            return self._verdicts.get(hashlib.sha256(content).hexdigest())

    @property
    def best(self) -> Candidate | None:
        with self._lock:
            return self._held
