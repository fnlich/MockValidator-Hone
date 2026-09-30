"""The one clock: every deadline in honeminer comes from here.

A task has one hard end, E: the earliest of the offer's ``expires_at``, the
upload slots' expiries, and arrival + the configured task budget. The reply
must be sent by E - margin (the validator stops listening at about E - 30 s).
Claude works until the agent stop, which leaves the reserve (final clean-room
check + upload + clean-up) before the reply deadline.

There are no per-stage caps. A stage asks ``can_start(expected_s)`` and, once
started, runs until the agent stop. Everything is kept in monotonic time, so a
wall-clock jump cannot extend a deadline.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from honeminer.config import Settings

# Never leave the agent less than this share of a short window.
MIN_AGENT_SHARE = 0.5


class ClockError(ValueError):
    """The task cannot be answered in the time available."""


@dataclass(frozen=True)
class Plan:
    """A task's deadlines, in monotonic seconds."""

    start: float
    end: float  # E
    reply_by: float
    agent_stop: float
    reserve_final_check_s: float
    reserve_upload_s: float
    reserve_cleanup_s: float


class Clock:
    def __init__(
        self,
        plan: Plan,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self._monotonic = monotonic

    @classmethod
    def for_task(
        cls,
        settings: Settings,
        *,
        expires_at: float | None = None,
        slot_expiries: Iterable[float] = (),
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Clock:
        """Build the clock at the moment a task arrives.

        ``expires_at`` and ``slot_expiries`` are wall-clock UNIX seconds from
        the offer; a local run passes neither and gets the plain task budget.
        """

        now_wall, now_mono = wall(), monotonic()
        ends = [now_wall + settings.task_budget_s]
        if expires_at is not None:
            ends.append(float(expires_at))
        ends.extend(float(value) for value in slot_expiries)
        end = now_mono + (min(ends) - now_wall)

        reply_by = end - settings.expiry_margin_s
        window = reply_by - now_mono
        if window <= 0:
            raise ClockError("the task expires before a reply could be sent")

        final, upload, cleanup = (
            float(settings.reserve_final_check_s),
            float(settings.reserve_upload_s),
            float(settings.reserve_cleanup_s),
        )
        reserve = final + upload + cleanup
        allowed = window * (1.0 - MIN_AGENT_SHARE)
        if reserve > allowed:
            # Short window: shrink the reserve in proportion instead of giving up.
            scale = allowed / reserve
            final, upload, cleanup = final * scale, upload * scale, cleanup * scale
            reserve = allowed

        plan = Plan(
            start=now_mono,
            end=end,
            reply_by=reply_by,
            agent_stop=reply_by - reserve,
            reserve_final_check_s=final,
            reserve_upload_s=upload,
            reserve_cleanup_s=cleanup,
        )
        return cls(plan, monotonic=monotonic)

    def now(self) -> float:
        return self._monotonic()

    def agent_remaining(self) -> float:
        """Seconds until the agent must stop (never negative)."""

        return max(0.0, self.plan.agent_stop - self.now())

    def reply_remaining(self) -> float:
        return max(0.0, self.plan.reply_by - self.now())

    def fraction_left(self) -> float:
        """Share of the agent's working window still unused, 1.0 at start."""

        total = self.plan.agent_stop - self.plan.start
        return 0.0 if total <= 0 else min(1.0, self.agent_remaining() / total)

    def can_start(self, expected_s: float) -> bool:
        """A stage starts only if its expected duration fits before the agent stop."""

        return expected_s <= self.agent_remaining()

    def call_timeout(self, own_timeout_s: float | None = None) -> float:
        """Timeout for one call: its own limit, but never past the agent stop."""

        remaining = self.agent_remaining()
        return remaining if own_timeout_s is None else min(own_timeout_s, remaining)

    def work_log_timeout(self, own_timeout_s: float) -> float:
        """Time box for building the work log: its own limit, but never into the upload and clean-up reserve.

        Never below half a second, so the minimal log (built in milliseconds) always has time.
        """

        spare = self.reply_remaining() - self.plan.reserve_upload_s - self.plan.reserve_cleanup_s
        return max(0.5, min(own_timeout_s, spare))

    def agent_stop_wall(self, wall: Callable[[], float] = time.time) -> float:
        """The agent stop as a UNIX timestamp, for hooks inside the sandbox."""

        return wall() + (self.plan.agent_stop - self.now())
