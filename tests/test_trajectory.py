import base64
import json
import random

import pytest
from rlvr.v3.trajectory import parse_trajectory

from honeminer import trajectory as tj
from honeminer.trajectory import (
    INTERRUPTED,
    Exchange,
    Header,
    TrajectoryTooLarge,
    assemble,
    build,
    build_within,
    minimal,
    read_traffic,
    record_line,
    turns_from,
)

HEADER = Header(task_id="a" * 64, challenge_id="local-test", miner_hotkey="local", model_name="claude-opus-5-5")
SUBMISSION = b"diff --git a/x b/x\n"
T = 30.0  # generous time box: these tests check content, not the fallback
SYSTEM = [{"type": "text", "text": "You are Claude Code. " * 200}]
TOOLS = [{"name": "Bash", "description": "run a command " * 100, "input_schema": {"type": "object"}}]


def sse(*blocks, stop="end_turn", error=None):
    """An Anthropic streaming response. Blocks: ("text", s) | ("thinking", s) | ("tool", id, name, input)."""

    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}}]
    for index, block in enumerate(blocks):
        if block[0] == "tool":
            _, call_id, name, value = block
            events.append({"type": "content_block_start", "index": index,
                           "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}})
            raw = json.dumps(value)
            for start in range(0, len(raw), 7):  # streamed in pieces, as the API does
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": "input_json_delta", "partial_json": raw[start:start + 7]}})
        else:
            kind, text = block
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": kind, kind: ""}})
            for start in range(0, len(text), 64):
                events.append({"type": "content_block_delta", "index": index,
                               "delta": {"type": f"{kind}_delta", kind: text[start:start + 64]}})
        events.append({"type": "content_block_stop", "index": index})
    if error is not None:
        events.append({"type": "error", "error": error})
    else:
        events.append({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}})
        events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Conversation:
    """Plays Claude Code's traffic: every request repeats the whole conversation so far."""

    def __init__(self):
        self.messages = [{"role": "user", "content": "fix the bug"}]
        self.exchanges = []

    def request(self):
        return json.dumps({"model": "claude-opus-5-5", "system": SYSTEM, "tools": TOOLS, "stream": True,
                           "max_tokens": 32000, "messages": self.messages}).encode()

    def call(self, response, status=200, transport_error=False, path="/v1/messages?beta=true"):
        self.exchanges.append(Exchange(len(self.exchanges), path, None if transport_error else status,
                                       self.request(), None if transport_error else response, transport_error))

    def turn(self, *blocks, results=None):
        """One successful model call; ``results`` maps tool ids to (output, is_error)."""

        self.call(sse(*blocks))
        content = []
        for block in blocks:
            if block[0] == "tool":
                content.append({"type": "tool_use", "id": block[1], "name": block[2], "input": block[3]})
            else:
                content.append({"type": block[0], block[0]: block[1]})
        self.messages = self.messages + [{"role": "assistant", "content": content}]
        if results:
            self.messages = self.messages + [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": key, "content": out, "is_error": err}
                for key, (out, err) in results.items()]}]

    def turns(self):
        return turns_from(self.exchanges)


def events(data):
    return [e.model_dump() for e in parse_trajectory(data).events]


def decode(event, key):
    return base64.b64decode(event[key])


def long_run(n, output_size=200):
    convo = Conversation()
    for i in range(n):
        convo.turn(("thinking", f"step {i}"), ("text", f"running check {i}"),
                   ("tool", f"toolu_{i}", "Bash", {"command": f"make test-{i}"}),
                   results={f"toolu_{i}": ("x" * output_size, False)})
    convo.turn(("text", "done"))
    return convo


# ------------------------------------------------------------------ assembly


def test_sse_text_thinking_and_streamed_tool_input_are_reassembled():
    message = assemble(sse(("thinking", "let me look"), ("text", "Reading the file."),
                           ("tool", "toolu_1", "Read", {"file_path": "/work/a.c", "limit": 40})))
    assert message.text("thinking") == "let me look" and message.text("text") == "Reading the file."
    ((call_id, name, body),) = message.tool_uses()
    assert (call_id, name) == ("toolu_1", "Read")
    assert json.loads(body) == {"file_path": "/work/a.c", "limit": 40}
    assert message.stop_reason == "end_turn" and message.usage == {"input_tokens": 10, "output_tokens": 5}
    condensed = json.loads(message.condensed())
    assert condensed["honeminer_response"] == "condensed_from_sse"


def test_plain_json_response_and_garbage():
    body = json.dumps({"type": "message", "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                       "usage": {}}).encode()
    assert assemble(body).text("text") == "hi"
    assert assemble(b"\x00not json").content == []


# ------------------------------------------------------------------ building


def test_a_simple_run_gives_a_valid_log_bound_to_the_submission():
    convo = Conversation()
    convo.turn(("text", "Looking."), ("tool", "toolu_1", "Bash", {"command": "ls"}),
               results={"toolu_1": ("a.c\nb.c\n", False)})
    convo.turn(("text", "Done."))
    built = build_within(convo.turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES, timeout_s=T)
    parsed = parse_trajectory(built.data)
    assert built.level == "full" and parsed.task_id == HEADER.task_id and parsed.harness_name == "honeminer"
    kinds = [e["event_type"] for e in events(built.data)]
    assert kinds == ["model_turn", "tool_call", "tool_result", "model_turn", "tool_call", "tool_result",
                     "final_submission"]
    first, _, result, second, *_ = events(built.data)
    assert decode(first, "request_body_b64") == convo.exchanges[0].request  # exact bytes as sent
    assert decode(first, "response_body_b64") == convo.exchanges[0].response
    assert first["output"] == "Looking." and first["generated_bytes_b64"] == "" and first["tokens"] == []
    assert decode(result, "output_body_b64") == b"a.c\nb.c\n" and result["is_error"] is False
    delta = json.loads(decode(second, "request_body_b64"))
    prefix = json.loads(convo.exchanges[1].request)["messages"][:1]
    assert delta["prefix_sha256"] == tj._sha(tj._canon(prefix))
    assert delta["honeminer_request"] == "delta" and delta["prefix_messages"] == 1 and len(delta["messages"]) == 2
    assert delta["system"]["honeminer"] == "same_as_previous"


def test_parallel_tools_a_tool_error_and_a_killed_run():
    convo = Conversation()
    convo.turn(("tool", "toolu_a", "Read", {"file_path": "a"}), ("tool", "toolu_b", "Bash", {"command": "false"}),
               results={"toolu_a": ("contents", False), "toolu_b": ("exit 1", True)})
    convo.turn(("tool", "toolu_c", "Bash", {"command": "sleep 999"}))  # killed at the deadline: no result
    log = events(build(convo.turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data)
    results = {e["call_id"]: e for e in log if e["event_type"] == "tool_result"}
    assert results["toolu_b"]["is_error"] is True and results["toolu_a"]["is_error"] is False
    assert decode(results["toolu_c"], "output_body_b64") == INTERRUPTED and results["toolu_c"]["is_error"] is True


def test_failures_rate_limit_error_in_stream_and_transport():
    convo = Conversation()
    convo.call(b'{"type":"error","error":{"type":"rate_limit_error"}}', status=429)
    convo.call(sse(("text", "partial"), error={"type": "overloaded_error"}))
    convo.call(None, transport_error=True)
    convo.call(b"{}", path="/v1/messages/count_tokens")  # ignored
    convo.turn(("text", "fine now"))
    log = events(build(convo.turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data)
    failures = [e for e in log if e["event_type"] == "model_failure"]
    assert [(f["failure_kind"], f["status_code"]) for f in failures] == [
        ("http_error", 429), ("provider_rejected", 200), ("transport_error", None)]
    assert failures[2]["response_body_b64"] is None
    assert [e["output"] for e in log if e["event_type"] == "model_turn"] == ["fine now"]


def test_no_traffic_still_gives_a_valid_labeled_log():
    built = build_within([], HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES, timeout_s=T)
    first = events(built.data)[0]
    assert json.loads(decode(first, "request_body_b64")) == {"honeminer": "no_recorded_model_traffic"}
    only_failures = Conversation()
    only_failures.call(None, transport_error=True)
    parse_trajectory(build(only_failures.turns(), HEADER, SUBMISSION, 10**6).data)


def test_growth_is_linear_and_output_is_deterministic():
    small = build(long_run(10).turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data
    large = build(long_run(60).turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data
    assert len(large) < 8 * len(small)  # quadratic growth would be ~36x
    assert large == build(long_run(60).turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data


def test_lone_surrogates_and_duplicate_ids_stay_valid():
    convo = Conversation()
    convo.call(b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
               b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"\\ud800x"}}\n')
    convo.turn(("tool", "toolu_1", "Bash", {}), results={"toolu_1": ("ok", False)})
    convo.turn(("tool", "toolu_1", "Bash", {}))  # a repeated id is kept once
    log = events(build(convo.turns(), HEADER, SUBMISSION, tj.LOCAL_MAX_BYTES).data)
    assert sum(e["event_type"] == "tool_call" and e["call_id"] == "toolu_1" for e in log) == 1


# ------------------------------------------------------------------ the size limit


def test_each_rung_is_used_as_the_limit_shrinks():
    convo = Conversation()
    for i in range(40):
        convo.turn(("text", f"step {i} " + "reasoning about the fix " * 800),
                   ("tool", f"toolu_{i}", "Bash", {"command": f"make test-{i}"}),
                   results={f"toolu_{i}": ("x" * 40_000, False)})
    turns = convo.turns()
    sizes = [len(tj.render(turns, HEADER, SUBMISSION, level)) for level in tj.LADDER]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)
    for level, size in zip(tj.LADDER, sizes, strict=True):
        built = build_within(turns, HEADER, SUBMISSION, int(size / tj.TARGET_FRACTION) + 2, timeout_s=T)
        assert built.level == level.name
    seen = set()
    for limit in (sizes[-1] // 2, 40_000, 6_000, 3_000):
        built = build_within(turns, HEADER, SUBMISSION, limit, timeout_s=T)
        assert len(built.data) <= limit
        seen.add(built.level)
    assert "dropped" in seen and any(level.startswith("cap") or level == "minimal" for level in seen)
    dropped = events(build(turns, HEADER, SUBMISSION, sizes[-1] // 2).data)
    note = [e for e in dropped if e["event_type"] == "tool_call" and e["call_id"] == "honeminer-omitted-turns"]
    assert note and json.loads(decode(note[0], "input_body_b64"))["honeminer"] == "omitted_turns"
    kept = [e for e in dropped if e["event_type"] == "model_turn"]
    assert kept[0]["output"].startswith("step 0 ") and kept[-1]["output"].startswith("step 39 ")


def test_never_over_the_limit_property():
    rng = random.Random(5)
    floor = len(minimal(HEADER, SUBMISSION))
    for n in (1, 2, 3, 7, 25, 120, 300):
        convo = Conversation()
        for i in range(n):
            size = rng.choice([0, 100, 5_000, 70_000] if n <= 25 else [0, 100, 2_000])  # keeps test traffic small
            convo.turn(("text", "t" * rng.choice([0, 50, 20_000])), ("tool", f"t{i}", "Bash", {"c": i}),
                       results={f"t{i}": ("o" * size, rng.random() < 0.2)})
        if n % 2:
            convo.call(None, transport_error=True)
        turns = convo.turns()
        for limit in (500, floor - 1, floor, 2_000, 8_000, 50_000, 700_000, 5 * 1024**2, 64 * 1024**2):
            if limit < floor:
                with pytest.raises(TrajectoryTooLarge):
                    build_within(turns, HEADER, SUBMISSION, limit, timeout_s=T)
                continue
            built = build_within(turns, HEADER, SUBMISSION, limit, timeout_s=T)
            assert len(built.data) <= limit, (n, limit, built.level)
            parse_trajectory(built.data)


def test_a_big_tool_output_is_trimmed_in_the_middle_with_its_hash():
    convo = Conversation()
    convo.turn(("tool", "toolu_1", "Bash", {}), results={"toolu_1": ("h" * 50_000 + "t" * 50_000, False)})
    convo.turn(("text", "done"))
    built = build(convo.turns(), HEADER, SUBMISSION, 150_000)
    result = next(e for e in events(built.data) if e["event_type"] == "tool_result")
    body = decode(result, "output_body_b64")
    assert body.startswith(b"h" * 100) and body.endswith(b"t" * 100) and b"bytes elided, sha256=" in body


def test_timeout_and_errors_fall_back_to_the_minimal_log(monkeypatch):
    turns = long_run(5).turns()

    def boom(*args, **kwargs):
        raise RuntimeError("builder bug")

    monkeypatch.setattr(tj, "build", boom)
    built = build_within(turns, HEADER, SUBMISSION, 100_000, timeout_s=T)
    assert built.level == "minimal-fallback" and len(built.data) <= 100_000
    parse_trajectory(built.data)

    monkeypatch.setattr(tj, "build", lambda *a, **k: __import__("time").sleep(2))
    assert build_within(turns, HEADER, SUBMISSION, 100_000, timeout_s=0.1).level == "minimal-fallback"


# ------------------------------------------------------------------ records


def test_traffic_file_round_trip_skips_a_torn_line(tmp_path):
    path = tmp_path / "traffic.jsonl"
    path.write_text(record_line(1, "/v1/messages", 200, b"{}", b"x") + record_line(0, "/v1/messages", None, b"{}",
                                                                                   None, True) + '{"seq": 2, "pa')
    exchanges = read_traffic(path)
    assert [e.seq for e in exchanges] == [0, 1] and exchanges[0].transport_error and exchanges[1].response == b"x"
    assert read_traffic(tmp_path / "missing.jsonl") == []


def test_build_from_traffic_file(tmp_path):
    convo = long_run(3)
    path = tmp_path / "traffic.jsonl"
    path.write_text("".join(record_line(e.seq, e.path, e.status, e.request, e.response, e.transport_error)
                            for e in convo.exchanges))
    built = tj.build_from_traffic(path, HEADER, SUBMISSION, 10**6, timeout_s=T)
    assert built.level == "full"
    assert built.data == build(convo.turns(), HEADER, SUBMISSION, 10**6).data
    missing = tj.build_from_traffic(tmp_path / "none.jsonl", HEADER, SUBMISSION, 10**6, timeout_s=T)
    parse_trajectory(missing.data)


def test_the_prefix_hash_survives_a_changed_message():
    convo = long_run(4)
    # Claude Code moves cache_control markers: an earlier message changes between requests.
    request = json.loads(convo.exchanges[-1].request)
    request["messages"][1]["content"][0]["cache_control"] = {"type": "ephemeral"}
    convo.exchanges.append(Exchange(99, "/v1/messages", 200, json.dumps(request).encode(), sse(("text", "x"))))
    last = convo.turns()[-1]
    delta = json.loads(last.request_delta)
    assert delta["prefix_messages"] == 1
    assert delta["prefix_sha256"] == tj._sha(tj._canon(request["messages"][:1]))


def test_a_large_submission_does_not_raise_the_floor():
    big = b"+" * 3_000_000
    built = build_within(long_run(3).turns(), HEADER, big, 20_000, timeout_s=T)
    assert len(built.data) <= 20_000
    assert parse_trajectory(built.data).submission_sha256 == tj._sha(big)
    assert build_within([], HEADER, big, tj.LOCAL_MAX_BYTES, timeout_s=T).level == "full"  # echoed whole when it fits


def test_the_first_copy_of_a_tool_result_is_the_real_one():
    convo = Conversation()
    convo.turn(("tool", "t1", "Bash", {"command": "make"}), results={"t1": ("REAL OUTPUT", False)})
    convo.turn(("text", "next"))
    # Claude Code clears old tool results to save context; the later copy is not what the tool returned.
    for message in convo.messages:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if block.get("type") == "tool_result":
                block["content"] = "[Old tool result content cleared]"
    convo.turn(("text", "done"))
    (tool,) = [t for turn in convo.turns() for t in turn.tools]
    assert tool.output == b"REAL OUTPUT"


def test_unicode_line_separators_inside_json_do_not_split_sse_events():
    body = ("data: " + json.dumps({"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "text", "text": ""}}) + "\n"
            + "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "line1 line2\x85"}},
                                    ensure_ascii=False) + "\n"
            + "data: " + json.dumps({"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": " end"}}) + "\n").encode()
    assert assemble(body).text("text") == "line1 line2\x85 end"


@pytest.mark.parametrize("body", [b"null", b"[]", b'"x"', b"data: 1\n", b"data: []\n"])
def test_responses_that_are_json_but_not_objects_are_tolerated(body):
    assert assemble(body).content == []
    turns = turns_from([Exchange(0, "/v1/messages", 502, b"{}", body)])
    assert len(turns) == 1 and turns[0].kind == "failure"


def test_deeply_nested_tool_input_never_breaks_the_log():
    deep: object = "x"
    for _ in range(900):
        deep = [deep]
    convo = Conversation()
    convo.turn(("tool", "t1", "Bash", {"deep": deep}), results={"t1": ("ok", False)})
    convo.turn(("text", "done"))
    for limit in (3_000, 20_000, tj.LOCAL_MAX_BYTES):
        built = build_within(convo.turns(), HEADER, SUBMISSION, limit, timeout_s=T)
        assert len(built.data) <= limit
        parse_trajectory(built.data)


def test_lone_surrogates_in_tool_names_and_ids_are_cleaned():
    turn = tj.Turn("turn", b"{}", b"{}", "0" * 64, 0, 1, b"", b"{}", "", "",
                   tools=(tj.ToolEvent("id\ud800", "Ba\udc00sh", b"{}", b"ok", False),))
    built = build(turns=[turn] * 1, header=HEADER, submission=SUBMISSION, max_bytes=tj.LOCAL_MAX_BYTES)
    assert built.level == "full"  # nothing had to be dropped to make it canonical
    names = [e["tool_name"] for e in events(built.data) if e["event_type"] == "tool_call"]
    assert "Ba�sh" in names or "Ba?sh" in names
