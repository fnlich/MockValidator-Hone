import asyncio
import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest
from rlvr import protocol
from rlvr.protocol import sign_message, verify_signature
from rlvr.v3.api import MinerTaskRequest
from rlvr.v3.identity import compute_task_id

from honeminer.config import load_env
from honeminer.pack import RECIPES_DIR, build_pack, load_recipe
from honeminer.server import OfferServer, authorized, build_app, hotkey_of
from honeminer.tasks import load_pack
from tests.test_pack import host_runner
from tests.test_reply import HOTKEY, ORIGINS, Store, honest_solve, offer

VALIDATOR = "hk-validator"


@pytest.fixture(scope="module")
def stats_pack(tmp_path_factory):
    out = tmp_path_factory.mktemp("packs") / "python-stats"
    return load_pack(build_pack(load_recipe(RECIPES_DIR / "python-stats"), out, host_runner))


@pytest.fixture(autouse=True)
def hmac_signing(monkeypatch):
    """rlvr's HMAC fallback, so plain string ids can sign (rlvr's own tests do the same)."""

    monkeypatch.setattr(protocol, "_HAVE_CRYPTO", False)


def metagraph(permit=True, stake=100.0):
    return SimpleNamespace(hotkeys=[VALIDATOR, HOTKEY, "hk-stranger"], S=[stake, 0.0, 50.0],
                           validator_permit=[permit, False, False])


def make_server(pack, tmp_path, *, solve_fn=None, env=None, meta=None, store=None):
    settings = load_env(None, environ={"HONEMINER_RUNS_DIR": str(tmp_path / "runs"), **(env or {})})
    store = store or Store(pack)
    http = httpx.AsyncClient(transport=httpx.MockTransport(store.handle))
    server = OfferServer(settings, wallet=HOTKEY, metagraph=meta or metagraph(), http=http,
                         solve_fn=solve_fn or honest_solve()[0], origins=ORIGINS)
    return server, store


def signed(body: bytes, signer=VALIDATOR, signed_for=HOTKEY):
    return {"Content-Type": "application/json", **sign_message(signer, body, signed_for=signed_for)}


def call(server, body, headers=None, path="/solve"):
    async def go():
        transport = httpx.ASGITransport(app=build_app(server))
        async with httpx.AsyncClient(transport=transport, base_url="http://miner") as client:
            return await client.post(path, content=body, headers=headers if headers is not None else signed(body))

    return asyncio.run(go())


def test_a_signed_offer_from_a_validator_gets_a_signed_reply(stats_pack, tmp_path):
    server, store = make_server(stats_pack, tmp_path)
    task = offer(stats_pack)
    response = call(server, task.model_dump_json().encode())
    assert response.status_code == 200, response.text
    assert response.headers["Epistula-Signed-By"] == HOTKEY and len(response.content) <= 16_384
    assert verify_signature(response.headers, response.content, expected_signed_for=VALIDATOR)
    assert json.loads(response.content)["challenge_id"] == task.challenge_id and set(store.puts) == {
        "/patch", "/trajectory"}
    lines = (tmp_path / "runs" / "offers.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["status"] == 200
    archived = list((tmp_path / "runs").glob("*-offer-challenge-1"))
    assert archived and (archived[0] / "offer.json").is_file() and (archived[0] / "reply.json").is_file()


@pytest.mark.parametrize("case,status", [
    ("unsigned", 401), ("signed-for-someone-else", 401), ("tampered-body", 401), ("replay", 409),
    ("not-registered", 403), ("no-permit", 403), ("low-stake", 403), ("other-miners-slots", 403),
    ("not-json", 400), ("unsupported-policy", 422), ("expired", 410), ("too-big", 413),
])
def test_requests_that_must_be_refused(stats_pack, tmp_path, case, status):
    env, meta, task = {}, None, offer(stats_pack)
    if case == "no-permit":
        meta = metagraph(permit=False)
    elif case == "low-stake":
        env["HONEMINER_MIN_VALIDATOR_STAKE"] = "1000"
    elif case == "other-miners-slots":  # a valid offer, for another miner's slots
        raw = task.model_dump(mode="json")
        for slot in raw["slots"].values():
            slot["hotkey"] = "hk-other"
        task = MinerTaskRequest.model_validate(raw)
    elif case == "unsupported-policy":  # a valid offer, for a policy we do not support
        identity = task.identity.model_copy(update={"execution_profile_id": "other-profile"})
        task = offer(SimpleNamespace(identity=identity, task_id=compute_task_id(identity), refs=stats_pack.refs))
    elif case == "expired":
        task = task.model_copy(update={"expires_at": 1_000_000})
    server, store = make_server(stats_pack, tmp_path, env=env, meta=meta)
    body = b"not json" if case == "not-json" else task.model_dump_json().encode()
    headers = signed(body, signer="hk-nobody" if case == "not-registered" else VALIDATOR,
                     signed_for="hk-other" if case == "signed-for-someone-else" else HOTKEY)
    if case == "unsigned":
        headers = {"Content-Type": "application/json"}
    elif case == "tampered-body":
        body = body.replace(b"challenge-1", b"challenge-2")
    elif case == "too-big":
        body = b" " * 1_000_001
        headers = signed(body)
    elif case == "replay":
        assert call(server, body, headers).status_code == 200
    response = call(server, body, headers)
    assert response.status_code == status, response.text
    assert "Epistula-Signature" not in response.headers and "Epistula-Request-Signature" not in response.headers
    if case != "replay":
        assert store.puts == {}


def test_no_acceptable_answer_is_a_clear_refusal_and_nothing_is_uploaded(stats_pack, tmp_path):
    server, store = make_server(stats_pack, tmp_path, solve_fn=honest_solve(trajectory=None)[0])
    response = call(server, offer(stats_pack).model_dump_json().encode())
    assert response.status_code == 422 and "no work log" in response.json()["error"] and store.puts == {}


def test_offers_that_cannot_get_a_useful_solve_are_refused_at_once(stats_pack, tmp_path):
    server, store = make_server(stats_pack, tmp_path, env={"HONEMINER_MIN_SOLVE_S": "3600"})
    response = call(server, offer(stats_pack).model_dump_json().encode())
    assert response.status_code == 503 and store.puts == {}


def test_a_queued_offer_gives_up_when_no_slot_frees_in_time(stats_pack, tmp_path):
    server, store = make_server(stats_pack, tmp_path, env={"HONEMINER_SLOTS": "1"})
    body = offer(stats_pack).model_dump_json().encode()

    async def go():
        await server.slots.acquire()  # another offer is being solved
        # Agent time is ~1100 s (the 20-minute budget less the reserves); ask for all but ~1 s of it.
        server.settings = dataclasses.replace(server.settings, min_solve_s=int(offer_agent_s(stats_pack)) - 1)
        return await server.handle_solve(signed(body), body)

    status, payload = asyncio.run(go())
    assert status == 503 and "busy" in payload["error"] and store.puts == {}


def offer_agent_s(pack):
    from honeminer.reply import offer_clock

    return offer_clock(offer(pack), load_env(None, environ={})).agent_remaining()


def notice(challenge="challenge-1", task_id=None, pack=None):
    return json.dumps({"protocol_version": 3, "message_type": "failure_notice_v1", "challenge_id": challenge,
                       "task_id": task_id or pack.task_id, "uid": 7, "hotkey": HOTKEY,
                       "failure": {"version": 1, "reason_code": "check_failed",
                                   "failed_check": "02-percentile: expected 2.5"}}).encode()


def test_failure_notices_are_archived_only_for_tasks_we_answered(stats_pack, tmp_path):
    server, _ = make_server(stats_pack, tmp_path)
    unknown = notice(pack=stats_pack)
    assert call(server, unknown, path="/v3/failure").status_code == 404  # nothing served yet
    assert call(server, offer(stats_pack).model_dump_json().encode()).status_code == 200
    body = notice(pack=stats_pack)
    assert call(server, body, path="/v3/failure").status_code == 200
    assert call(server, body, path="/v3/failure").status_code == 200  # a duplicate is acknowledged, not re-stored
    lines = [json.loads(x) for x in (tmp_path / "runs" / "notices.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["reason"] == "check_failed" and lines[0]["validator"] == VALIDATOR
    assert call(server, notice(challenge="challenge-9", pack=stats_pack), path="/v3/failure").status_code == 404
    other = notice(pack=stats_pack)
    assert call(server, other, signed(other, signer="hk-stranger"), path="/v3/failure").status_code == 403


def test_helpers():
    assert hotkey_of("hk") == "hk"
    assert hotkey_of(SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5abc"))) == "5abc"
    assert hotkey_of(SimpleNamespace(ss58_address="5def")) == "5def"
    assert authorized(metagraph(), VALIDATOR, min_stake=0, require_permit=True)
    assert not authorized(SimpleNamespace(hotkeys=[VALIDATOR]), VALIDATOR, min_stake=0, require_permit=True)
    assert not authorized(None, VALIDATOR, min_stake=0, require_permit=False)


def test_no_credential_reaches_the_offer_archive(stats_pack, tmp_path):
    secret = "sk-ant-very-secret-credential"
    server, _ = make_server(stats_pack, tmp_path, env={"HONEMINER_AUTH": "api_key", "ANTHROPIC_API_KEY": secret})
    assert call(server, offer(stats_pack).model_dump_json().encode()).status_code == 200
    for path in (tmp_path / "runs").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_after_a_504_the_slot_stays_taken_until_the_solve_really_ends(stats_pack, tmp_path):
    import threading
    import time

    finished = threading.Event()

    def slow(pack, clock, spec):
        time.sleep(7)
        finished.set()
        raise RuntimeError("too late anyway")

    server, _ = make_server(stats_pack, tmp_path, solve_fn=slow,
                            env={"HONEMINER_MIN_SOLVE_S": "0", "HONEMINER_EXPIRY_MARGIN_S": "30"})
    body = offer(stats_pack, expires_in=35).model_dump_json().encode()

    async def go():
        status, _ = await server.handle_solve(signed(body), body)
        held_after_504 = server.slots.locked()
        while not finished.is_set():
            await asyncio.sleep(0.1)
        for _ in range(30):  # released shortly after the solve thread ends
            if not server.slots.locked():
                break
            await asyncio.sleep(0.1)
        return status, held_after_504, server.slots.locked()

    status, held_after_504, held_at_the_end = asyncio.run(go())
    assert status == 504 and held_after_504 and not held_at_the_end


def test_an_unauthorized_caller_does_not_fill_the_replay_cache(stats_pack, tmp_path):
    server, _ = make_server(stats_pack, tmp_path)
    body = offer(stats_pack).model_dump_json().encode()
    headers = signed(body, signer="hk-nobody")
    assert call(server, body, headers).status_code == 403
    assert server.nonces.check_and_add(headers["Epistula-Uuid"])  # never recorded


def test_the_downloaded_workspace_archive_is_deleted_after_the_offer(stats_pack, tmp_path):
    server, _ = make_server(stats_pack, tmp_path)
    assert call(server, offer(stats_pack).model_dump_json().encode()).status_code == 200
    assert not list((tmp_path / "runs").rglob("*.tar.zst"))
