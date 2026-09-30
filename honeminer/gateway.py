"""Host gateway: the sandbox's only way out.

It listens on a unix socket that is bind-mounted into the agent container (the
in-container forwarder relays 127.0.0.1:8080 to it) and:

- passes Anthropic API requests through to the upstream, injecting the real
  credential (the container only holds a dummy token);
- enforces one model per solve and refuses server-side tools (web search/fetch),
  which would work even without container network;
- counts tokens (from message usage) and rate-limit answers for the summary line;
- serves ``/_honeminer/gate`` for the Stop hook.

The work log (trajectory) is deferred, so bodies are not recorded.
"""

from __future__ import annotations

import json
import os
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


GateHandler = Callable[[dict], dict]


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
                try:
                    with gateway._client.stream(self.command, url, headers=headers, content=body) as response:
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
                        self.wfile.write(b"0\r\n\r\n")
                        streamed = "text/event-stream" in response.headers.get("content-type", "")
                        with gateway._lock:
                            gateway.usage.requests += 1
                            if response.status_code == 429:
                                gateway.usage.rate_limited += 1
                            elif self.path.startswith("/v1/messages") and response.status_code == 200:
                                gateway.usage.add(usage_from_body(bytes(captured), streamed))
                except httpx.HTTPError as exc:
                    self._json(502, {"type": "error", "error": {"type": "api_error",
                                                                 "message": f"honeminer gateway: {exc}"}})

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


def connect_unix(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    return sock
