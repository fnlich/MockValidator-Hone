#!/usr/bin/env python3
"""A mock Hone Subnet validator: dispatch a problem to a miner and grade it.

This reproduces the exact dispatch path a real SN5 validator uses, minus the
chain and the private problem server, so you can exercise a miner end to end on
your own machine:

    derive a per-miner request id
      -> sign a TaskRequest for that miner's hotkey
      -> POST /solve
      -> verify the reply is signed BY the miner and FOR us
      -> grade the returned code against HIDDEN tests in the real sandbox
      -> report the payment the subnet would have assigned

Every security-relevant step calls the validator's own code from the ``rlvr``
package (``sign_message``, ``verify_signature``, ``derive_request_id``,
``Verifier``, ``compute_payments``). Nothing is re-implemented here: a harness
that rolled its own signing could drift from the real protocol and tell you a
broken miner is fine, which is the one thing a harness must never do.

Usage:

    # against a miner listening locally
    python mock_validator.py --url http://127.0.0.1:8091

    # one problem, verbose
    python mock_validator.py --problem sum-of-digits -v

Authorization note: a real miner only answers hotkeys that hold a validator
permit in its metagraph, and this harness's key holds none. Either run the
miner via ``run_local_miner.py`` (which installs a stub metagraph trusting this
harness), or start your miner with ``MINER_REQUIRE_VALIDATOR_PERMIT=false``
while testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from rlvr.config import Settings
from rlvr.execution.executor import get_executor
from rlvr.problemserver.api import derive_request_id
from rlvr.protocol import (
    SolutionPayload,
    TaskRequest,
    sign_message,
    verify_signature,
)
from rlvr.scoring.payment import compute_payments
from rlvr.scoring.verifier import Verifier
from rlvr.types import MinerOutcome, Problem, SolutionResponse, TestCase

PROBLEMS_DIR = Path(__file__).parent / "problems"

# Mirrors the real validator's defaults (rlvr/config.py, rlvr/neurons/live.py).
MINER_MAX_RESPONSE_BYTES = 128_000
RESPONSE_GRACE_S = 10.0

# Deterministic dev keys so runs are reproducible. These are well-known test
# seeds and hold nothing; never point them at anything that matters.
VALIDATOR_URI = "//HoneHarnessValidator"
DEFAULT_MINER_URI = "//HoneHarnessMiner"


def keypair(uri: str):
    from bittensor_wallet import Keypair

    return Keypair.create_from_uri(uri)


# --------------------------------------------------------------------------- #
# Problems
# --------------------------------------------------------------------------- #
@dataclass
class MockProblem:
    name: str
    language: str
    entrypoint: str
    statement: str
    public_examples: list[dict[str, Any]]
    hidden_tests: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> "MockProblem":
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = {
            "language", "entrypoint", "statement", "public_examples", "hidden_tests"
        } - set(data)
        if missing:
            raise ValueError(f"{path.name} is missing {sorted(missing)}")
        return cls(name=data.get("name", path.stem), **{
            k: data[k] for k in
            ("language", "entrypoint", "statement", "public_examples", "hidden_tests")
        })

    def cases(self, which: str) -> list[TestCase]:
        raw = self.public_examples if which == "public" else self.hidden_tests
        return [
            TestCase(
                args=list(case.get("args", []) or []),
                kwargs=dict(case.get("kwargs", {}) or {}),
                expected=case.get("expected"),
            )
            for case in raw
        ]


def load_problems(selector: Optional[str] = None) -> list[MockProblem]:
    paths = sorted(PROBLEMS_DIR.glob("*.json"))
    problems = [MockProblem.load(p) for p in paths]
    if selector:
        problems = [p for p in problems if selector in p.name]
    return problems


# --------------------------------------------------------------------------- #
# Dispatch — the same sequence rlvr/neurons/live.py performs
# --------------------------------------------------------------------------- #
@dataclass
class Dispatch:
    """What came back from the miner, before any grading."""

    ok: bool
    detail: str
    code: str = ""
    raw_response: str = ""
    latency_ms: float = 0.0
    status: Optional[int] = None
    signed: bool = False


async def dispatch(
    client: httpx.AsyncClient,
    url: str,
    problem: MockProblem,
    validator_kp,
    miner_hotkey: str,
    *,
    challenge_id: str,
    uid: int,
    deadline_s: float,
) -> Dispatch:
    # The miner never learns the real problem id — only this per-dispatch digest.
    request_id = derive_request_id(challenge_id, uid, miner_hotkey)
    request = TaskRequest(
        problem_id=request_id,
        language=problem.language,
        statement=problem.statement,
        entrypoint=problem.entrypoint,
        public_examples=problem.cases("public"),
        deadline_s=deadline_s,
    )
    body = request.model_dump_json().encode("utf-8")
    headers = sign_message(validator_kp, body, signed_for=miner_hotkey)
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(body))

    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.post(f"{url.rstrip('/')}/solve", content=body, headers=headers),
            timeout=deadline_s + RESPONSE_GRACE_S,
        )
    except asyncio.TimeoutError:
        return Dispatch(False, f"no response within {deadline_s + RESPONSE_GRACE_S:.0f}s",
                        latency_ms=(time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - an unreachable miner scores zero
        return Dispatch(False, f"transport error: {type(exc).__name__}: {exc}",
                        latency_ms=(time.monotonic() - started) * 1000)
    latency_ms = (time.monotonic() - started) * 1000

    if response.status_code != 200:
        detail = response.text[:200].replace("\n", " ")
        return Dispatch(False, f"HTTP {response.status_code}: {detail}",
                        latency_ms=latency_ms, status=response.status_code)
    if len(response.content) > MINER_MAX_RESPONSE_BYTES:
        return Dispatch(False, "response exceeds the 128 KB cap",
                        latency_ms=latency_ms, status=200)

    reply_headers = {
        name: response.headers.get(name, "")
        for name in (
            "Epistula-Version", "Epistula-Timestamp", "Epistula-Uuid",
            "Epistula-Signed-By", "Epistula-Signed-For", "Epistula-Request-Signature",
        )
    }
    # The four checks a real validator applies before it will grade anything.
    if reply_headers["Epistula-Signed-By"] != miner_hotkey:
        return Dispatch(False, "reply not signed by the miner's hotkey",
                        latency_ms=latency_ms, status=200)
    if not verify_signature(
        reply_headers, response.content, expected_signed_for=headers["Epistula-Signed-By"]
    ):
        return Dispatch(False, "reply signature invalid or not bound to this validator",
                        latency_ms=latency_ms, status=200)
    try:
        payload = SolutionPayload.model_validate_json(response.content)
    except Exception as exc:  # noqa: BLE001
        return Dispatch(False, f"reply is not a SolutionPayload: {str(exc)[:160]}",
                        latency_ms=latency_ms, status=200, signed=True)
    if payload.problem_id != request.problem_id:
        return Dispatch(False, "reply echoed the wrong request id",
                        latency_ms=latency_ms, status=200, signed=True)

    return Dispatch(True, "authenticated", code=payload.code,
                    raw_response=payload.raw_response, latency_ms=latency_ms,
                    status=200, signed=True)


# --------------------------------------------------------------------------- #
# Grading — the validator's own Verifier against the HIDDEN tests
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    problem: str
    accepted: bool          # the reply authenticated
    detail: str
    passed: int = 0
    total: int = 0
    all_passed: bool = False
    payment: float = 0.0
    latency_ms: float = 0.0
    compile_error: Optional[str] = None
    skipped: bool = False   # this host cannot grade it (e.g. rust without Docker)


def grade(problem: MockProblem, result: Dispatch, executor_kind: str) -> Verdict:
    if not result.ok:
        return Verdict(problem.name, False, result.detail, latency_ms=result.latency_ms)

    settings = Settings(_env_file=None, executor=executor_kind)
    try:
        executor = get_executor(settings, language=problem.language)
    except Exception as exc:  # noqa: BLE001
        # A grading backend this host cannot provide (Rust needs the Docker
        # executor and a running daemon) must not destroy the whole run: the
        # other problems still have verdicts worth reporting.
        return Verdict(
            problem.name, True, f"cannot grade {problem.language} here: {exc}",
            latency_ms=result.latency_ms, skipped=True,
        )
    verifier = Verifier(executor, settings)
    local = Problem(
        problem_id=problem.name,
        language=problem.language,
        statement=problem.statement,
        entrypoint=problem.entrypoint,
        tests=problem.cases("hidden"),
        public_examples=problem.cases("public"),
    )
    solution = SolutionResponse(
        problem_id=problem.name, code=result.code,
        raw_response=result.raw_response, latency_ms=result.latency_ms,
    )
    verification = verifier.verify(local, solution)
    outcome = MinerOutcome(
        uid=0, hotkey="mock", solution=solution,
        verification=verification, reward=verification.reward,
    )
    payment = compute_payments([outcome]).get(0, 0.0)
    return Verdict(
        problem=problem.name, accepted=True, detail=result.detail,
        passed=verification.num_passed, total=verification.num_tests,
        all_passed=verification.all_passed, payment=payment,
        latency_ms=result.latency_ms, compile_error=verification.compile_error,
    )


# --------------------------------------------------------------------------- #
async def run(args) -> int:
    problems = load_problems(args.problem)
    if not problems:
        print(f"no problems matched {args.problem!r} in {PROBLEMS_DIR}", file=sys.stderr)
        return 2

    validator_kp = keypair(args.validator_uri)
    miner_hotkey = args.miner_hotkey or keypair(DEFAULT_MINER_URI).ss58_address
    print(f"validator {validator_kp.ss58_address}")
    print(f"miner     {miner_hotkey}  @ {args.url}")
    print(f"executor  {args.executor}   problems: {len(problems)}\n")

    verdicts: list[Verdict] = []
    async with httpx.AsyncClient() as client:
        for index, problem in enumerate(problems):
            result = await dispatch(
                client, args.url, problem, validator_kp, miner_hotkey,
                challenge_id=f"{args.challenge_id}-{index}", uid=args.uid,
                deadline_s=args.deadline,
            )
            verdict = grade(problem, result, args.executor)
            verdicts.append(verdict)
            mark = "SKIP" if verdict.skipped else ("PASS" if verdict.all_passed else "FAIL")
            print(
                f"[{mark}] {verdict.problem:<28} "
                f"{verdict.passed}/{verdict.total} hidden  "
                f"pay={verdict.payment:.3f}  {verdict.latency_ms / 1000:.1f}s"
            )
            if verdict.skipped or not verdict.accepted:
                label = "skipped" if verdict.skipped else "rejected"
                print(f"         {label}: {verdict.detail}")
            elif verdict.compile_error:
                print(f"         {verdict.compile_error.splitlines()[0][:150]}")
            if args.verbose and verdict.accepted and not verdict.skipped:
                print("         --- code ---")
                for line in result.code.splitlines()[:20]:
                    print(f"         {line}")

    graded = [v for v in verdicts if not v.skipped]
    skipped = len(verdicts) - len(graded)
    solved = sum(1 for v in graded if v.all_passed)
    earned = sum(v.payment for v in graded)
    print(
        f"\n{solved}/{len(graded)} solved   total payment {earned:.3f}"
        + (f"   ({skipped} skipped)" if skipped else "")
    )
    print("(the subnet pays only for a FULL hidden-suite pass; partial == zero)")
    return 0 if graded and solved == len(graded) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8091",
                        help="miner base URL (default: http://127.0.0.1:8091)")
    parser.add_argument("--problem", help="only problems whose name contains this")
    parser.add_argument("--miner-hotkey",
                        help="the miner's ss58 hotkey (default: the harness dev key)")
    parser.add_argument("--validator-uri", default=VALIDATOR_URI,
                        help="dev URI for the harness validator key")
    parser.add_argument("--executor", default="subprocess", choices=("subprocess", "docker"),
                        help="sandbox used for grading; rust requires docker")
    parser.add_argument("--deadline", type=float, default=120.0,
                        help="seconds advertised to the miner (default: 120)")
    parser.add_argument("--challenge-id", default="mock-challenge")
    parser.add_argument("--uid", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
