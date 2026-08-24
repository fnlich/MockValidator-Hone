"""Host the hone-subnet ``examples/custom_miner`` solver under this harness.

``run_local_miner.py --solver`` needs a zero-argument factory, but the custom
miner's ``VerifyingSolver`` takes a backend, so this module supplies the glue:

    # exercise the wire + self-verify loop with no browser at all
    python run_local_miner.py --solver bridges.custom_miner:stub

    # the real ChatGPT-over-CDP backend
    CUSTOM_MINER_DIR=/path/to/hone-subnet/examples/custom_miner \
    CHATGPT_PORTS=9222,9223 \
    python run_local_miner.py --solver bridges.custom_miner:chatgpt

``CUSTOM_MINER_DIR`` points at the custom_miner directory in your hone-subnet
checkout; it defaults to a sibling clone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent.parent.parent / "hone-subnet" / "examples" / "custom_miner"
CUSTOM_MINER_DIR = Path(os.environ.get("CUSTOM_MINER_DIR", _DEFAULT))

if not (CUSTOM_MINER_DIR / "solvers").is_dir():
    raise SystemExit(
        f"custom_miner not found at {CUSTOM_MINER_DIR}. Set CUSTOM_MINER_DIR to "
        "<hone-subnet>/examples/custom_miner"
    )
sys.path.insert(0, str(CUSTOM_MINER_DIR))

from solvers.verify import VerifyingSolver  # noqa: E402


def _tuning() -> dict:
    return dict(
        max_attempts=int(os.environ.get("SOLVER_MAX_ATTEMPTS", "3")),
        safety_margin_s=float(os.environ.get("SOLVER_SAFETY_MARGIN_S", "5")),
        max_budget_s=float(os.environ.get("SOLVER_MAX_BUDGET_S", "240")),
    )


def chatgpt() -> VerifyingSolver:
    """The real backend. Needs Chrome on CHATGPT_PORTS, logged in to ChatGPT.

    The pool attaches lazily on the first solve, so no startup hook is required
    here — which is exactly what lets the browser-backed solver run under this
    chain-free harness instead of only under run_chatgpt_miner.py.
    """
    from solvers.chatgpt_cdp import ChatGPTPool

    ports = [int(p) for p in os.environ.get("CHATGPT_PORTS", "9222").replace(",", " ").split()]
    pool = ChatGPTPool(
        ports,
        host=os.environ.get("CHATGPT_HOST", "127.0.0.1"),
        tabs_per_browser=int(os.environ.get("CHATGPT_TABS_PER_BROWSER", "2")),
    )
    return VerifyingSolver(pool, **_tuning())


def stub() -> VerifyingSolver:
    """A scripted backend: answers wrong first, then correct.

    Nothing here talks to a browser, so it proves the whole chain — signed
    dispatch, the self-verify loop, repair, signed reply, hidden-test grading —
    on a machine with no Chrome. If this passes and `chatgpt` does not, the
    problem is the browser, not the miner.
    """

    class _Chat:
        def __init__(self) -> None:
            self._n = -1

        async def send(self, text: str, timeout_s: float) -> str:
            self._n += 1
            if self._n == 0:
                # A plausible off-by-one, the kind a model really produces.
                return ("```python\ndef sum_of_digits(n):\n    s = 0\n"
                        "    while n > 9:\n        s += n % 10\n        n //= 10\n"
                        "    return s\n```")
            return ("```python\ndef sum_of_digits(n):\n    s = 0\n"
                    "    while n > 0:\n        s += n % 10\n        n //= 10\n"
                    "    return s\n```")

        async def close(self) -> None:
            return None

    class _Backend:
        async def open(self): return _Chat()
        async def aclose(self) -> None: return None
        def stats(self) -> dict: return {"stub": True}

    return VerifyingSolver(_Backend(), **_tuning())
