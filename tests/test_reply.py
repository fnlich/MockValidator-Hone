import asyncio
import hashlib
import time

import httpx
import pytest
from rlvr.v3.api import MinerTaskRequest, validate_miner_response
from rlvr.v3.artifacts import MinerSlotSet, UploadSlot
from rlvr.v3.download import ArtifactDownloadError
from rlvr.v3.trajectory import parse_trajectory

from honeminer.answer import Rank
from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_pack, load_recipe
from honeminer.reply import ReplyError, answer_offer, offer_clock
from honeminer.solve import SolveResult
from honeminer.tasks import load_pack
from honeminer.trajectory import Header, build_within
from tests.test_pack import host_runner

ORIGIN = "https://store.invalid"
ORIGINS = frozenset({"https://store.invalid:443"})
HOTKEY = "hk-miner"
ANSWER = b"diff --git a/stats.py b/stats.py\n"


@pytest.fixture(scope="module")
def stats_pack(tmp_path_factory):
    out = tmp_path_factory.mktemp("packs") / "python-stats"
    return load_pack(build_pack(load_recipe(RECIPES_DIR / "python-stats"), out, host_runner))


def offer(pack, *, trajectory_max=1 << 20, submission_max=1 << 20, expires_in=1500, slot_expires_in=None):
    expires = int(time.time()) + expires_in
    slot_expires = int(time.time()) + (slot_expires_in or expires_in)

    def slot(role, fmt, limit):
        return UploadSlot(challenge_id="challenge-1", task_id=pack.task_id, uid=7, hotkey=HOTKEY, artifact_role=role,
                          artifact_format=fmt, upload_id=f"up-{role}", upload_url=f"{ORIGIN}/{role}",
                          expires_at=slot_expires, max_bytes=limit)

    return MinerTaskRequest(
        protocol_version=3, challenge_id="challenge-1", task_id=pack.task_id, identity=pack.identity,
        workspace=pack.refs["workspace"], workspace_url=f"{ORIGIN}/workspace", expires_at=expires,
        slots=MinerSlotSet(submission=slot("patch", "unified_diff_v1", submission_max),
                           trajectory=slot("trajectory", "trajectory_v1", trajectory_max)),
    )


class Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def streamed(content):
    return httpx.Response(200, headers={"Content-Length": str(len(content))}, stream=Body(content))


class Store:
    def __init__(self, pack, tamper=False):
        self.pack, self.tamper, self.puts = pack, tamper, {}

    def handle(self, request):
        if request.method == "GET" and request.url.path == "/workspace":
            data = (self.pack.root / "workspace.tar.zst").read_bytes()
            return streamed(data[:-1] + b"\x00" if self.tamper else data)
        if request.method == "PUT":
            self.puts[request.url.path] = request.read()
            return httpx.Response(201)
        return httpx.Response(404)


def honest_solve(content=ANSWER, header_override=None, trajectory="build"):
    seen = {}

    def solve_fn(pack, clock, spec):
        seen.update(pack=pack, clock=clock, spec=spec, files=sorted(p.name for p in pack.root.iterdir()))
        header = header_override or spec.header
        log = build_within([], header, content, spec.max_bytes, timeout_s=5).data if trajectory == "build" else None
        return SolveResult(content, Rank.CHECKED, "finished", 1, None, trajectory=log,
                           trajectory_error="" if log else "boom")

    return solve_fn, seen


def run(pack, task, solve_fn, tmp_path, store=None, env=None):
    store = store or Store(pack)
    settings = load_env(None, environ=env or {})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(store.handle)) as http:
            return await answer_offer(task, settings, http, origins=ORIGINS, hotkey=HOTKEY, solve_fn=solve_fn,
                                      workdir=tmp_path)

    return asyncio.run(go()), store


def test_an_offer_is_answered_with_both_artifacts_bound_to_it(stats_pack, tmp_path):
    task = offer(stats_pack)
    solve_fn, seen = honest_solve()
    reply, store = run(stats_pack, task, solve_fn, tmp_path)
    validate_miner_response(task, reply.response)
    assert store.puts["/patch"] == ANSWER and store.puts["/trajectory"] == reply.trajectory
    log = parse_trajectory(reply.trajectory)
    assert (log.task_id, log.challenge_id, log.miner_hotkey) == (stats_pack.task_id, "challenge-1", HOTKEY)
    assert log.submission_sha256 == hashlib.sha256(ANSWER).hexdigest()
    assert seen["files"] == ["workspace.tar.zst"] and seen["pack"].task_id == stats_pack.task_id
    assert seen["spec"].max_bytes == 1 << 20
    assert set(reply.seconds) == {"download", "solve", "upload"}


def test_the_trajectory_limit_is_the_smaller_of_the_slot_and_the_setting(stats_pack, tmp_path):
    solve_fn, seen = honest_solve()
    run(stats_pack, offer(stats_pack, trajectory_max=50_000), solve_fn, tmp_path,
        env={"HONEMINER_TRAJECTORY_MAX_BYTES": "20000"})
    assert seen["spec"].max_bytes == 20_000


def test_the_offer_clock_takes_the_earliest_deadline(stats_pack):
    settings = load_env(None, environ={})
    clock = offer_clock(offer(stats_pack, expires_in=1500, slot_expires_in=900), settings)
    assert 900 - 60 < clock.reply_remaining() + settings.expiry_margin_s <= 900


@pytest.mark.parametrize("case", ["no-log", "too-big", "wrong-hotkey-in-log", "slots-for-another-hotkey",
                                  "log-off", "tampered-workspace"])
def test_nothing_is_uploaded_when_the_reply_would_be_rejected(stats_pack, tmp_path, case):
    task, env, store = offer(stats_pack), None, Store(stats_pack)
    solve_fn, _ = honest_solve()
    if case == "no-log":
        solve_fn, _ = honest_solve(trajectory=None)
    elif case == "too-big":
        task = offer(stats_pack, submission_max=4)
    elif case == "wrong-hotkey-in-log":
        solve_fn, _ = honest_solve(header_override=Header(stats_pack.task_id, "challenge-1", "someone-else", "m"))
    elif case == "slots-for-another-hotkey":
        task = task.model_copy(update={"slots": task.slots.model_copy(update={
            "submission": task.slots.submission.model_copy(update={"hotkey": "other"})})})
    elif case == "log-off":
        env = {"HONEMINER_TRAJECTORY": "off"}
    elif case == "tampered-workspace":
        store = Store(stats_pack, tamper=True)
    with pytest.raises(ArtifactDownloadError if case == "tampered-workspace" else ReplyError):
        run(stats_pack, task, solve_fn, tmp_path, store=store, env=env)
    assert store.puts == {}
