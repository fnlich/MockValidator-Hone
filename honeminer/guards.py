"""Stall guard: stop a run that keeps asking the gate about the same unchanged failure."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

REPEATS_TO_STALL = 3


@dataclass
class StallGuard:
    repeats: int = REPEATS_TO_STALL
    seen: Counter = field(default_factory=Counter)

    def observe(self, content: bytes, stage: str, hint: str) -> bool:
        """Record one failing gate round; True once the same state has repeated enough to stop."""

        key = hashlib.sha256(content + b"\0" + stage.encode() + b"\0" + hint.encode()).hexdigest()
        self.seen[key] += 1
        return self.seen[key] >= self.repeats
