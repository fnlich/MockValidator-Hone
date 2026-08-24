#!/usr/bin/env python3
"""A mock Hone Subnet validator: dispatch a problem to a miner POOL and grade it.

This reproduces the dispatch path a real SN5 validator uses, minus the chain and
the private problem server, so you can exercise miners end to end on your own
machine:

    for each problem:
      derive a per-miner request id      (challenge_id + uid + hotkey)
        -> sign a TaskRequest for each miner's own hotkey
        -> POST /solve to every miner CONCURRENTLY
        -> verify each reply is signed BY that miner and FOR us
        -> grade each returned program against HIDDEN tests in the real sandbox
        -> compute payments ACROSS THE POOL and report the round

Dispatching to a pool rather than one miner is what makes the numbers real. The
payment formula's latency term is relative to the fastest *correct* responder
(``rlvr.scoring.payment``), so against a single miner it is always 1.0 and the
0.95-1.0 spread never appears. Pool pass-rate and the difficulty band are
likewise pool-level signals that do not exist for one miner.

Every security-relevant step calls the validator's own code from the ``rlvr``
package (``sign_message``, ``verify_signature``, ``derive_request_id``,
``Verifier``, ``compute_payments``, ``pass_rate``). Nothing is re-implemented
here: a harness that rolled its own signing could drift from the real protocol
and tell you a broken miner is fine, which is the one thing a harness must
never do.

Usage:

    # one miner
    python mock_validator.py --miner 127.0.0.1:8091

    # a pool, each with its own hotkey (a //Dev URI is accepted for local runs)
    python mock_validator.py --miner 127.0.0.1:8091=//M1 \
                             --miner 127.0.0.1:8092=//M2 \
                             --miner 10.0.0.7:8091=5F...

    # or from a file
    python mock_validator.py --miners pool.json

Authorization note: a real miner only answers hotkeys holding a validator permit
in its metagraph, and this harness's key holds none. Either run miners via
``run_local_miner.py`` (which installs a stub metagraph trusting this harness),
or start them with ``MINER_REQUIRE_VALIDATOR_PERMIT=false`` while testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from dataclasses import dataclass, field
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
from rlvr.scoring.difficulty import classify_band, pass_rate
from rlvr.scoring.payment import compute_payments
from rlvr.scoring.verifier import Verifier
from rlvr.types import MinerOutcome, Problem, SolutionResponse, TestCase

from miners import Miner, build_pool

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
# Dispatch — the same sequence rlvr/neurons/live.py performs, per miner
# --------------------------------------------------------------------------- #
@dataclass
class Dispatch:
    """What came back from one miner, before any grading."""

    miner: Miner
    ok: bool
    detail: str
    code: str = ""
    raw_response: str = ""
    latency_ms: float = 0.0
    status: Optional[int] = None


async def dispatch_one(
    client: httpx.AsyncClient,
    miner: Miner,
    problem: MockProblem,
    validator_kp,
    *,
    challenge_id: str,
    deadline_s: float,
) -> Dispatch:
    # The miner never learns the real problem id — only this per-dispatch digest,
    # derived from its own uid and hotkey exactly as the subnet does.
    request_id = derive_request_id(challenge_id, miner.uid, miner.hotkey)
    request = TaskRequest(
        problem_id=request_id,
        language=problem.language,
        statement=problem.statement,
        entrypoint=problem.entrypoint,
        public_examples=problem.cases("public"),
        deadline_s=deadline_s,
    )
    body = request.model_dump_json().encode("utf-8")
    headers = sign_message(validator_kp, body, signed_for=miner.hotkey)
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(body))

    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.post(f"{miner.url}/solve", content=body, headers=headers),
            timeout=deadline_s + RESPONSE_GRACE_S,
        )
    except asyncio.TimeoutError:
        return Dispatch(miner, False, f"no response within {deadline_s + RESPONSE_GRACE_S:.0f}s",
                        latency_ms=(time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - an unreachable miner scores zero
        return Dispatch(miner, False, f"transport error: {type(exc).__name__}: {exc}",
                        latency_ms=(time.monotonic() - started) * 1000)
    latency_ms = (time.monotonic() - started) * 1000

    if response.status_code != 200:
        detail = response.text[:200].replace("\n", " ")
        return Dispatch(miner, False, f"HTTP {response.status_code}: {detail}",
                        latency_ms=latency_ms, status=response.status_code)
    if len(response.content) > MINER_MAX_RESPONSE_BYTES:
        return Dispatch(miner, False, "response exceeds the 128 KB cap",
                        latency_ms=latency_ms, status=200)

    reply_headers = {
        name: response.headers.get(name, "")
        for name in (
            "Epistula-Version", "Epistula-Timestamp", "Epistula-Uuid",
            "Epistula-Signed-By", "Epistula-Signed-For", "Epistula-Request-Signature",
        )
    }
    # The four checks a real validator applies before it will grade anything.
    if reply_headers["Epistula-Signed-By"] != miner.hotkey:
        return Dispatch(miner, False, "reply not signed by this miner's hotkey",
                        latency_ms=latency_ms, status=200)
    if not verify_signature(
        reply_headers, response.content, expected_signed_for=headers["Epistula-Signed-By"]
    ):
        return Dispatch(miner, False, "reply signature invalid or not bound to this validator",
                        latency_ms=latency_ms, status=200)
    try:
        payload = SolutionPayload.model_validate_json(response.content)
    except Exception as exc:  # noqa: BLE001
        return Dispatch(miner, False, f"reply is not a SolutionPayload: {str(exc)[:160]}",
                        latency_ms=latency_ms, status=200)
    if payload.problem_id != request.problem_id:
        return Dispatch(miner, False, "reply echoed the wrong request id",
                        latency_ms=latency_ms, status=200)

    return Dispatch(miner, True, "authenticated", code=payload.code,
                    raw_response=payload.raw_response, latency_ms=latency_ms, status=200)


# --------------------------------------------------------------------------- #
# Grading — the validator's own Verifier against the HIDDEN tests
# --------------------------------------------------------------------------- #
class Grader:
    """Executors are built once per language and reused across the pool.

    Constructing the Docker executor shells out to ``docker info``; doing that
    once per miner would dominate the round.
    """

    def __init__(self, executor_kind: str):
        self._settings = Settings(_env_file=None, executor=executor_kind)
        self._verifiers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def verifier(self, language: str):
        with self._lock:
            if language not in self._verifiers:
                executor = get_executor(self._settings, language=language)
                self._verifiers[language] = Verifier(executor, self._settings)
            return self._verifiers[language]

    def verify(self, problem: MockProblem, result: Dispatch):
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
        return self.verifier(problem.language).verify(local, solution), solution


@dataclass
class MinerResult:
    miner: Miner
    accepted: bool
    detail: str
    passed: int = 0
    total: int = 0
    all_passed: bool = False
    payment: float = 0.0
    latency_ms: float = 0.0
    compile_error: Optional[str] = None
    code: str = ""


@dataclass
class RoundResult:
    problem: str
    skipped: bool = False
    skip_reason: str = ""
    results: list[MinerResult] = field(default_factory=list)
    pool_pass_rate: float = 0.0
    band: str = "unknown"


# --------------------------------------------------------------------------- #
async def run_round(
    client: httpx.AsyncClient,
    problem: MockProblem,
    pool: list[Miner],
    validator_kp,
    grader: Grader,
    *,
    challenge_id: str,
    deadline_s: float,
    dispatch_concurrency: int,
    verify_concurrency: int,
) -> RoundResult:
    """Dispatch one problem to the whole pool, then grade and pay the round."""
    # A language this host cannot grade is caught before any miner is bothered.
    try:
        grader.verifier(problem.language)
    except Exception as exc:  # noqa: BLE001
        return RoundResult(problem.name, skipped=True, skip_reason=str(exc))

    send_gate = asyncio.Semaphore(max(1, dispatch_concurrency))

    async def send(miner: Miner) -> Dispatch:
        async with send_gate:
            return await dispatch_one(
                client, miner, problem, validator_kp,
                challenge_id=challenge_id, deadline_s=deadline_s,
            )

    # I/O-bound fan-out: every miner is contacted at once, as the real validator
    # does. return_exceptions so one hostile miner cannot abort the round.
    raw = await asyncio.gather(*(send(m) for m in pool), return_exceptions=True)
    dispatches = [
        d if isinstance(d, Dispatch)
        else Dispatch(m, False, f"dispatch crashed: {d!r}"[:200])
        for m, d in zip(pool, raw)
    ]

    # Grading is sandbox-bound, so it gets its own, much smaller gate — the same
    # split the real validator makes between dispatch and verify concurrency.
    verify_gate = asyncio.Semaphore(max(1, verify_concurrency))
    failures: dict[int, MinerResult] = {}

    async def check(result: Dispatch) -> Optional[MinerOutcome]:
        if not result.ok:
            failures[result.miner.uid] = MinerResult(
                result.miner, False, result.detail, latency_ms=result.latency_ms
            )
            return None
        async with verify_gate:
            try:
                verification, solution = await asyncio.to_thread(
                    grader.verify, problem, result
                )
            except Exception as exc:  # noqa: BLE001 - one bad grade is not the round
                failures[result.miner.uid] = MinerResult(
                    result.miner, True, f"grading failed: {type(exc).__name__}: {exc}",
                    latency_ms=result.latency_ms,
                )
                return None
        return MinerOutcome(
            uid=result.miner.uid, hotkey=result.miner.hotkey, solution=solution,
            verification=verification, reward=verification.reward,
        )

    graded = await asyncio.gather(*(check(d) for d in dispatches))
    outcomes = [o for o in graded if o is not None]
    codes = {d.miner.uid: d.code for d in dispatches if d.ok}

    # Payments are computed over the WHOLE pool: the latency multiplier is
    # relative to the fastest correct responder, so it only means anything here.
    payments = compute_payments(outcomes)
    by_uid = {m.uid: m for m in pool}
    results = [
        MinerResult(
            miner=by_uid[o.uid], accepted=True, detail="graded",
            passed=o.verification.num_passed, total=o.verification.num_tests,
            all_passed=o.verification.all_passed,
            payment=payments.get(o.uid, 0.0),
            latency_ms=o.solution.latency_ms,
            compile_error=o.verification.compile_error,
            code=codes.get(o.uid, ""),
        )
        for o in outcomes
    ]
    results.extend(failures.values())
    results.sort(key=lambda r: (-r.payment, r.miner.uid))

    rate = pass_rate(outcomes)
    band = classify_band(rate, 0.35, 0.65).value if outcomes else "unknown"
    return RoundResult(problem.name, results=results, pool_pass_rate=rate, band=band)


# --------------------------------------------------------------------------- #
def report_round(round_result: RoundResult, verbose: bool) -> None:
    print(f"\n=== {round_result.problem} ===")
    if round_result.skipped:
        print(f"  SKIP  {round_result.skip_reason}")
        return
    for result in round_result.results:
        mark = "PASS" if result.all_passed else "FAIL"
        print(
            f"  [{mark}] {result.miner.label:<24} {result.miner.short_hotkey:<14} "
            f"{result.passed}/{result.total} hidden  pay={result.payment:.4f}  "
            f"{result.latency_ms / 1000:6.2f}s"
        )
        if not result.accepted or result.detail.startswith("grading failed"):
            print(f"          {result.detail}")
        elif result.compile_error:
            print(f"          {result.compile_error.splitlines()[0][:140]}")
        if verbose and result.code:
            for line in result.code.splitlines()[:12]:
                print(f"          | {line}")
    print(f"  pool pass-rate {round_result.pool_pass_rate:.0%}  band={round_result.band}")


async def run(args) -> int:
    problems = load_problems(args.problem)
    if not problems:
        print(f"no problems matched {args.problem!r} in {PROBLEMS_DIR}", file=sys.stderr)
        return 2

    validator_kp = keypair(args.validator_uri)
    default_hotkey = keypair(DEFAULT_MINER_URI).ss58_address
    try:
        pool = build_pool(args.miner, args.miners, args.url, default_hotkey)
    except (ValueError, OSError) as exc:
        print(f"bad miner pool: {exc}", file=sys.stderr)
        return 2
    if not pool:
        print("no miners; pass --miner host:port (repeatable) or --miners file",
              file=sys.stderr)
        return 2

    print(f"validator {validator_kp.ss58_address}")
    print(f"pool      {len(pool)} miner(s), executor={args.executor}, "
          f"{len(problems)} problem(s)")
    for miner in pool:
        print(f"          {miner.label:<24} {miner.short_hotkey}")

    grader = Grader(args.executor)
    rounds: list[RoundResult] = []
    async with httpx.AsyncClient() as client:
        for index, problem in enumerate(problems):
            result = await run_round(
                client, problem, pool, validator_kp, grader,
                challenge_id=f"{args.challenge_id}-{index}",
                deadline_s=args.deadline,
                dispatch_concurrency=args.dispatch_concurrency,
                verify_concurrency=args.verify_concurrency,
            )
            rounds.append(result)
            report_round(result, args.verbose)

    graded_rounds = [r for r in rounds if not r.skipped]
    skipped = len(rounds) - len(graded_rounds)

    totals = {m.uid: 0.0 for m in pool}
    solves = {m.uid: 0 for m in pool}
    for round_result in graded_rounds:
        for result in round_result.results:
            totals[result.miner.uid] += result.payment
            solves[result.miner.uid] += int(result.all_passed)

    print(f"\n=== leaderboard over {len(graded_rounds)} graded problem(s)"
          + (f", {skipped} skipped" if skipped else "") + " ===")
    grand_total = sum(totals.values())
    for rank, miner in enumerate(sorted(pool, key=lambda m: (-totals[m.uid], m.uid)), 1):
        share = totals[miner.uid] / grand_total if grand_total else 0.0
        print(
            f"  {rank}. {miner.label:<24} solved {solves[miner.uid]}/{len(graded_rounds)}"
            f"   payment {totals[miner.uid]:.4f}   weight share {share:6.1%}"
        )
    print("(the subnet pays only for a FULL hidden-suite pass; partial == zero)")

    if not graded_rounds:
        return 1
    return 0 if any(solves[m.uid] == len(graded_rounds) for m in pool) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--miner", action="append", default=[], metavar="HOST:PORT[=HOTKEY]",
                        help="a miner to dispatch to; repeat for a pool. HOTKEY may be "
                             "an ss58 address or a //Dev URI for local testing")
    parser.add_argument("--miners", metavar="FILE",
                        help="JSON list of {uid?, host, port, hotkey?} miners")
    parser.add_argument("--url", help="single-miner shorthand, e.g. http://127.0.0.1:8091")
    parser.add_argument("--problem", help="only problems whose name contains this")
    parser.add_argument("--validator-uri", default=VALIDATOR_URI,
                        help="dev URI for the harness validator key")
    parser.add_argument("--executor", default="subprocess", choices=("subprocess", "docker"),
                        help="sandbox used for grading; rust requires docker")
    parser.add_argument("--deadline", type=float, default=120.0,
                        help="seconds advertised to each miner (default: 120)")
    parser.add_argument("--dispatch-concurrency", type=int, default=64,
                        help="how many miners are contacted at once (default: 64)")
    parser.add_argument("--verify-concurrency", type=int, default=4,
                        help="how many sandboxes grade at once (default: 4)")
    parser.add_argument("--challenge-id", default="mock-challenge")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the code each miner returned")
    args = parser.parse_args()
    if not args.miner and not args.miners and not args.url:
        args.url = "http://127.0.0.1:8091"
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
