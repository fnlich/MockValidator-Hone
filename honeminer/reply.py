"""Answer one live offer: workspace -> solve -> work log -> uploads -> the reply the validator signs for.

This is the miner side of a round, shared by ``rehearse`` (in process) and ``serve`` (over HTTP). Everything
the validator and the problem server will check is checked here first with rlvr's own code, so a reply that
leaves this module is one they accept: the refs match the slots (``validate_miner_response``), both artifacts
fit their slots, and the work log parses and is bound to this challenge, this hotkey and the uploaded answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from rlvr.v3.api import MinerTaskRequest, MinerTaskResponse, validate_miner_response
from rlvr.v3.download import download_artifact
from rlvr.v3.miner import upload_miner_result
from rlvr.v3.trajectory import parse_trajectory

from honeminer.clock import Clock
from honeminer.config import Settings
from honeminer.solve import SolveResult, WorkLogSpec
from honeminer.tasks import WORKSPACE_LIMITS, TaskPack, offer_pack
from honeminer.trajectory import Header


class ReplyError(RuntimeError):
    """No acceptable reply can be sent for this offer (nothing was uploaded)."""


SolveFn = Callable[[TaskPack, Clock, WorkLogSpec], SolveResult]


@dataclass(frozen=True)
class Reply:
    response: MinerTaskResponse
    submission: bytes
    trajectory: bytes
    solve: SolveResult
    seconds: dict[str, float]


def offer_clock(task: MinerTaskRequest, settings: Settings) -> Clock:
    """The offer's deadlines: its expiry, both slots' expiries and the task budget, whichever is first."""

    return Clock.for_task(settings, expires_at=task.expires_at,
                          slot_expiries=(task.slots.submission.expires_at, task.slots.trajectory.expires_at))


def check_artifacts(task: MinerTaskRequest, hotkey: str, submission: bytes, trajectory: bytes | None,
                    error: str = "") -> None:
    """Everything the server checks at commit, before anything is uploaded."""

    if trajectory is None:
        raise ReplyError(f"no work log: {error or 'the work log is off'}")
    if len(submission) > task.slots.submission.max_bytes:
        raise ReplyError(f"the answer is {len(submission)} bytes; the slot allows {task.slots.submission.max_bytes}")
    if len(trajectory) > task.slots.trajectory.max_bytes:
        raise ReplyError(f"the work log is {len(trajectory)} bytes; the slot allows {task.slots.trajectory.max_bytes}")
    try:
        log = parse_trajectory(trajectory)
    except ValueError as exc:
        raise ReplyError(f"the work log is invalid: {exc}") from None
    expected = (task.task_id, task.challenge_id, hotkey, hashlib.sha256(submission).hexdigest())
    if (log.task_id, log.challenge_id, log.miner_hotkey, log.submission_sha256) != expected:
        raise ReplyError("the work log is not bound to this offer, hotkey and answer")


async def answer_offer(task: MinerTaskRequest, settings: Settings, http: httpx.AsyncClient, *,
                       origins: frozenset[str], hotkey: str, solve_fn: SolveFn, workdir: Path,
                       clock: Clock | None = None) -> Reply:
    """Solve an offer and upload both artifacts; raises ``ReplyError`` (nothing uploaded) when it cannot."""

    if type(task) is not MinerTaskRequest:
        raise TypeError("task must be a validated miner request")
    if task.slots.submission.hotkey != hotkey:
        raise ReplyError("the offer's slots are for another hotkey")
    if not settings.trajectory:
        raise ReplyError("HONEMINER_TRAJECTORY is off; a reply needs a work log")
    clock = clock or offer_clock(task, settings)
    seconds: dict[str, float] = {}
    started = time.monotonic()

    root = Path(workdir) / f"offer-{task.task_id[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    role = task.workspace.artifact_role
    await download_artifact(http, task.workspace_url, task.workspace, root / f"{role}.tar.zst", WORKSPACE_LIMITS,
                            allowed_origins=origins)
    pack = offer_pack(task, root)
    seconds["download"] = round(time.monotonic() - started, 3)

    limit = task.slots.trajectory.max_bytes
    if settings.trajectory_max_bytes:
        limit = min(limit, settings.trajectory_max_bytes)
    spec = WorkLogSpec(Header(task_id=task.task_id, challenge_id=task.challenge_id, miner_hotkey=hotkey,
                              model_name=settings.model), limit)
    result = await asyncio.to_thread(solve_fn, pack, clock, spec)
    seconds["solve"] = round(time.monotonic() - started - seconds["download"], 3)

    submission = result.content
    check_artifacts(task, hotkey, submission, result.trajectory, result.trajectory_error)
    uploading = time.monotonic()
    response = await upload_miner_result(http, task, submission, result.trajectory, allowed_origins=origins)
    validate_miner_response(task, response)
    seconds["upload"] = round(time.monotonic() - uploading, 3)
    return Reply(response, submission, result.trajectory, result, seconds)
