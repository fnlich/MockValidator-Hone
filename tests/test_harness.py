"""Regression tests for the mock validator.

These run the whole loop in-process against a real signed miner — no network,
no chain — so they cover the part that would otherwise only be exercised by
hand: that the harness genuinely authenticates and grades, and that it fails
when it should rather than rubber-stamping.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mock_validator as mv  # noqa: E402

keypair_mod = pytest.importorskip("bittensor_wallet")
TestClient = pytest.importorskip("starlette.testclient").TestClient

from rlvr.neurons.demo_miner import (  # noqa: E402
    DemoMiner,
    DemoMinerSettings,
    build_demo_miner_app,
)
from rlvr.protocol import SolutionPayload  # noqa: E402

VALIDATOR = mv.keypair(mv.VALIDATOR_URI)
MINER = mv.keypair(mv.DEFAULT_MINER_URI)

CORRECT = "def sum_of_digits(n):\n    return sum(int(c) for c in str(n))\n"
NEARLY = "def sum_of_digits(n):\n    return sum(int(c) for c in str(n)) - 1\n"


def _problem(name="sum-of-digits") -> mv.MockProblem:
    matches = mv.load_problems(name)
    assert matches, f"{name} not found in {mv.PROBLEMS_DIR}"
    return matches[0]


def _miner_app(code: str, *, trusted=True):
    """A real DemoMiner returning fixed code, behind a stub metagraph."""

    class Fixed(DemoMiner):
        async def solve(self, request, timeout_s):
            return SolutionPayload(problem_id=request.problem_id, code=code)

    hotkeys = [VALIDATOR.ss58_address] if trusted else [MINER.ss58_address]
    metagraph = SimpleNamespace(hotkeys=hotkeys, validator_permit=[True], S=[1.0])
    miner = Fixed(
        DemoMinerSettings(_env_file=None), None,
        wallet=SimpleNamespace(hotkey=MINER), subtensor=None, metagraph=metagraph,
    )
    return build_demo_miner_app(miner)


class _AppTransport:
    """Route the harness's httpx calls into an in-process ASGI app."""

    def __init__(self, app): self._client = TestClient(app)

    async def post(self, url, *, content, headers):
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
        return self._client.post(path, content=content, headers=headers)


async def _dispatch(app, problem, *, miner_hotkey=None, validator=VALIDATOR):
    return await mv.dispatch(
        _AppTransport(app), "http://miner.test", problem, validator,
        miner_hotkey or MINER.ss58_address,
        challenge_id="t", uid=1, deadline_s=30.0,
    )


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
    import json

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
    # And the wire model has no field capable of carrying hidden cases at all.
    assert set(wire) == {
        "problem_id", "language", "statement", "entrypoint",
        "public_examples", "deadline_s", "prompt_variant",
    }


@pytest.mark.asyncio
async def test_a_correct_miner_is_paid():
    verdict = mv.grade(
        _problem(), await _dispatch(_miner_app(CORRECT), _problem()), "subprocess"
    )
    assert verdict.accepted and verdict.all_passed
    assert verdict.passed == verdict.total
    assert verdict.payment > 0


@pytest.mark.asyncio
async def test_an_almost_correct_miner_is_paid_nothing():
    """Accuracy-or-nothing: passing most hidden tests is worth exactly zero."""
    verdict = mv.grade(
        _problem(), await _dispatch(_miner_app(NEARLY), _problem()), "subprocess"
    )
    assert verdict.accepted and not verdict.all_passed
    assert verdict.payment == 0.0


@pytest.mark.asyncio
async def test_an_unauthorized_validator_is_rejected():
    stranger = mv.keypair("//Stranger")
    result = await _dispatch(_miner_app(CORRECT), _problem(), validator=stranger)
    assert not result.ok and result.status == 403


@pytest.mark.asyncio
async def test_a_reply_signed_for_the_wrong_peer_is_rejected():
    other = mv.keypair("//SomeoneElse").ss58_address
    result = await _dispatch(_miner_app(CORRECT), _problem(), miner_hotkey=other)
    assert not result.ok and result.status == 401


@pytest.mark.asyncio
async def test_empty_code_is_accepted_on_the_wire_but_scores_zero():
    verdict = mv.grade(
        _problem(), await _dispatch(_miner_app(""), _problem()), "subprocess"
    )
    assert verdict.accepted          # the reply authenticated fine
    assert not verdict.all_passed and verdict.payment == 0.0


def test_an_ungradeable_language_is_skipped_not_fatal():
    """Rust without Docker must not destroy the other problems' verdicts."""
    rust = _problem("rust")
    ok = mv.Dispatch(True, "authenticated", code="fn main(){}", latency_ms=1.0)
    verdict = mv.grade(rust, ok, "subprocess")
    assert verdict.skipped and verdict.accepted
    assert "Docker" in verdict.detail or "docker" in verdict.detail
