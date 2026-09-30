"""Build the V4 work log (trajectory_v1) from recorded gateway traffic. No model is involved.

Rules:
- It never goes over the size limit: the size is measured on the exact serialized bytes, and a bounded
  shrinking ladder always ends in a log that fits (or an explicit refusal below the ~1.5 KB floor).
- It never touches the solve: it runs after Claude has stopped, from traffic that already happened.
- It is compact, not perfect: later requests store only what changed (with hashes of what was elided);
  anything condensed is labeled with a ``honeminer`` key, never invented.
- The same traffic always gives the same bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from rlvr.v3.canonical import canonical_json_bytes
from rlvr.v3.trajectory import Trajectory, parse_trajectory, serialize_trajectory

from honeminer import __version__

TRIM_OVER = 16 * 1024
TRIM_KEEP = 8 * 1024
ESSENTIAL_CAP = 4 * 1024
KEEP_RECENT = 20
TARGET_FRACTION = 0.99
LOCAL_MAX_BYTES = 64 * 1024**2


class TrajectoryTooLarge(ValueError):
    """Even the smallest valid log does not fit the slot."""


# ------------------------------------------------------------------ records


@dataclass(frozen=True)
class Exchange:
    seq: int
    path: str
    status: int | None
    request: bytes
    response: bytes | None
    transport_error: bool = False


def record_line(seq: int, path: str, status: int | None, request: bytes, response: bytes | None,
                transport_error: bool = False) -> str:
    """One traffic.jsonl line (written by the gateway)."""

    return json.dumps({
        "seq": seq, "path": path, "status": status, "transport_error": transport_error,
        "request_b64": base64.b64encode(request).decode(),
        "response_b64": None if response is None else base64.b64encode(response).decode(),
    }, separators=(",", ":")) + "\n"


def read_traffic(path: Path) -> list[Exchange]:
    exchanges = []
    if not path.is_file():
        return exchanges
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a torn last line from a killed writer
        response = item.get("response_b64")
        exchanges.append(Exchange(
            seq=int(item["seq"]), path=item["path"], status=item.get("status"),
            request=base64.b64decode(item["request_b64"]),
            response=None if response is None else base64.b64decode(response),
            transport_error=bool(item.get("transport_error")),
        ))
    return sorted(exchanges, key=lambda e: e.seq)


# ------------------------------------------------------------------ response assembly


@dataclass
class Message:
    content: list[dict] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict = field(default_factory=dict)
    error: dict | None = None
    raw_inputs: dict[int, str] = field(default_factory=dict)  # block index -> streamed JSON text

    def text(self, kind: str) -> str:
        return "".join(block.get(kind, "") for block in self.content if block.get("type") == kind)

    def tool_uses(self) -> list[tuple[str, str, bytes]]:
        uses = []
        for index, block in enumerate(self.content):
            if block.get("type") == "tool_use" and block.get("id"):
                raw = self.raw_inputs.get(index)
                body = raw.encode("utf-8", "replace") if raw else json.dumps(block.get("input", {}), ensure_ascii=False,
                                                           separators=(",", ":")).encode("utf-8", "replace")
                uses.append((str(block["id"]), str(block.get("name") or "tool"), body))
        return uses

    def condensed(self) -> bytes:
        return json.dumps({"honeminer_response": "condensed_from_sse", "content": self.content,
                           "stop_reason": self.stop_reason, "usage": self.usage},
                          ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8", "replace")


def assemble(body: bytes) -> Message:
    """Rebuild the final message from an SSE stream or a plain JSON response."""

    message = Message()
    text = body.decode("utf-8", "replace")
    if not text.lstrip().startswith(("event:", "data:")):
        try:
            data = json.loads(text)
        except ValueError:
            return message
        if not isinstance(data, dict):
            return message
        if data.get("type") == "error":
            message.error = data.get("error") or {}
        message.content = [b for b in data.get("content") or [] if isinstance(b, dict)]
        message.stop_reason = data.get("stop_reason")
        message.usage = data.get("usage") or {}
        return message
    blocks: dict[int, dict] = {}
    pieces: dict[tuple[int, str], list[str]] = {}  # joined once at the end (no quadratic concatenation)
    for line in re.split(r"\r\n|\r|\n", text):
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_start":
            message.usage.update((event.get("message") or {}).get("usage") or {})
        elif kind == "content_block_start":
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["input"] = {}
            blocks[int(event.get("index", len(blocks)))] = block
        elif kind == "content_block_delta":
            index = int(event.get("index", 0))
            block = blocks.setdefault(index, {"type": "text"})
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                pieces.setdefault((index, "text"), [block.get("text", "")]).append(str(delta.get("text", "")))
            elif delta.get("type") == "thinking_delta":
                pieces.setdefault((index, "thinking"), [block.get("thinking", "")]).append(
                    str(delta.get("thinking", "")))
            elif delta.get("type") == "input_json_delta":
                pieces.setdefault((index, "input"), []).append(str(delta.get("partial_json", "")))
        elif kind == "message_delta":
            message.stop_reason = (event.get("delta") or {}).get("stop_reason", message.stop_reason)
            message.usage.update(event.get("usage") or {})
        elif kind == "error":
            message.error = event.get("error") or {}
    for (index, key), parts in pieces.items():
        if key == "input":
            message.raw_inputs[index] = "".join(parts)
        else:
            blocks[index][key] = "".join(parts)
    for index, raw in message.raw_inputs.items():
        try:
            blocks[index]["input"] = json.loads(raw) if raw else {}
        except (ValueError, KeyError):
            pass
    message.content = [blocks[i] for i in sorted(blocks)]
    return message


# ------------------------------------------------------------------ turns


def _canon(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8", "replace")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ToolEvent:
    call_id: str
    name: str
    input: bytes
    output: bytes | None  # None: the run ended before the tool returned
    is_error: bool


@dataclass(frozen=True)
class Turn:
    """One model call and the tool calls it made (with their results)."""

    kind: str  # "turn" | "failure"
    request_full: bytes
    request_delta: bytes
    request_sha: str
    prefix: int
    new_messages: int
    response: bytes | None
    condensed: bytes
    output: str
    reasoning: str
    tools: tuple[ToolEvent, ...] = ()
    status: int | None = None
    failure_kind: str = ""


def _tool_results(request: dict) -> dict[str, tuple[bytes, bool]]:
    found: dict[str, tuple[bytes, bool]] = {}
    for message in request.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                value = block.get("content", "")
                body = value.encode("utf-8", "replace") if isinstance(value, str) else _canon(value)
                found[str(block["tool_use_id"])] = (body, bool(block.get("is_error")))
    return found


class _PrefixHasher:
    """sha256 of the canonical JSON array of a message prefix, each message canonicalized once (not per turn)."""

    def __init__(self) -> None:
        self.states = [hashlib.sha256(b"[")]

    def digest(self, messages: list, common: int) -> str:
        del self.states[common + 1:]  # states past the shared prefix hashed messages that changed
        for index in range(len(self.states) - 1, common):
            state = self.states[-1].copy()
            state.update((b"," if index else b"") + _canon(messages[index]))
            self.states.append(state)
        final = self.states[common].copy()
        final.update(b"]")
        return final.hexdigest()


def _delta(request: dict, previous: dict | None, hasher: _PrefixHasher) -> tuple[bytes, int, int]:
    messages = request.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    if previous is None:
        return b"", 0, len(messages)
    before = previous.get("messages") or []
    if not isinstance(before, list):
        before = []
    common = 0
    while common < min(len(before), len(messages)) and before[common] == messages[common]:
        common += 1
    delta: dict = {"honeminer_request": "delta", "prefix_messages": common,
                   "prefix_sha256": hasher.digest(messages, common), "messages": messages[common:]}
    for key, value in request.items():
        if key == "messages":
            continue
        if key in ("system", "tools") and previous.get(key) == value:
            delta[key] = {"honeminer": "same_as_previous", "sha256": _sha(_canon(value))}
        else:
            delta[key] = value
    return _canon(delta), common, len(messages) - common


def turns_from(exchanges: Iterable[Exchange]) -> list[Turn]:
    """Model calls in order. Only the first request is kept whole; later ones as deltas (linear size)."""

    raw: list[dict] = []
    results: dict[str, tuple[bytes, bool]] = {}
    previous: dict | None = None
    hasher = _PrefixHasher()
    for exchange in exchanges:
        if not exchange.path.startswith("/v1/messages") or exchange.path.startswith("/v1/messages/count_tokens"):
            continue
        try:
            request = json.loads(exchange.request or b"{}")
        except ValueError:
            request = {}
        if not isinstance(request, dict):
            request = {}
        for call_id, found in _tool_results(request).items():
            results.setdefault(call_id, found)  # later copies may be rewritten (Claude Code clears old results)
        delta, prefix, new = _delta(request, previous, hasher)
        previous = request
        raw.append({"full": exchange.request if not raw else b"", "delta": delta or exchange.request,
                    "sha": _sha(exchange.request), "prefix": prefix, "new": new, "exchange": exchange})
    turns = []
    for entry in raw:
        exchange: Exchange = entry["exchange"]
        common = dict(request_full=entry["full"], request_delta=entry["delta"], request_sha=entry["sha"],
                      prefix=entry["prefix"], new_messages=entry["new"])
        if exchange.transport_error or exchange.response is None:
            turns.append(Turn("failure", response=None, condensed=b"", output="", reasoning="",
                              failure_kind="transport_error", **common))
            continue
        message = assemble(exchange.response)
        if exchange.status != 200 or message.error is not None:
            kind = "http_error" if exchange.status != 200 else "provider_rejected"
            status = exchange.status if isinstance(exchange.status, int) and 100 <= exchange.status <= 599 else 500
            turns.append(Turn("failure", response=exchange.response, condensed=b"", output="", reasoning="",
                              status=status, failure_kind=kind, **common))
            continue
        tools = tuple(
            ToolEvent(call_id, name, body, *(results.get(call_id) or (None, True)))
            for call_id, name, body in message.tool_uses()
        )
        turns.append(Turn("turn", response=exchange.response, condensed=message.condensed(),
                          output=message.text("text"), reasoning=message.text("thinking"), tools=tools, **common))
    return turns


# ------------------------------------------------------------------ rendering and fitting


@dataclass(frozen=True)
class Level:
    name: str
    condense: bool = False  # responses as assembled JSON instead of raw SSE
    trim: bool = False  # long strings keep head and tail
    essentials_older: bool = False  # all but the newest KEEP_RECENT turns reduced to essentials
    drop_oldest: int = 0  # turns dropped from the start (the first turn always stays)
    cap: int | None = None  # hard cap for every text and body


LADDER = (Level("full"), Level("condensed", condense=True), Level("trimmed", condense=True, trim=True),
          Level("essentials", condense=True, trim=True, essentials_older=True))
INTERRUPTED = b"honeminer: the run ended before this tool returned"


def _trim_bytes(data: bytes, over: int, keep: int) -> bytes:
    if len(data) <= over:
        return data
    marker = f"\n...[honeminer: {len(data) - 2 * keep} bytes elided, sha256={_sha(data)}]...\n".encode()
    return data[:keep] + marker + (data[len(data) - keep:] if keep else b"")


def _clean(text: str) -> str:
    """Lone surrogates (possible after JSON unescaping) cannot be canonicalized; replace them."""

    return text.encode("utf-8", "replace").decode("utf-8")


def _trim_text(text: str, over: int, keep: int) -> str:
    return _trim_bytes(_clean(text).encode(), over, keep).decode("utf-8", "ignore")


def _trim_json(data: bytes, over: int, keep: int) -> bytes:
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return _trim_bytes(data, over, keep)

    def walk(item):
        if isinstance(item, str):
            return _trim_text(item, over, keep)
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item

    try:
        return _canon(walk(value))
    except RecursionError:  # nesting too deep to walk: trim the bytes instead
        return _trim_bytes(data, over, keep)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _render_turn(index: int, turn: Turn, level: Level, recent: bool, first: bool, seen: set[str]) -> list[dict]:
    essentials = level.essentials_older and not recent
    over, keep = (TRIM_OVER, TRIM_KEEP)
    if level.cap is not None:
        over, keep = level.cap, max(0, level.cap // 2)
    if essentials:
        over, keep = min(over, ESSENTIAL_CAP), min(keep, ESSENTIAL_CAP // 2)
    trim = level.trim or essentials or level.cap is not None

    if essentials and not first:
        request = _canon({"honeminer_request": "essentials", "sha256": turn.request_sha,
                          "prefix_messages": turn.prefix, "new_messages": turn.new_messages})
    else:
        request = (turn.request_full or turn.request_delta) if first else turn.request_delta
        if trim:
            request = _trim_json(request, over, keep)
    if level.cap is not None and len(request) > level.cap:
        request = _canon({"honeminer_request": "essentials", "sha256": turn.request_sha,
                          "prefix_messages": turn.prefix, "new_messages": turn.new_messages})

    if turn.kind == "failure":
        response = turn.response
        if response is not None and trim:
            response = _trim_bytes(response, over, keep)
        return [{"event_type": "model_failure", "request_body_b64": _b64(request),
                 "response_body_b64": None if turn.failure_kind == "transport_error" else _b64(response or b""),
                 "failure_kind": turn.failure_kind,
                 "status_code": None if turn.failure_kind == "transport_error" else turn.status}]

    response = turn.condensed if (level.condense or essentials) else turn.response or b""
    if trim:
        response = _trim_json(response, over, keep)
    output, reasoning = _clean(turn.output), _clean(turn.reasoning)
    if trim:
        output, reasoning = _trim_text(output, over, keep), _trim_text(reasoning, over, keep)
    events = [{"event_type": "model_turn", "request_body_b64": _b64(request), "response_body_b64": _b64(response),
               "generated_bytes_b64": "", "reasoning": reasoning, "output": output, "tokens": []}]
    tools = []
    for tool in turn.tools:
        call_id = _clean(tool.call_id)[:256]
        if call_id and call_id not in seen:  # ids must be unique across the whole log
            seen.add(call_id)
            tools.append(replace(tool, call_id=call_id))
    for tool in tools:
        tool_input = _trim_bytes(tool.input, over, keep) if trim else tool.input
        events.append({"event_type": "tool_call", "call_id": tool.call_id,
                       "tool_name": _clean(tool.name)[:256] or "tool", "input_body_b64": _b64(tool_input)})
    for tool in tools:
        body = INTERRUPTED if tool.output is None else tool.output
        if trim:
            body = _trim_bytes(body, over, keep)
        events.append({"event_type": "tool_result", "call_id": tool.call_id, "output_body_b64": _b64(body),
                       "is_error": bool(tool.is_error or tool.output is None)})
    return events


@dataclass(frozen=True)
class Header:
    task_id: str
    challenge_id: str
    miner_hotkey: str
    model_name: str
    model_provider: str = "anthropic"
    harness_name: str = "honeminer"
    harness_version: str = __version__


def _closing(submission: bytes, cap: int | None = None) -> list[dict]:
    """The demo miner's ending. At capped rungs the echoed submission is trimmed (its sha256 stays exact)."""

    digest = _sha(submission)
    echoed = submission if cap is None else _trim_bytes(submission, cap, cap // 2)
    try:
        submission.decode("utf-8")
        utf8 = True
    except UnicodeDecodeError:
        utf8 = False
    output = canonical_json_bytes({"sha256": digest, "size_bytes": len(submission), "utf8": utf8})
    return [
        {"event_type": "tool_call", "call_id": "submission-validation-0", "tool_name": "validate_submission",
         "input_body_b64": _b64(echoed)},
        {"event_type": "tool_result", "call_id": "submission-validation-0", "output_body_b64": _b64(output),
         "is_error": False},
        {"event_type": "final_submission", "submission_sha256": digest},
    ]


def _placeholder_turn() -> list[dict]:
    """A labeled model turn for a run whose traffic was lost (keeps the schema's one-turn minimum)."""

    note = _canon({"honeminer": "no_recorded_model_traffic"})
    return [{"event_type": "model_turn", "request_body_b64": _b64(note), "response_body_b64": _b64(note),
             "generated_bytes_b64": "", "reasoning": "", "output": "", "tokens": []}]


def render(turns: list[Turn], header: Header, submission: bytes, level: Level) -> bytes:
    kept = list(enumerate(turns))
    dropped = 0
    if level.drop_oldest and len(kept) > 2:
        dropped = min(level.drop_oldest, len(kept) - 2)
        kept = [kept[0]] + kept[1 + dropped:]
    events: list[dict] = []
    seen = {"submission-validation-0", "honeminer-omitted-turns"}
    newest = {index for index, _ in kept[-KEEP_RECENT:]}
    for position, (index, turn) in enumerate(kept):
        events.extend(_render_turn(index, turn, level, index in newest, index == 0, seen))
        if position == 0 and dropped:
            note_id = "honeminer-omitted-turns"
            events.append({"event_type": "tool_call", "call_id": note_id, "tool_name": "honeminer_note",
                           "input_body_b64": _b64(_canon({"honeminer": "omitted_turns", "count": dropped}))})
            events.append({"event_type": "tool_result", "call_id": note_id,
                           "output_body_b64": _b64(b"turns omitted to fit the upload size limit"),
                           "is_error": False})
    if not any(e["event_type"] == "model_turn" for e in events):
        events = _placeholder_turn() + events
    events.extend(_closing(submission, level.cap))
    for sequence, event in enumerate(events):
        event["sequence"] = sequence
    trajectory = Trajectory.model_validate({
        "schema_version": 1, "task_id": header.task_id, "challenge_id": header.challenge_id,
        "miner_hotkey": header.miner_hotkey, "submission_sha256": _sha(submission),
        "harness_name": header.harness_name, "harness_version": header.harness_version,
        "model_provider": header.model_provider, "model_name": header.model_name, "events": events,
    })
    return serialize_trajectory(trajectory)


@dataclass(frozen=True)
class Built:
    data: bytes
    level: str
    turns: int


def _fits(data: bytes | None, limit: int) -> bool:
    return data is not None and len(data) <= limit


def _lower_bound(turns: list[Turn], level: Level) -> int:
    """Base64 size of the bodies an untrimmed rung must carry; the real log is larger."""

    raw = 0
    for position, turn in enumerate(turns):
        raw += len((turn.request_full or turn.request_delta) if position == 0 else turn.request_delta)
        raw += len(turn.condensed if level.condense else turn.response or b"")
        raw += sum(len(tool.input) + len(tool.output or b"") for tool in turn.tools)
    return raw * 4 // 3


def _try_render(turns: list[Turn], header: Header, submission: bytes, level: Level) -> bytes | None:
    """``render``, or None when this rung breaks a schema cap (a field or the event count); a smaller rung follows."""

    try:
        return render(turns, header, submission, level)
    except (ValueError, RecursionError):
        return None


def build(turns: list[Turn], header: Header, submission: bytes, max_bytes: int) -> Built:
    """The richest valid log that fits ``max_bytes``; never larger. Raises only below the floor."""

    target = max(1, int(max_bytes * TARGET_FRACTION))
    for level in LADDER:
        if not level.trim and _lower_bound(turns, level) > target:
            continue  # cannot fit: skip the render (keeps big runs fast)
        data = _try_render(turns, header, submission, level)
        if _fits(data, target):
            return Built(data, level.name, len(turns))
    base = LADDER[-1]
    # Drop oldest turns (first and last always stay): binary search the fewest drops that fit.
    low, high = 1, max(1, len(turns) - 2)
    best: bytes | None = None
    while low <= high and len(turns) > 2:
        middle = (low + high) // 2
        data = _try_render(turns, header, submission, replace(base, drop_oldest=middle, name="dropped"))
        if _fits(data, target):
            best, high = data, middle - 1
        else:
            low = middle + 1
    if best is not None:
        return Built(best, "dropped", len(turns))
    # Everything but two turns is gone: cap every text and body.
    drop = max(0, len(turns) - 2)
    for cap in (4096, 1024, 256, 64, 16, 0):
        data = _try_render(turns, header, submission, replace(base, drop_oldest=drop, cap=cap, name=f"cap{cap}"))
        if _fits(data, target):
            return Built(data, f"cap{cap}", len(turns))
    floor = minimal(header, submission)
    if _fits(floor, max_bytes):
        return Built(floor, "minimal", len(turns))
    raise TrajectoryTooLarge(f"the smallest valid work log is {len(floor)} bytes; the slot allows {max_bytes}")


def minimal(header: Header, submission: bytes, turns: list[Turn] | None = None) -> bytes:
    """The cheapest valid log: one labeled turn (the last recorded one, fully capped) and the closing events."""

    chosen = [turns[-1]] if turns else []
    level = Level("minimal", condense=True, trim=True, cap=0)
    return render([replace(t, tools=()) for t in chosen], header, submission, level)


def build_within(turns: list[Turn], header: Header, submission: bytes, max_bytes: int,
                 timeout_s: float, level: str | None = None) -> Built:
    """``build`` with a time box: if it is not done in time (or fails), the minimal log is used."""

    outcome: dict = {}

    def work():
        try:
            outcome["built"] = build(turns, header, submission, max_bytes)
        except Exception as exc:  # noqa: BLE001 - the answer must never depend on the log
            outcome["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout_s)
    built = outcome.get("built")
    if built is not None and level:
        built = replace(built, level=level)
    if built is None:
        if isinstance(outcome.get("error"), TrajectoryTooLarge):
            raise outcome["error"]
        try:
            data = minimal(header, submission, turns)
        except (ValueError, RecursionError):
            data = None
        if data is None or len(data) > max_bytes:
            data = minimal(header, submission)
        if len(data) > max_bytes:
            raise TrajectoryTooLarge(f"the smallest valid work log is {len(data)} bytes; the slot allows {max_bytes}")
        built = Built(data, "minimal-fallback", len(turns))
    assert len(built.data) <= max_bytes, "work log over the size limit"
    parse_trajectory(built.data)
    return built


def build_from_traffic(traffic: Path, header: Header, submission: bytes, max_bytes: int,
                       timeout_s: float) -> Built:
    """The whole pipeline (read, pair, fit) inside ``timeout_s`` (from ``Clock.work_log_timeout``).

    On timeout or error the labeled minimal log is used. Never returns more than ``max_bytes``; raises
    ``TrajectoryTooLarge`` only when no valid log can fit.
    """

    started = time.monotonic()
    outcome: dict = {}

    def work():
        try:
            outcome["turns"] = turns_from(read_traffic(traffic))
        except Exception as exc:  # noqa: BLE001 - the answer must never depend on the log
            outcome["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout_s)
    turns = outcome.get("turns")
    if turns is None:
        return build_within([], header, submission, max_bytes, timeout_s=0.1, level="minimal-fallback")
    return build_within(turns, header, submission, max_bytes,
                        timeout_s=max(0.1, timeout_s - (time.monotonic() - started)))
