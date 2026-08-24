"""Regression tests for the mock validator.

These run the whole loop in-process against a real signed miner — no network,
no chain — so they cover the part that would otherwise only be exercised by
hand: that the harness genuinely authenticates and grades, that pool-relative
payment works, and that it fails when it should rather than rubber-stamping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mock_validator as mv  # noqa: E402
from miners import Miner, build_pool, parse_miner  # noqa: E402

pytest.importorskip("bittensor_wallet")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from rlvr.neurons.demo_miner import (  # noqa: E402
    DemoMiner,
    DemoMinerSettings,
    build_demo_miner_app,
)
from rlvr.protocol import SolutionPayload  # noqa: E402
from rlvr.scoring.payment import compute_payments  # noqa: E402
from rlvr.types import Verification  # noqa: E402

VALIDATOR = mv.keypair(mv.VALIDATOR_URI)
MINER = mv.keypair(mv.DEFAULT_MINER_URI)
DEV = MINER.ss58_address

CORRECT = "def sum_of_digits(n):\n    return sum(int(c) for c in str(n))\n"
NEARLY = "def sum_of_digits(n):\n    return sum(int(c) for c in str(n)) - 1\n"


def _problem(name="sum-of-digits") -> mv.MockProblem:
    matches = mv.load_problems(name)
    assert matches, f"{name} not found in {mv.PROBLEMS_DIR}"
    return matches[0]


def _miner(hotkey: Optional[str] = None) -> Miner:
    return Miner(uid=1, host="miner.test", port=80, hotkey=hotkey or DEV)


def _miner_app(code: str):
    """A real DemoMiner returning fixed code, behind a stub metagraph."""

    class Fixed(DemoMiner):
        async def solve(self, request, timeout_s):
            return SolutionPayload(problem_id=request.problem_id, code=code)

    metagraph = SimpleNamespace(
        hotkeys=[VALIDATOR.ss58_address], validator_permit=[True], S=[1.0]
    )
    miner = Fixed(
        DemoMinerSettings(_env_file=None), None,
        wallet=SimpleNamespace(hotkey=MINER), subtensor=None, metagraph=metagraph,
    )
    return build_demo_miner_app(miner)


class _AppTransport:
    """Route the harness's httpx calls into an in-process ASGI app."""

    def __init__(self, app):
        self._client = TestClient(app)

    async def post(self, url, *, content, headers):
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
        return self._client.post(path, content=content, headers=headers)


async def _dispatch(app, problem, *, miner=None, validator=VALIDATOR) -> mv.Dispatch:
    return await mv.dispatch_one(
        _AppTransport(app), miner or _miner(), problem, validator,
        challenge_id="t", deadline_s=30.0,
    )


def _grade_one(problem, result: mv.Dispatch):
    """Grade a single dispatch the way run_round does, via the real Grader."""
    verification, solution = mv.Grader("subprocess").verify(problem, result)
    outcome = mv.MinerOutcome(
        uid=result.miner.uid, hotkey=result.miner.hotkey, solution=solution,
        verification=verification, reward=verification.reward,
    )
    return verification, compute_payments([outcome]).get(result.miner.uid, 0.0)


# --------------------------------------------------------------------------- #
# Problems
# --------------------------------------------------------------------------- #
def test_problems_ship_public_and_hidden_cases():
    for problem in mv.load_problems():
        assert problem.public_examples, f"{problem.name} has no public examples"
        assert problem.hidden_tests, f"{problem.name} has no hidden tests"
        assert problem.language in {"python", "rust"}


def test_hidden_tests_are_not_leaked_to_the_miner():
    """The whole subnet rests on this: a miner may only see public examples.

    Checked structurally rather than by substring: a scalar like ``0`` occurs
    all over a JSON body for innocent reasons, so the invariant is that the
    request carries exactly the public cases and no other case object.
    """
    from rlvr.protocol import TaskRequest

    problem = _problem()
    hidden_only = [c for c in problem.hidden_tests if c not in problem.public_examples]
    assert hidden_only, "this problem needs a hidden case that is not also public"

    request = TaskRequest(
        problem_id="x", language=problem.language, statement=problem.statement,
        entrypoint=problem.entrypoint, public_examples=problem.cases("public"),
    )
    wire = json.loads(request.model_dump_json())

    sent = [(c["args"], c["kwargs"], c["expected"]) for c in wire["public_examples"]]
    public = [
        (c.get("args", []), c.get("kwargs", {}), c.get("expected"))
        for c in problem.public_examples
    ]
    assert sent == public, "the request must carry exactly the public examples"
    for case in hidden_only:
        key = (case.get("args", []), case.get("kwargs", {}), case.get("expected"))
        assert key not in sent, f"hidden case {key} reached the miner"
    assert set(wire) == {
        "problem_id", "language", "statement", "entrypoint",
        "public_examples", "deadline_s", "prompt_variant",
    }


# --------------------------------------------------------------------------- #
# Dispatch and grading
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_correct_miner_is_paid():
    result = await _dispatch(_miner_app(CORRECT), _problem())
    assert result.ok
    verification, payment = _grade_one(_problem(), result)
    assert verification.all_passed and verification.num_passed == verification.num_tests
    assert payment > 0


@pytest.mark.asyncio
async def test_an_almost_correct_miner_is_paid_nothing():
    """Accuracy-or-nothing: passing most hidden tests is worth exactly zero."""
    result = await _dispatch(_miner_app(NEARLY), _problem())
    assert result.ok
    verification, payment = _grade_one(_problem(), result)
    assert not verification.all_passed
    assert payment == 0.0


@pytest.mark.asyncio
async def test_empty_code_is_accepted_on_the_wire_but_scores_zero():
    result = await _dispatch(_miner_app(""), _problem())
    assert result.ok                     # the reply authenticated fine
    verification, payment = _grade_one(_problem(), result)
    assert not verification.all_passed and payment == 0.0


@pytest.mark.asyncio
async def test_an_unauthorized_validator_is_rejected():
    stranger = mv.keypair("//Stranger")
    result = await _dispatch(_miner_app(CORRECT), _problem(), validator=stranger)
    assert not result.ok and result.status == 403


@pytest.mark.asyncio
async def test_a_reply_signed_for_the_wrong_peer_is_rejected():
    other = mv.keypair("//SomeoneElse").ss58_address
    result = await _dispatch(_miner_app(CORRECT), _problem(), miner=_miner(other))
    assert not result.ok and result.status == 401


def test_an_ungradeable_language_raises_so_the_round_can_skip_it():
    """Rust without Docker must surface as a skip, not destroy the run."""
    with pytest.raises(Exception, match="Docker"):
        mv.Grader("subprocess").verifier("rust")


# --------------------------------------------------------------------------- #
# Miner pool parsing
# --------------------------------------------------------------------------- #
def test_parse_host_port_defaults_to_the_dev_hotkey():
    miner = parse_miner("127.0.0.1:8091", uid=1, default_hotkey=DEV)
    assert (miner.host, miner.port, miner.hotkey) == ("127.0.0.1", 8091, DEV)
    assert miner.url == "http://127.0.0.1:8091"


def test_parse_accepts_an_explicit_ss58_and_a_dev_uri():
    explicit = parse_miner(f"h:1={DEV}", uid=1, default_hotkey="x")
    assert explicit.hotkey == DEV
    derived = parse_miner("h:1=//M1", uid=1, default_hotkey="x")
    assert derived.hotkey == mv.keypair("//M1").ss58_address


@pytest.mark.parametrize("spec", ["nohost", "h:notaport", "h:0", "h:70000"])
def test_bad_miner_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        parse_miner(spec, uid=1, default_hotkey=DEV)


def test_duplicate_uids_are_rejected_because_payments_are_keyed_by_uid(tmp_path):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps([
        {"uid": 1, "host": "a", "port": 1}, {"uid": 1, "host": "b", "port": 2},
    ]))
    with pytest.raises(ValueError, match="duplicate uid"):
        build_pool([], str(path), None, DEV)


def test_a_pool_file_and_flags_combine_with_distinct_uids(tmp_path):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps([{"host": "a", "port": 1}]))
    pool = build_pool(["b:2"], str(path), None, DEV)
    assert [m.uid for m in pool] == [1, 2]


# --------------------------------------------------------------------------- #
# Solver loading — a class must be INSTANTIATED, not passed through
# --------------------------------------------------------------------------- #
def test_a_solver_class_is_instantiated_not_returned_as_a_class():
    """Regression: a class carries solve_task as an attribute, so an attribute
    test mistakes it for an instance and the miner calls solve_task unbound with
    the task as self — failing every request while looking like a solver bug."""
    import inspect

    import run_local_miner

    solver = run_local_miner.load_solver("run_local_miner:EchoSolver")
    assert not inspect.isclass(solver), "load_solver returned the class itself"
    assert callable(solver.solve_task) and callable(solver.aclose)


def test_a_non_solver_target_is_rejected_with_a_useful_message():
    import run_local_miner

    # A class that instantiates fine but is not a Solver.
    with pytest.raises(SystemExit, match="no callable"):
        run_local_miner.load_solver("json:JSONDecoder")


# --------------------------------------------------------------------------- #
# Pool-relative payment: the latency tiebreaker only exists across a pool
# --------------------------------------------------------------------------- #
def _passing(uid: int, latency_ms: float) -> mv.MinerOutcome:
    return mv.MinerOutcome(
        uid=uid, hotkey=f"hk{uid}",
        solution=mv.SolutionResponse(problem_id="p", code="x", latency_ms=latency_ms),
        verification=Verification(
            problem_id="p", num_tests=1, num_passed=1, all_passed=True, reward=1.0,
        ),
        reward=1.0,
    )


def test_a_slower_correct_miner_is_paid_slightly_less_than_the_fastest():
    payments = compute_payments([_passing(1, 10.0), _passing(2, 3010.0)])
    assert payments[1] == 1.0                       # fastest correct answer
    assert 0.95 < payments[2] < 1.0                 # 3s later, inside the band
    assert payments[2] == pytest.approx(0.999426, abs=1e-5)


def test_a_wrong_answer_is_paid_zero_no_matter_how_fast():
    fast_wrong = mv.MinerOutcome(
        uid=1, hotkey="hk1",
        solution=mv.SolutionResponse(problem_id="p", code="x", latency_ms=1.0),
        verification=Verification(
            problem_id="p", num_tests=5, num_passed=4, all_passed=False, reward=0.0,
        ),
        reward=0.0,
    )
    assert compute_payments([fast_wrong])[1] == 0.0
