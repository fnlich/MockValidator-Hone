"""Rehearse one whole round locally: a strict problem server, rlvr's real validator, and our miner.

No network: the problem server and the object store are an ``httpx.MockTransport`` on a fake https origin.
The validator side is rlvr's own ``evaluate_round`` (lease checks, dispatch, quorum, commit/reveal checks,
submission download, grading, and then ``compute_round_payments``).

The problem server is not public, so this one implements its published contract, stricter than anything
visible in hone-subnet: a reply's refs must match its slots (``slot_mismatch``), both uploads must exist and
match the signed sha256 and size (``artifact_invalid``), and the work log must parse and be bound to this
task, this challenge, the miner's hotkey and the uploaded answer (``trajectory_invalid``).

Six miners take part, so the release quorum of 4 signed responses holds even when honeminer fails to reply:
honeminer, the pack's reference answer, a slower copy of it (speed decides the payment between passes), an
empty answer, a broken answer, and one that never answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rlvr.policy import RELEASE_POLICY
from rlvr.v3.api import (
    EPISTULA_HEADERS,
    ChallengeCommitRequest,
    ChallengeFeedbackResponse,
    CommitRevealResponse,
    LeaseRequest,
    LeaseResponse,
    MinerSubmission,
    MinerTaskRequest,
    MinerTaskResponse,
    derive_miner_request_id,
)
from rlvr.v3.artifacts import MinerSlotSet, UploadSlot
from rlvr.v3.miner import upload_miner_result
from rlvr.v3.trajectory import parse_trajectory

from honeminer.tasks import TaskPack
from honeminer.trajectory import Header, build_within

ORIGIN = "https://rehearsal.invalid"
ORIGINS = frozenset({"https://rehearsal.invalid:443"})
UNSIGNED = "rehearsal-unsigned"  # in-process replies carry no Epistula signature; `serve` will be rehearsed over HTTP
HONEMINER, REFERENCE, COPYCAT, EMPTY, BROKEN, SILENT = "honeminer", "reference", "copycat", "empty", "broken", "silent"
MINERS = ((1, HONEMINER), (2, REFERENCE), (3, COPYCAT), (4, EMPTY), (5, BROKEN), (6, SILENT))
COPYCAT_DELAY_S = 0.5
BROKEN_ANSWER = b"this is not a patch\n"


class _Body(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content


def streamed(content: bytes) -> httpx.Response:
    """A 200 whose body is a real byte stream (rlvr's downloader reads raw chunks, with Content-Length)."""

    return httpx.Response(200, headers={"Content-Length": str(len(content))}, stream=_Body(content))


def hotkey_for(name: str) -> str:
    return f"rehearsal-{name}"


@dataclass
class Upload:
    slot: UploadSlot
    data: bytes | None = None


@dataclass(frozen=True)
class Verdict:
    uid: int
    hotkey: str
    granted: bool
    reason: str  # "" for a grant, else slot_mismatch | artifact_invalid | trajectory_invalid
    detail: str


class RehearsalServer:
    """The problem server and object store, as one MockTransport handler."""

    def __init__(self, pack: TaskPack, *, lease_s: int, trajectory_max_bytes: int,
                 submission_max_bytes: int | None = None, put_faults: int = 0,
                 now: Callable[[], float] = time.time) -> None:
        self.pack, self.lease_s, self.now = pack, lease_s, now
        self.trajectory_max_bytes = trajectory_max_bytes
        self.submission_max_bytes = submission_max_bytes or (
            RELEASE_POLICY.v3_script_bytes if pack.is_terminal else RELEASE_POLICY.v3_patch_bytes)
        self.put_faults = put_faults  # the first N uploads answer 503 (the client must retry)
        self.challenge_id = f"rehearsal-{uuid.uuid4().hex[:16]}"
        self.lease: LeaseResponse | None = None
        self.uploads: dict[str, Upload] = {}
        self.verdicts: dict[int, Verdict] = {}
        self.feedback: list[dict] = []
        self.log: list[str] = []

    # -------------------------------------------------------------- lease

    def _slot(self, uid: int, hotkey: str, which: str, expires_at: int) -> UploadSlot:
        if which == "trajectory":
            role, fmt, limit = "trajectory", "trajectory_v1", self.trajectory_max_bytes
        elif self.pack.is_terminal:
            role, fmt, limit = "script", "bash_script_v1", self.submission_max_bytes
        else:
            role, fmt, limit = "patch", "unified_diff_v1", self.submission_max_bytes
        upload_id = f"{self.challenge_id}-{uid}-{which}"
        slot = UploadSlot(challenge_id=self.challenge_id, task_id=self.pack.task_id, uid=uid, hotkey=hotkey,
                          artifact_role=role, artifact_format=fmt, upload_id=upload_id,
                          upload_url=f"{ORIGIN}/upload/{upload_id}", expires_at=expires_at, max_bytes=limit)
        self.uploads[upload_id] = Upload(slot)
        return slot

    def _lease(self, body: bytes) -> LeaseResponse:
        request = LeaseRequest.model_validate_json(body)
        issued = int(self.now())
        expires_at = issued + self.lease_s
        pool = [MinerSlotSet(submission=self._slot(c.uid, c.hotkey, "submission", expires_at),
                             trajectory=self._slot(c.uid, c.hotkey, "trajectory", expires_at))
                for c in request.candidates[:RELEASE_POLICY.v3_miners_per_task]]
        role = self.pack.environment_role
        self.lease = LeaseResponse(
            protocol_version=3, challenge_id=self.challenge_id, task_id=self.pack.task_id,
            identity=self.pack.identity, workspace=self.pack.refs[role], workspace_url=f"{ORIGIN}/workspace",
            verifier=self.pack.refs["verifier"], issued_at=issued, expires_at=expires_at, slot_pool=pool,
            commit_min_signed_responses=RELEASE_POLICY.v3_commit_quorum,
        )
        return self.lease

    # -------------------------------------------------------------- storage

    def _put(self, upload_id: str, request: httpx.Request, body: bytes) -> httpx.Response:
        upload = self.uploads.get(upload_id)
        if upload is None:
            return httpx.Response(404)
        if self.put_faults > 0:
            self.put_faults -= 1
            self.log.append(f"PUT {upload_id}: injected 503")
            return httpx.Response(503)
        if upload.data is not None:
            return httpx.Response(412)  # If-None-Match: * — objects are write-once
        if request.headers.get("if-none-match") != "*":
            return httpx.Response(428)
        if self.now() > upload.slot.expires_at:
            return httpx.Response(403)
        if len(body) > upload.slot.max_bytes or int(request.headers.get("content-length", -1)) != len(body):
            return httpx.Response(413)
        upload.data = body
        return httpx.Response(201)

    # -------------------------------------------------------------- commit

    def _check(self, submission: MinerSubmission) -> Verdict:
        def fail(reason: str, detail: str) -> Verdict:
            return Verdict(submission.uid, submission.hotkey, False, reason, detail)

        if not submission.response_body:
            return fail("artifact_invalid", f"no reply: {submission.error}")
        slots = next((s for s in self.lease.slot_pool if s.submission.uid == submission.uid), None)
        if slots is None or slots.submission.hotkey != submission.hotkey:
            return fail("slot_mismatch", "no slots were issued to this miner")
        if submission.request_id != derive_miner_request_id(self.challenge_id, submission.uid, submission.hotkey):
            return fail("slot_mismatch", "request id does not match this challenge and miner")
        try:
            response = MinerTaskResponse.model_validate_json(submission.response_body)
        except ValueError as exc:
            return fail("slot_mismatch", f"reply does not parse: {exc}")
        if (response.challenge_id, response.task_id) != (self.challenge_id, self.pack.task_id):
            return fail("slot_mismatch", "reply names another challenge or task")
        for ref, slot in ((response.submission, slots.submission), (response.trajectory, slots.trajectory)):
            if (ref.upload_id, ref.artifact_role, ref.artifact_format) != (
                    slot.upload_id, slot.artifact_role, slot.artifact_format):
                return fail("slot_mismatch", f"{slot.artifact_role} ref does not match its slot")
        stored = {}
        for ref in (response.submission, response.trajectory):
            data = self.uploads[ref.upload_id].data
            if data is None:
                return fail("artifact_invalid", f"{ref.artifact_role} was never uploaded")
            if hashlib.sha256(data).hexdigest() != ref.sha256 or len(data) != ref.size_bytes:
                return fail("artifact_invalid", f"stored {ref.artifact_role} differs from the signed ref")
            stored[ref.artifact_role] = data
        log_bytes = stored["trajectory"]
        if len(log_bytes) > slots.trajectory.max_bytes:
            return fail("trajectory_invalid", "work log is over the slot's max_bytes")
        try:
            log = parse_trajectory(log_bytes)
        except ValueError as exc:
            return fail("trajectory_invalid", f"work log does not parse: {exc}")
        answer_sha = response.submission.sha256
        bound = (log.task_id, log.challenge_id, log.miner_hotkey, log.submission_sha256)
        if bound != (self.pack.task_id, self.challenge_id, submission.hotkey, answer_sha):
            names = ("task_id", "challenge_id", "miner_hotkey", "submission_sha256")
            wrong = [n for n, a, b in zip(names, bound, (self.pack.task_id, self.challenge_id, submission.hotkey,
                                                          answer_sha), strict=True) if a != b]
            return fail("trajectory_invalid", f"work log is not bound to this round: {', '.join(wrong)}")
        return Verdict(submission.uid, submission.hotkey, True, "", f"{len(log.events)} log events")

    def _commit(self, body: bytes) -> CommitRevealResponse:
        request = ChallengeCommitRequest.model_validate_json(body)
        grants, failures = [], []
        for submission in request.submissions:
            verdict = self._check(submission)
            self.verdicts[submission.uid] = verdict
            if not verdict.granted:
                failures.append({"uid": verdict.uid, "hotkey": verdict.hotkey, "reason": verdict.reason})
                continue
            ref = MinerTaskResponse.model_validate_json(submission.response_body).submission
            grants.append({"uid": verdict.uid, "hotkey": verdict.hotkey, "upload_id": ref.upload_id,
                           "sha256": ref.sha256, "size_bytes": ref.size_bytes, "format": ref.artifact_format,
                           "read_url": f"{ORIGIN}/read/{ref.upload_id}"})
        return CommitRevealResponse(
            protocol_version=3, challenge_id=self.challenge_id, task_id=self.pack.task_id,
            verifier=self.pack.refs["verifier"], verifier_policy=self.pack.identity.verifier_policy,
            verifier_url=f"{ORIGIN}/verifier", grading_expires_at=int(self.now()) + 3600,
            submission_grants=grants, artifact_failures=failures,
        )

    # -------------------------------------------------------------- transport

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = await request.aread()
        if method == "POST" and path == "/v3/challenges/lease":
            return httpx.Response(200, content=self._lease(body).model_dump_json())
        if method == "POST" and path == "/v3/challenges/commit":
            return httpx.Response(200, content=self._commit(body).model_dump_json())
        if method == "POST" and path == "/v3/challenges/feedback":
            self.feedback.append(json.loads(body))
            return httpx.Response(200, content=ChallengeFeedbackResponse(
                protocol_version=3, challenge_id=self.challenge_id, task_id=self.pack.task_id).model_dump_json())
        if method == "GET" and path == "/workspace":
            return streamed((self.pack.root / f"{self.pack.environment_role}.tar.zst").read_bytes())
        if method == "GET" and path == "/verifier":
            return streamed((self.pack.root / "verifier.tar.zst").read_bytes())
        if method == "PUT" and path.startswith("/upload/"):
            return self._put(path.rsplit("/", 1)[1], request, body)
        if method == "GET" and path.startswith("/read/"):
            upload = self.uploads.get(path.rsplit("/", 1)[1])
            if upload is None or upload.data is None:
                return httpx.Response(404)
            return streamed(upload.data)
        return httpx.Response(404)


# ------------------------------------------------------------------ miners


def envelope(task: MinerTaskRequest, uid: int, hotkey: str, response: MinerTaskResponse | None, error: str,
             latency_ms: int) -> MinerSubmission:
    """The envelope the validator commits (the in-process stand-in for a signed HTTP reply)."""

    signed = response is not None
    return MinerSubmission(
        uid=uid, hotkey=hotkey, request_id=derive_miner_request_id(task.challenge_id, uid, hotkey),
        response_body=response.model_dump_json() if signed else "",
        response_headers={name: UNSIGNED for name in EPISTULA_HEADERS} if signed else {},
        error="" if signed else (error[:4096] or "miner unavailable"), latency_ms=latency_ms,
    )


LogFn = Callable[[MinerTaskRequest, str, bytes], bytes]


def honest_log(task: MinerTaskRequest, hotkey: str, content: bytes) -> bytes:
    header = Header(task_id=task.task_id, challenge_id=task.challenge_id, miner_hotkey=hotkey,
                    model_name="stand-in", harness_name="honeminer-rehearsal")
    return build_within([], header, content, task.slots.trajectory.max_bytes, timeout_s=5).data


@dataclass
class StandIn:
    """A scripted miner: uploads a fixed answer with a valid log (or the log ``log_fn`` makes)."""

    uid: int
    hotkey: str
    content: bytes
    http: httpx.AsyncClient
    log_fn: LogFn = honest_log
    delay_s: float = 0.0

    async def solve_v3(self, task: MinerTaskRequest) -> tuple[MinerSubmission, MinerTaskResponse | None]:
        started = time.monotonic()
        await asyncio.sleep(self.delay_s)
        response = await upload_miner_result(self.http, task, self.content, self.log_fn(task, self.hotkey,
                                                                                        self.content),
                                             allowed_origins=ORIGINS)
        return envelope(task, self.uid, self.hotkey, response, "", _ms(started)), response


AnswerFn = Callable[[MinerTaskRequest, httpx.AsyncClient], Awaitable[object]]  # -> honeminer.reply.Reply


@dataclass
class Honeminer:
    """honeminer as a validator's solver: ``answer`` is ``reply.answer_offer`` bound to settings."""

    uid: int
    hotkey: str
    http: httpx.AsyncClient
    answer: AnswerFn
    task: MinerTaskRequest | None = None
    reply: object | None = None
    error: str = ""

    async def solve_v3(self, task: MinerTaskRequest) -> tuple[MinerSubmission, MinerTaskResponse | None]:
        self.task = task
        started = time.monotonic()
        try:
            self.reply = await self.answer(task, self.http)
        except Exception as exc:  # noqa: BLE001 - reported as an unavailable reply, exactly as a validator sees it
            self.error = f"{type(exc).__name__}: {exc}"
            return envelope(task, self.uid, self.hotkey, None, self.error, _ms(started)), None
        response = self.reply.response
        return envelope(task, self.uid, self.hotkey, response, "", _ms(started)), response


def answer_with(settings, solve_fn, workdir: Path) -> AnswerFn:
    """honeminer's answer for the rehearsal: ``reply.answer_offer`` with this settings and solve function."""

    from honeminer.reply import answer_offer

    async def answer(task: MinerTaskRequest, http: httpx.AsyncClient):
        return await answer_offer(task, settings, http, origins=ORIGINS, hotkey=hotkey_for(HONEMINER),
                                  solve_fn=solve_fn, workdir=workdir)

    return answer


def reference_solve(content: bytes, settings):
    """A solve function that ships a known answer without a model: rehearses the plumbing for free."""

    from honeminer.answer import Rank
    from honeminer.solve import SolveResult
    from honeminer.trajectory import build_within as build

    def solve_fn(pack, clock, spec):
        built = build([], spec.header, content, spec.max_bytes,
                      timeout_s=clock.work_log_timeout(settings.trajectory_build_s))
        return SolveResult(content, Rank.CHECKED, "reference", 0, None, trajectory=built.data)

    return solve_fn


def _ms(started: float) -> int:
    return max(1, int((time.monotonic() - started) * 1000))


# ------------------------------------------------------------------ the round


@dataclass
class Rehearsal:
    server: RehearsalServer
    result: object  # rlvr RoundResult
    payments: dict[int, float]
    honeminer: Honeminer
    rows: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.result.status != "completed":
            return 2
        return 0 if self.payments.get(self.honeminer.uid, 0.0) > 0 else 1

    def to_json(self) -> dict:
        return {"challenge_id": self.server.challenge_id, "task_id": self.server.pack.task_id,
                "pack": self.server.pack.name, "round": {"status": self.result.status, "reason": self.result.reason},
                "miners": self.rows, "server_log": self.server.log,
                "note": "in-process replies are unsigned; Epistula signing is rehearsed with serve"}

    def render(self) -> str:
        lines = [f"round {self.result.status}" + (f": {self.result.reason}" if self.result.reason else "")]
        for row in self.rows:
            verdict = "grant" if row["commit"] == "grant" else f"failure ({row['commit']})"
            lines.append(f"  {row['miner']:<10} reply={row['reply']:<5} commit={verdict:<30} "
                         f"grade={row['grade']:<9} latency={row['latency_ms']}ms payment={row['payment']:.4f}")
            if row.get("detail"):
                lines.append(f"  {'':<10} {row['detail']}")
        return "\n".join(lines)


def rehearsal_policy(policy):
    """The validator's round policy, pointed at the rehearsal origin (5 miners, release quorum 4)."""

    return dataclasses.replace(policy, artifact_origins=ORIGINS, dispatch_concurrency=len(MINERS))


async def rehearse(pack: TaskPack, *, answer: AnswerFn, policy, workdir: Path, lease_s: int,
                   trajectory_max_bytes: int, put_faults: int = 0,
                   stand_in_logs: dict[str, LogFn] | None = None) -> Rehearsal:
    from rlvr.v3.client import V3ProblemServerClient
    from rlvr.v3.round import compute_round_payments, evaluate_round

    server = RehearsalServer(pack, lease_s=lease_s, trajectory_max_bytes=trajectory_max_bytes, put_faults=put_faults)
    reference = pack.reference() or b""
    answers = {REFERENCE: reference, COPYCAT: reference, EMPTY: b"", BROKEN: BROKEN_ANSWER}
    logs = stand_in_logs or {}
    workdir = Path(workdir)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handle)) as http:
        ours = Honeminer(MINERS[0][0], hotkey_for(HONEMINER), http, answer)
        solvers = [ours] + [StandIn(uid, hotkey_for(name), answers[name], http, logs.get(name, honest_log),
                                    COPYCAT_DELAY_S if name == COPYCAT else 0.0)
                            for uid, name in MINERS if name in answers]
        client = V3ProblemServerClient(ORIGIN, "rehearsal-validator", http, retries=1)
        result = await evaluate_round(client, http, solvers, rehearsal_policy(policy),
                                      cache_dir=workdir / "cache", work_dir=workdir / "grading",
                                      candidates=[(uid, hotkey_for(name)) for uid, name in MINERS])
    payments = compute_round_payments(result, speed_half_life_ms=RELEASE_POLICY.payment_speed_half_life_ms,
                                      speed_floor=RELEASE_POLICY.payment_speed_floor)
    rehearsal = Rehearsal(server, result, payments, ours)
    evaluations = {e.uid: e for e in result.evaluations}
    for uid, name in MINERS:
        verdict = server.verdicts.get(uid)
        evaluation = evaluations.get(uid)
        detail = ours.error if name == HONEMINER and ours.error else (
            verdict.detail if verdict and not verdict.granted else "")
        if evaluation is not None and evaluation.result.failed_check:
            detail = (detail + " " if detail else "") + evaluation.result.failed_check.splitlines()[0]
        rehearsal.rows.append({
            "miner": name, "uid": uid,
            "reply": "none" if verdict is None or verdict.detail.startswith("no reply") else "sent",
            "commit": "grant" if verdict and verdict.granted else (verdict.reason if verdict else "not committed"),
            "grade": evaluation.result.status if evaluation else "-",
            "latency_ms": evaluation.latency_ms if evaluation else 0,
            "payment": payments.get(uid, 0.0), "detail": detail,
        })
    return rehearsal
