"""Host gateway: the sandbox's only way out.

It listens on a unix socket that is bind-mounted into the agent container (the
in-container forwarder relays 127.0.0.1:8080 to it) and:

- passes Anthropic API requests through to the upstream, injecting the real
  credential (the container only holds a dummy token);
- enforces one model per solve and refuses server-side tools (web search/fetch),
  which would work even without container network;
- counts tokens (from message usage) and rate-limit answers for the summary line;
- serves ``/_honeminer/gate`` for the Stop hook;
- records each model exchange (request and response bytes) to ``traffic.jsonl`` for the work log. Recording is
  passive: a record is queued only after the response has fully reached Claude, and a background thread writes
  it, so Claude never waits on it and a write failure never touches the run.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import socket
import socketserver
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import httpx

GATE_PATH = "/_honeminer/gate"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    rate_limited: int = 0
    rejected: list[str] = field(default_factory=list)

    def add(self, usage: dict) -> None:
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        self.cache_write_tokens += int(usage.get("cache_creation_input_tokens") or 0)


@dataclass(frozen=True)
class GatewayConfig:
    socket_path: Path
    model: str
    auth: str  # "api_key" | "oauth"
    credential: str
    upstream: str = "https://api.anthropic.com"
    traffic: Path | None = None  # where model exchanges are recorded for the work log; None = not recorded


GateHandler = Callable[[dict], dict]
log = logging.getLogger(__name__)


def is_model_call(path: str) -> bool:
    return path.startswith("/v1/messages") and not path.startswith("/v1/messages/count_tokens")


class TrafficRecorder:
    """Appends exchanges to a JSONL file from its own thread; ``put`` never blocks and never raises."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.failed = False
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._write, daemon=True, name="honeminer-traffic")
        self._thread.start()

    def put(self, seq: int, path: str, status: int | None, request: bytes, response: bytes | None,
            transport_error: bool) -> None:
        self._queue.put((seq, path, status, request, response, transport_error))

    def _write(self) -> None:
        from honeminer.trajectory import record_line

        handle = None
        while True:
            item = self._queue.get()
            if item is None:
                break
            if self.failed:
                continue
            try:
                if handle is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    handle = self.path.open("a", encoding="utf-8")
                handle.write(record_line(*item))
                handle.flush()
            except Exception as exc:  # noqa: BLE001 - the work log must never affect the run
                self.failed = True
                log.warning("traffic recording stopped: %s", exc)
        if handle is not None:
            handle.close()

    def close(self, timeout_s: float = 30) -> None:
        self._queue.put(None)
        self._thread.join(timeout_s)


def usage_from_body(body: bytes, streamed: bool) -> dict:
    """Sum the usage reported in a Messages response (JSON or SSE)."""

    total: dict[str, int] = {}
    if not streamed:
        try:
            usage = json.loads(body).get("usage") or {}
        except (ValueError, AttributeError):
            return total
        return {k: v for k, v in usage.items() if isinstance(v, int)}
    for line in body.splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        usage = (event.get("message") or {}).get("usage") if event.get("type") == "message_start" else None
        if event.get("type") == "message_delta":
            usage = event.get("usage")
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = value if key == "output_tokens" else total.get(key, 0) + value
    return total


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Gateway:
    def __init__(self, config: GatewayConfig, gate: GateHandler | None = None) -> None:
        self.config = config
        self.gate = gate or (lambda request: {"decision": "allow"})
        self.usage = Usage()
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=httpx.Timeout(connect=30, read=900, write=60, pool=30))
        self._server: _UnixServer | None = None
        self._seq = itertools.count()
        self.recorder = TrafficRecorder(config.traffic) if config.traffic is not None else None

    def reject(self, reason: str) -> None:
        with self._lock:
            self.usage.rejected.append(reason)

    def check_request(self, path: str, body: bytes) -> str | None:
        """Why a model request must not be forwarded, or None."""

        if not path.startswith("/v1/messages") or path.startswith("/v1/messages/count_tokens"):
            return None
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return "request body is not JSON"
        if payload.get("model") != self.config.model:
            return f"model {payload.get('model')!r} is not the configured {self.config.model!r}"
        for tool in payload.get("tools") or []:
            kind = tool.get("type") if isinstance(tool, dict) else None
            if kind not in (None, "custom"):
                return f"server-side tool {kind!r} is not allowed"
        return None

    def upstream_headers(self, headers: dict[str, str]) -> dict[str, str]:
        out = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
        for key in list(out):
            if key.lower() in ("authorization", "x-api-key"):
                del out[key]
        if self.config.auth == "api_key":
            out["x-api-key"] = self.config.credential
        else:
            out["authorization"] = f"Bearer {self.config.credential}"
            betas = [b for b in (out.pop("anthropic-beta", "") or "").split(",") if b.strip()]
            if OAUTH_BETA not in betas:
                betas.append(OAUTH_BETA)
            out["anthropic-beta"] = ",".join(betas)
        return out

    def _handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def address_string(self):  # unix sockets have no client address
                return "sandbox"

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _proxy(self) -> None:
                body = self._body()
                if self.path.split("?")[0] == GATE_PATH:
                    try:
                        request = json.loads(body or b"{}")
                    except ValueError:
                        request = {}
                    self._json(200, gateway.gate(request))
                    return
                problem = gateway.check_request(self.path, body)
                if problem:
                    gateway.reject(problem)
                    self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                 "message": f"honeminer gateway: {problem}"}})
                    return
                headers = gateway.upstream_headers(dict(self.headers.items()))
                url = gateway.config.upstream.rstrip("/") + self.path
                record = gateway.recorder is not None and is_model_call(self.path)
                seq = next(gateway._seq) if record else 0
                status, captured, failed = None, None, True
                try:
                    with gateway._client.stream(self.command, url, headers=headers, content=body) as response:
                        status = response.status_code
                        self.send_response(response.status_code)
                        for key, value in response.headers.items():
                            if key.lower() not in HOP_BY_HOP and key.lower() != "content-encoding":
                                self.send_header(key, value)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        captured = bytearray()
                        for chunk in response.iter_raw():
                            if not chunk:
                                continue
                            captured.extend(chunk)
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                        # Count before the final chunk: once Claude sees the end, the counts are already final.
                        streamed = "text/event-stream" in response.headers.get("content-type", "")
                        with gateway._lock:
                            gateway.usage.requests += 1
                            if response.status_code == 429:
                                gateway.usage.rate_limited += 1
                            elif self.path.startswith("/v1/messages") and response.status_code == 200:
                                gateway.usage.add(usage_from_body(bytes(captured), streamed))
                        self.wfile.write(b"0\r\n\r\n")
                        failed = False
                except httpx.HTTPError as exc:
                    self._json(502, {"type": "error", "error": {"type": "api_error",
                                                                 "message": f"honeminer gateway: {exc}"}})
                finally:
                    if record:  # after Claude has the whole response; a cut-off stream is a transport error
                        gateway.recorder.put(seq, self.path, None if failed else status, body,
                                             None if failed or captured is None else bytes(captured), failed)

            do_POST = _proxy
            do_GET = _proxy
            do_PUT = _proxy
            do_DELETE = _proxy

        return Handler

    def start(self) -> None:
        path = self.config.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self._server = _UnixServer(str(path), self._handler())
        os.chmod(path, 0o666)
        threading.Thread(target=self._server.serve_forever, daemon=True, name="honeminer-gateway").start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._client.close()
        self.config.socket_path.unlink(missing_ok=True)
        if self.recorder is not None:
            self.recorder.close()


def connect_unix(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    return sock
