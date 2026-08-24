#!/usr/bin/env python3
"""Serve a miner locally so the mock validator has something to talk to.

A real miner refuses any caller that does not hold a validator permit in its
metagraph, and the harness's dev key holds none. This starts the reference
miner with a STUB metagraph that grants the harness's validator hotkey a permit,
so the full signed round trip works with no chain and no registration.

    python run_local_miner.py                 # trivial built-in solver
    python run_local_miner.py --solver mypkg.mymodule:MySolver

``--solver`` takes ``module:attribute`` naming either a Solver instance or a
zero-argument callable returning one. A Solver is anything with:

    async def solve_task(task, timeout_s) -> object with .code and .raw_response
    async def aclose() -> None

which is the same seam ``examples/custom_miner`` in the hone-subnet repo uses,
so a solver written against that runs here unchanged.

This is a TEST rig. It signs with a well-known dev key and trusts a fabricated
metagraph; never expose it to the internet or point it at a real wallet.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from mock_validator import DEFAULT_MINER_URI, VALIDATOR_URI, keypair


@dataclass
class _Answer:
    code: str
    raw_response: str = ""


class EchoSolver:
    """A deliberately naive solver, so the harness demonstrates both outcomes.

    It solves `sum_of_digits` correctly and gets `encode` subtly wrong, which is
    what makes a first run informative: you see a PASS and a FAIL side by side
    and can tell the harness is really grading rather than rubber-stamping.
    """

    async def solve_task(self, task, timeout_s: float) -> _Answer:
        if task.language == "rust":
            return _Answer(code="", raw_response="<this solver does not do rust>")
        if task.entrypoint == "sum_of_digits":
            return _Answer(code="def sum_of_digits(n):\n    return sum(int(c) for c in str(n))\n")
        if task.entrypoint == "encode":
            # Wrong on purpose: collapses non-adjacent runs of the same char.
            return _Answer(
                code=(
                    "def encode(s):\n"
                    "    out = {}\n"
                    "    for ch in s:\n"
                    "        out[ch] = out.get(ch, 0) + 1\n"
                    "    return [[k, v] for k, v in out.items()]\n"
                )
            )
        return _Answer(code="", raw_response="<unknown entrypoint>")

    async def aclose(self) -> None:
        return None


def load_solver(spec: str):
    """Resolve ``module:attribute`` to a Solver INSTANCE.

    The class case must be detected with ``inspect.isclass`` rather than by
    asking whether the target has ``solve_task``: a class carries its methods as
    attributes, so an attribute test reports "already an instance" for a class,
    the class is never constructed, and the miner ends up calling ``solve_task``
    unbound with the task as ``self``. That fails on every single request while
    looking like a bug in the user's solver.
    """
    if ":" not in spec:
        raise SystemExit("--solver must look like module:attribute")
    module_name, attribute = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import {module_name!r}: {exc}") from None
    try:
        target = getattr(module, attribute)
    except AttributeError:
        raise SystemExit(f"{module_name!r} has no attribute {attribute!r}") from None

    if inspect.isclass(target):
        solver = target()                    # a Solver class -> instantiate it
    elif callable(target) and not hasattr(target, "solve_task"):
        solver = target()                    # a factory returning a Solver
    else:
        solver = target                      # already a Solver instance

    for method in ("solve_task", "aclose"):
        if not callable(getattr(solver, method, None)):
            raise SystemExit(
                f"{spec} resolved to {solver!r}, which has no callable {method}(); "
                "a Solver needs async solve_task(task, timeout_s) and aclose()"
            )
    return solver


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--solver", help="module:attribute of your Solver")
    parser.add_argument("--miner-uri", default=DEFAULT_MINER_URI)
    parser.add_argument("--validator-uri", default=VALIDATOR_URI)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    import uvicorn
    from rlvr.neurons.demo_miner import DemoMiner, DemoMinerSettings, build_demo_miner_app

    solver = load_solver(args.solver) if args.solver else EchoSolver()
    miner_kp = keypair(args.miner_uri)
    validator_kp = keypair(args.validator_uri)

    class _Miner(DemoMiner):
        """DemoMiner with its answer source replaced; every check kept intact."""

        async def solve(self, request, timeout_s: float):
            from rlvr.protocol import SolutionPayload

            task = SimpleNamespace(
                problem_id=request.problem_id,
                language=request.language,
                statement=request.statement,
                entrypoint=request.entrypoint,
                public_examples=[c.model_dump(mode="json") for c in request.public_examples],
                deadline_s=request.deadline_s,
            )
            try:
                answer = await solver.solve_task(task, timeout_s)
                code, raw = answer.code, answer.raw_response
            except Exception as exc:  # noqa: BLE001 - a failed solve scores zero
                print(f"[local-miner] solver failed: {type(exc).__name__}: {exc}")
                code, raw = "", "<solver failed>"
            return SolutionPayload(problem_id=request.problem_id, code=code, raw_response=raw)

        async def aclose(self) -> None:
            await solver.aclose()

    settings = DemoMinerSettings(
        _env_file=None, miner_max_concurrent_requests=args.concurrency
    )
    # The fabricated metagraph: exactly one permitted validator, ours.
    metagraph: Any = SimpleNamespace(
        hotkeys=[validator_kp.ss58_address], validator_permit=[True], S=[1000.0]
    )
    miner = _Miner(
        settings, solver,
        wallet=SimpleNamespace(hotkey=miner_kp), subtensor=None, metagraph=metagraph,
    )

    print(f"[local-miner] hotkey  {miner_kp.ss58_address}")
    print(f"[local-miner] trusts  {validator_kp.ss58_address} (stub metagraph, permit=True)")
    print(f"[local-miner] serving http://{args.host}:{args.port}\n")
    uvicorn.run(build_demo_miner_app(miner), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
