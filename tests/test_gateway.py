import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from honeminer.gateway import Gateway, GatewayConfig, usage_from_body

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "honeminer" / "kit_templates"))
import forwarder  # noqa: E402

SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":120,'
    b'"cache_read_input_tokens":30,"output_tokens":1}}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":42}}\n\n'
)


class Upstream(BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, dict(self.headers.items()), body))
        self.send_response(Upstream.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(SSE)))
        self.end_headers()
        self.wfile.write(SSE)

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Upstream.seen, Upstream.status = [], 200
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make_gateway(tmp_path, upstream, auth="api_key", gate=None):
    config = GatewayConfig(socket_path=Path("/tmp") / f"hm-gw-{tmp_path.name[-12:]}.sock",
                           model="claude-opus-5-5", auth=auth, credential="real-secret", upstream=upstream)
    gateway = Gateway(config, gate)
    gateway.start()
    return gateway


def client(gateway):
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(gateway.config.socket_path)), base_url="http://sandbox")


def message(model="claude-opus-5-5", **extra):
    return json.dumps({"model": model, "max_tokens": 10, "messages": [], **extra}).encode()


def test_passes_bytes_through_and_injects_the_api_key(tmp_path, upstream):
    gateway = make_gateway(tmp_path, upstream)
    try:
        with client(gateway) as http:
            response = http.post("/v1/messages", content=message(),
                                 headers={"x-api-key": "dummy", "authorization": "Bearer dummy"})
        assert response.status_code == 200 and response.content == SSE
        path, headers, body = Upstream.seen[0]
        assert body == message() and headers["x-api-key"] == "real-secret"
        assert "dummy" not in json.dumps(headers)
        assert gateway.usage.input_tokens == 120 and gateway.usage.output_tokens == 42
        assert gateway.usage.cache_read_tokens == 30 and gateway.usage.requests == 1
    finally:
        gateway.stop()


def test_oauth_uses_bearer_and_adds_the_beta(tmp_path, upstream):
    gateway = make_gateway(tmp_path, upstream, auth="oauth")
    try:
        with client(gateway) as http:
            http.post("/v1/messages", content=message(), headers={"anthropic-beta": "tools-1"})
        _, headers, _ = Upstream.seen[0]
        assert headers["authorization"] == "Bearer real-secret"
        assert headers["anthropic-beta"] == "tools-1,oauth-2025-04-20"
    finally:
        gateway.stop()


def test_other_models_and_server_tools_are_refused(tmp_path, upstream):
    gateway = make_gateway(tmp_path, upstream)
    try:
        with client(gateway) as http:
            wrong = http.post("/v1/messages", content=message(model="claude-haiku-4-5"))
            web = http.post("/v1/messages", content=message(tools=[{"type": "web_search_20250305"}]))
            custom = http.post("/v1/messages", content=message(tools=[{"name": "Bash", "input_schema": {}}]))
            counted = http.post("/v1/messages/count_tokens", content=message(model="anything"))
        assert wrong.status_code == 400 and "claude-haiku-4-5" in wrong.text
        assert web.status_code == 400 and "server-side tool" in web.text
        assert custom.status_code == 200 and counted.status_code == 200
        assert len(gateway.usage.rejected) == 2
    finally:
        gateway.stop()


def test_rate_limits_are_counted(tmp_path, upstream):
    Upstream.status = 429
    gateway = make_gateway(tmp_path, upstream)
    try:
        with client(gateway) as http:
            assert http.post("/v1/messages", content=message()).status_code == 429
        assert gateway.usage.rate_limited == 1 and gateway.usage.output_tokens == 0
    finally:
        gateway.stop()


def test_gate_endpoint_calls_the_gate(tmp_path, upstream):
    calls = []
    gateway = make_gateway(tmp_path, upstream, gate=lambda r: calls.append(r) or {"decision": "block", "reason": "x"})
    try:
        with client(gateway) as http:
            answer = http.post("/_honeminer/gate", json={"session_id": "s1"}).json()
        assert answer == {"decision": "block", "reason": "x"} and calls == [{"session_id": "s1"}]
    finally:
        gateway.stop()


def test_forwarder_relays_tcp_to_the_gateway_socket(tmp_path, upstream):
    gateway = make_gateway(tmp_path, upstream)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    ready = threading.Event()
    threading.Thread(target=forwarder.serve, args=(port, str(gateway.config.socket_path), ready),
                     daemon=True).start()
    ready.wait(5)
    try:
        response = httpx.post(f"http://127.0.0.1:{port}/v1/messages", content=message())
        assert response.status_code == 200 and response.content == SSE
    finally:
        gateway.stop()


def test_usage_parsing_handles_json_and_garbage():
    assert usage_from_body(b'{"usage": {"input_tokens": 5, "output_tokens": 7}}', False) == {
        "input_tokens": 5, "output_tokens": 7}
    assert usage_from_body(b"not json", False) == {}
    assert usage_from_body(b"data: {broken\n", True) == {}
