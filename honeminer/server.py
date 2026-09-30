"""``serve``: the HTTP endpoint validators call with live offers.

``POST /solve`` takes a signed ``MinerTaskRequest`` and returns a ``MinerTaskResponse`` signed by our hotkey.
The request is admitted only if it is fresh, signed for us, not replayed, from an authorized validator
(registered, with a validator permit and enough stake), and for our slots and a supported task policy.
The answer comes from ``reply.answer_offer``, which uploads nothing unless the server would accept it.

``POST /v3/failure`` takes a validator's signed failure notice for a task we answered. Notices are only
archived (``runs/notices.jsonl``) and printed; they never feed an answer.

The request checks mirror hone-subnet's demo miner and reuse its code: ``verify_signature``,
``sign_message``, ``NonceCache`` and ``ServedTasks``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response  # module level: FastAPI resolves route annotations here
from rlvr.neurons.demo_miner import ServedTasks
from rlvr.policy import RELEASE_POLICY
from rlvr.protocol import NonceCache, sign_message, verify_signature
from rlvr.v3.api import FAILURE_NOTICE_MAX_BYTES, MinerFailureNotice, MinerTaskRequest, MinerTaskResponse

from honeminer.archive import RunArchive
from honeminer.config import ConfigError, Settings
from honeminer.pack import VERIFIER_POLICY
from honeminer.reply import ReplyError, SolveFn, answer_offer, offer_clock

log = logging.getLogger("honeminer.serve")
REPLY_MAX_BYTES = 16_384  # the validator reads at most this much (rlvr/neurons/live.py)


def hotkey_of(wallet: Any) -> str:
    """The ss58 address of a bittensor wallet or keypair; a plain string is its own id (rlvr's HMAC mode)."""

    if isinstance(wallet, str):
        return wallet
    for path in (("hotkey", "ss58_address"), ("ss58_address",)):
        value = wallet
        try:
            for name in path:
                value = getattr(value, name)
        except AttributeError:
            continue
        return str(value)
    return ""


def authorized(metagraph: Any, signed_by: str, *, min_stake: float, require_permit: bool) -> bool:
    """The demo miner's rule: a registered hotkey, with enough stake and (if required) a validator permit."""

    hotkeys = list(getattr(metagraph, "hotkeys", None) or [])
    if signed_by not in hotkeys:
        return False
    uid = hotkeys.index(signed_by)
    try:
        if min_stake > 0 and float(metagraph.S[uid]) < min_stake:
            return False
        if require_permit and not bool(metagraph.validator_permit[uid]):
            return False
    except (AttributeError, IndexError, TypeError, ValueError):
        return False
    return True


def _error(status: int, message: str) -> tuple[int, dict]:
    return status, {"error": message}


class OfferServer:
    """The request logic of ``serve`` (no sockets: the app and the tests call it directly)."""

    def __init__(self, settings: Settings, *, wallet: Any, metagraph: Any, http: httpx.AsyncClient,
                 solve_fn: SolveFn, origins: frozenset[str] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.settings, self.wallet, self.metagraph, self.http = settings, wallet, metagraph, http
        self.solve_fn = solve_fn
        self.origins = origins or frozenset(RELEASE_POLICY.v3_artifact_origins)
        self.now = now
        self.hotkey = hotkey_of(wallet)
        self.nonces = NonceCache(window_ms=8000)
        self.served = ServedTasks()
        self.slots = asyncio.Semaphore(settings.slots)
        self.runs = Path(settings.runs_dir)

    # -------------------------------------------------------------- checks shared by both endpoints

    def _admit(self, headers: Mapping[str, str], body: bytes, limit: int) -> tuple[int, dict] | None:
        if len(body) > limit:
            return _error(413, "request body too large")
        if not self.hotkey or not verify_signature(headers, body, expected_signed_for=self.hotkey):
            return _error(401, "invalid signature")
        if not self.nonces.check_and_add(headers.get("Epistula-Uuid", "")):
            return _error(409, "replayed request")
        if not authorized(self.metagraph, headers.get("Epistula-Signed-By", ""),
                          min_stake=self.settings.min_validator_stake,
                          require_permit=self.settings.require_validator_permit):
            return _error(403, "unauthorized signer")
        return None

    # -------------------------------------------------------------- /solve

    async def handle_solve(self, headers: Mapping[str, str], body: bytes) -> tuple[int, MinerTaskResponse | dict]:
        refused = self._admit(headers, body, self.settings.max_request_bytes)
        if refused:
            return refused
        try:
            task = MinerTaskRequest.model_validate_json(body)
        except ValueError:
            return _error(400, "invalid task request")
        if task.slots.submission.hotkey != self.hotkey:
            return _error(403, "task is assigned to another miner")
        identity = task.identity
        if (identity.execution_profile_id != RELEASE_POLICY.v3_execution_profile_id
                or identity.verifier_policy != VERIFIER_POLICY):
            return _error(422, "unsupported task policy")
        if task.expires_at <= self.now():
            return _error(410, "task expired")
        validator = headers.get("Epistula-Signed-By", "")
        try:
            clock = offer_clock(task, self.settings)
        except ValueError:
            return _error(410, "task expires before a reply could be sent")
        status, payload = await self._solve(task, clock, validator)
        self._record(task, validator, status, payload)
        return status, payload

    async def _solve(self, task: MinerTaskRequest, clock, validator: str) -> tuple[int, MinerTaskResponse | dict]:
        # Queue for a slot only while a useful solve could still start.
        wait_s = clock.agent_remaining() - self.settings.min_solve_s
        if wait_s <= 0:
            return _error(503, "not enough time left for a useful solve")
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=wait_s)
        except asyncio.TimeoutError:
            return _error(503, "busy: no solve slot freed in time")
        try:
            if clock.agent_remaining() < self.settings.min_solve_s:
                return _error(503, "not enough time left for a useful solve")
            archive = RunArchive.create(self.runs, f"offer-{task.challenge_id}")
            archive.write("offer.json", task.model_dump(mode="json"))
            try:
                reply = await asyncio.wait_for(
                    answer_offer(task, self.settings, self.http, origins=self.origins, hotkey=self.hotkey,
                                 solve_fn=self.solve_fn, workdir=archive.root, clock=clock),
                    timeout=max(1.0, clock.reply_remaining()))
            except ReplyError as exc:
                archive.write("error.txt", f"ReplyError: {exc}\n")
                return _error(422, f"no acceptable answer: {exc}"[:500])
            except asyncio.TimeoutError:
                archive.write("error.txt", "timed out before the reply deadline\n")
                return _error(504, "solve deadline exceeded")
            except Exception as exc:  # noqa: BLE001 - the validator only needs to know it failed
                archive.write("error.txt", f"{type(exc).__name__}: {exc}\n")
                log.warning("offer %s failed: %s", task.challenge_id, type(exc).__name__)
                return _error(500, "solve failed")
            archive.write("reply.json", {"response": reply.response.model_dump(mode="json"),
                                         "seconds": reply.seconds, "outcome": reply.solve.outcome,
                                         "rank": reply.solve.rank.name.lower()})
            self.served.add(validator, task.challenge_id, task.task_id, task.slots.submission.uid,
                            task.slots.submission.hotkey, time.monotonic())
            return 200, reply.response
        finally:
            self.slots.release()

    def _record(self, task: MinerTaskRequest, validator: str, status: int, payload) -> None:
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "challenge_id": task.challenge_id,
                "task_id": task.task_id, "validator": validator, "status": status,
                "error": payload.get("error") if isinstance(payload, dict) else None}
        self._append("offers.jsonl", line)

    def _append(self, name: str, line: dict) -> None:
        try:
            self.runs.mkdir(parents=True, exist_ok=True)
            with (self.runs / name).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError as exc:  # a full disk must not fail the reply
            log.warning("could not write %s: %s", name, exc)

    # -------------------------------------------------------------- /v3/failure

    async def handle_failure_notice(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict]:
        refused = self._admit(headers, body, min(self.settings.max_request_bytes, FAILURE_NOTICE_MAX_BYTES))
        if refused:
            return refused
        try:
            notice = MinerFailureNotice.model_validate_json(body)
        except ValueError:
            return _error(400, "invalid failure notice")
        if notice.hotkey != self.hotkey:
            return _error(403, "notice is for another miner")
        validator = headers.get("Epistula-Signed-By", "")
        seen = self.served.classify(validator, notice.challenge_id, notice.task_id, notice.uid, notice.hotkey,
                                    time.monotonic())
        if seen == "unknown":
            return _error(404, "no such task from this validator")
        if seen == "new":
            reason = notice.failure.reason_code
            line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "validator": validator,
                    "challenge_id": notice.challenge_id, "task_id": notice.task_id,
                    "reason": getattr(reason, "value", reason), "failed_check": notice.failure.failed_check}
            self._append("notices.jsonl", line)
            log.info("failure notice: %s challenge %s", line["reason"], notice.challenge_id[:32])
        return 200, {"accepted": True}


# ------------------------------------------------------------------ HTTP


def build_app(server: OfferServer, *, sync_metagraph: Callable[[], None] | None = None):
    """The FastAPI surface validators call. 200 replies to /solve are signed for the caller."""

    app = FastAPI(title="honeminer")
    synced = {"at": time.monotonic()}
    sync_lock = asyncio.Lock()

    async def maybe_sync() -> None:
        if sync_metagraph is None or time.monotonic() - synced["at"] < server.settings.metagraph_sync_s:
            return
        async with sync_lock:
            if time.monotonic() - synced["at"] < server.settings.metagraph_sync_s:
                return
            try:
                await asyncio.to_thread(sync_metagraph)
            except Exception as exc:  # noqa: BLE001 - keep the last known view
                log.warning("metagraph refresh failed (%s); using the cached view", type(exc).__name__)
            finally:
                synced["at"] = time.monotonic()  # back off after failures too

    async def read_bounded(request: Request, limit: int) -> bytes | None:
        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return None
        if declared < 0 or declared > limit:
            return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return None
        return bytes(body)

    def json_response(status: int, payload, signed_for: str | None = None) -> Response:
        if isinstance(payload, MinerTaskResponse):
            content = payload.model_dump_json().encode("utf-8")
        else:
            content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = Response(content=content, status_code=status, media_type="application/json")
        if status == 200 and signed_for is not None:
            if len(content) > REPLY_MAX_BYTES:
                log.error("reply is %d bytes; the validator reads at most %d", len(content), REPLY_MAX_BYTES)
            # Signed last, right before sending: the validator allows 8 s of timestamp skew.
            response.headers.update(sign_message(server.wallet, content, signed_for=signed_for))
        return response

    @app.post("/solve")
    async def solve_endpoint(request: Request) -> Response:
        await maybe_sync()
        body = await read_bounded(request, server.settings.max_request_bytes)
        if body is None:
            return json_response(413, {"error": "request body too large"})
        status, payload = await server.handle_solve(request.headers, body)
        return json_response(status, payload, signed_for=request.headers.get("Epistula-Signed-By", ""))

    @app.post("/v3/failure")
    async def failure_endpoint(request: Request) -> Response:
        await maybe_sync()
        body = await read_bounded(request, min(server.settings.max_request_bytes, FAILURE_NOTICE_MAX_BYTES))
        if body is None:
            return json_response(413, {"error": "request body too large"})
        status, payload = await server.handle_failure_notice(request.headers, body)
        return json_response(status, payload)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "hotkey": server.hotkey, "slots": server.settings.slots}

    return app


def claude_solver(settings: Settings) -> SolveFn:
    """The production solve: sandboxed Claude CLI on the offer's clock, with the offer's work-log header."""

    from honeminer.solve import solve_with_claude

    def solve_fn(pack, clock, spec):
        return solve_with_claude(pack, settings, clock=clock, work_log=spec, grade=False)

    return solve_fn


def run_server(settings: Settings) -> None:
    """Check the host and the chain, advertise the axon, then serve until stopped."""

    from honeminer.doctor import host_checks, report

    settings.require_live()
    text, ok = report(host_checks(settings))
    if not ok:
        raise ConfigError("host checks failed; run `python -m honeminer doctor`:\n" + text)
    try:
        import bittensor as bt  # type: ignore[import-not-found]
        import uvicorn
    except ImportError as exc:
        raise ConfigError(f"serve needs the chain extras: pip install -e '../hone-subnet[miner,chain]' ({exc})") \
            from None

    wallet = bt.Wallet(name=settings.wallet_name, hotkey=settings.wallet_hotkey)
    subtensor = bt.Subtensor(network=settings.subtensor_network)
    if not subtensor.is_hotkey_registered(netuid=settings.netuid, hotkey_ss58=wallet.hotkey.ss58_address):
        raise ConfigError(f"hotkey {wallet.hotkey.ss58_address} is not registered on netuid {settings.netuid}")
    metagraph = subtensor.metagraph(settings.netuid)
    axon_kwargs: dict[str, Any] = {"wallet": wallet, "port": settings.axon_port}
    if settings.axon_external_ip:
        axon_kwargs["external_ip"] = settings.axon_external_ip
    bt.Axon(**axon_kwargs).serve(netuid=settings.netuid, subtensor=subtensor)

    http = httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=300, write=300, pool=30))
    server = OfferServer(settings, wallet=wallet, metagraph=metagraph, http=http, solve_fn=claude_solver(settings))
    app = build_app(server, sync_metagraph=lambda: metagraph.sync(subtensor=subtensor))
    print(f"honeminer serving netuid={settings.netuid} hotkey={server.hotkey} port={settings.axon_port} "
          f"model={settings.model} slots={settings.slots}", flush=True)
    uvicorn.run(app, host=settings.axon_host, port=settings.axon_port, log_level="info")
