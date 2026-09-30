import random
import re
from pathlib import Path

import pytest

from honeminer.clock import MIN_AGENT_SHARE, Clock, ClockError
from honeminer.config import load_env

PACKAGE = Path(__file__).resolve().parent.parent / "honeminer"


class FakeTime:
    def __init__(self, wall: float, mono: float) -> None:
        self.wall_value, self.mono_value = wall, mono

    def wall(self) -> float:
        return self.wall_value

    def mono(self) -> float:
        return self.mono_value

    def advance(self, seconds: float) -> None:
        self.wall_value += seconds
        self.mono_value += seconds


def make(env=None, **kwargs):
    times = FakeTime(1_800_000_000.0, 500.0)
    settings = load_env(None, environ=env or {})
    clock = Clock.for_task(settings, wall=times.wall, monotonic=times.mono, **kwargs)
    return clock, times


def test_local_run_uses_the_task_budget():
    clock, _ = make()
    plan = clock.plan
    assert plan.end - plan.start == 1200
    assert plan.reply_by - plan.start == 1200 - 45
    assert plan.agent_stop - plan.start == 1200 - 45 - 55
    assert clock.fraction_left() == 1.0


def test_earliest_expiry_wins():
    clock, times = make(expires_at=1_800_000_000.0 + 900, slot_expiries=[1_800_000_000.0 + 700])
    assert clock.plan.end - clock.plan.start == 700


@pytest.mark.parametrize("seed", range(200))
def test_uploads_always_finish_before_the_validator_stops_listening(seed):
    rng = random.Random(seed)
    window = rng.uniform(300, 1200)
    slot = window + rng.uniform(-100, 300)
    env = {
        "HONEMINER_RESERVE_FINAL_CHECK_S": str(rng.randint(0, 120)),
        "HONEMINER_RESERVE_UPLOAD_S": str(rng.randint(0, 120)),
        "HONEMINER_RESERVE_CLEANUP_S": str(rng.randint(0, 30)),
    }
    try:
        clock, times = make(env, expires_at=1_800_000_000.0 + window, slot_expiries=[1_800_000_000.0 + slot])
    except ClockError:
        assert min(window, slot) <= 45
        return
    plan = clock.plan
    finish = plan.agent_stop + plan.reserve_final_check_s + plan.reserve_upload_s + plan.reserve_cleanup_s
    assert finish <= plan.end - 30 + 1e-9
    assert plan.agent_stop - plan.start >= MIN_AGENT_SHARE * (plan.reply_by - plan.start) - 1e-9


def test_short_window_shrinks_the_reserve_instead_of_failing():
    clock, _ = make({"HONEMINER_RESERVE_FINAL_CHECK_S": "300"}, expires_at=1_800_000_000.0 + 145)
    plan = clock.plan
    assert plan.reply_by - plan.start == 100
    assert plan.agent_stop - plan.start == pytest.approx(50)


def test_expired_offer_is_refused():
    with pytest.raises(ClockError):
        make(expires_at=1_800_000_000.0 + 40)


def test_start_gates_and_call_timeouts_follow_the_agent_stop():
    clock, times = make()
    times.advance(1000)
    remaining = clock.agent_remaining()
    assert remaining == pytest.approx(100)
    assert clock.can_start(60) and not clock.can_start(120)
    assert clock.call_timeout(30) == 30 and clock.call_timeout(500) == pytest.approx(100)
    assert clock.fraction_left() == pytest.approx(100 / 1100)
    times.advance(500)
    assert clock.agent_remaining() == 0 and clock.fraction_left() == 0


def test_wall_clock_jumps_do_not_move_deadlines():
    clock, times = make()
    stop = clock.plan.agent_stop
    times.wall_value += 3600
    assert clock.plan.agent_stop == stop
    assert clock.agent_remaining() == pytest.approx(1100)


def test_no_other_module_computes_its_own_deadline():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path.name in ("clock.py", "config.py"):
            continue
        text = path.read_text()
        if re.search(r"expires_at\s*-|time\.time\(\)\s*[+-]|\b280\b", text):
            offenders.append(path.name)
    assert offenders == []
