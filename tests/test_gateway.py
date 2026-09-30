import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from honeminer.gateway import Gateway, GatewayConfig, usage_from_body
from honeminer.trajectory import read_traffic

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


def make_gateway(tmp_path, upstream, auth="api_key", gate=None, traffic=None):
    config = GatewayConfig(socket_path=Path("/tmp") / f"hm-gw-{tmp_path.name[-12:]}.sock",
                           model="claude-opus-5-5", auth=auth, credential="real-secret", upstream=upstream,
                           traffic=traffic)
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


def test_recording_is_passive_and_complete(tmp_path, upstream):
    responses = {}
    for mode, traffic in (("off", None), ("on", tmp_path / "run" / "traffic.jsonl")):
        gateway = make_gateway(tmp_path, upstream, traffic=traffic)
        try:
            with client(gateway) as http:
                responses[mode] = [http.post("/v1/messages?beta=true", content=message(n=i)) for i in range(3)]
                http.post("/v1/messages/count_tokens", content=message())
                http.post("/_honeminer/gate", content=b"{}")
        finally:
            gateway.stop()
    # Claude sees exactly the same bytes and statuses with recording on.
    assert [(r.status_code, r.content) for r in responses["on"]] == [(r.status_code, r.content)
                                                                    for r in responses["off"]]
    exchanges = read_traffic(tmp_path / "run" / "traffic.jsonl")
    assert [e.seq for e in exchanges] == [0, 1, 2] and all(e.path == "/v1/messages?beta=true" for e in exchanges)
    assert [e.request for e in exchanges] == [message(n=i) for i in range(3)]
    assert all(e.response == SSE and e.status == 200 and not e.transport_error for e in exchanges)
    assert b"real-secret" not in (tmp_path / "run" / "traffic.jsonl").read_bytes()


def test_an_unreachable_upstream_is_recorded_as_a_transport_error(tmp_path):
    traffic = tmp_path / "traffic.jsonl"
    gateway = make_gateway(tmp_path, "http://127.0.0.1:9", traffic=traffic)
    try:
        with client(gateway) as http:
            assert http.post("/v1/messages", content=message()).status_code == 502
    finally:
        gateway.stop()
    (exchange,) = read_traffic(traffic)
    assert exchange.transport_error and exchange.response is None and exchange.status is None


def test_a_recording_failure_never_reaches_claude(tmp_path, upstream):
    (tmp_path / "blocker").write_text("a file where a directory should be")
    gateway = make_gateway(tmp_path, upstream, traffic=tmp_path / "blocker" / "traffic.jsonl")
    try:
        with client(gateway) as http:
            replies = [http.post("/v1/messages", content=message()) for _ in range(2)]
        assert all(r.status_code == 200 and r.content == SSE for r in replies)
    finally:
        gateway.stop()
    assert gateway.recorder.failed


def serve_upstream(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class Gzipping(BaseHTTPRequestHandler):
    """An upstream that compresses whenever the client allows it (as api.anthropic.com may)."""

    seen: list = []

    def do_POST(self):  # noqa: N802
        import gzip

        self.rfile.read(int(self.headers["Content-Length"]))
        accepted = self.headers.get("Accept-Encoding", "")
        Gzipping.seen.append(accepted)
        body = gzip.compress(SSE) if "gzip" in accepted else SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if body is not SSE:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_claude_always_gets_plain_bytes_even_when_upstream_could_compress(tmp_path):
    server, url = serve_upstream(Gzipping)
    traffic = tmp_path / "traffic.jsonl"
    gateway = make_gateway(tmp_path, url, traffic=traffic)
    try:
        with socket.socket(socket.AF_UNIX) as raw:  # a client that does not decode anything
            raw.connect(str(gateway.config.socket_path))
            body = message()
            raw.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            received = b""
            while chunk := raw.recv(65536):
                received += chunk
    finally:
        gateway.stop()
        server.shutdown()
    assert b"message_start" in received and b"content-encoding" not in received.lower()
    assert "gzip" not in Gzipping.seen[-1]
    assert gateway.usage.input_tokens == 120
    assert read_traffic(traffic)[0].response == SSE


class Breaking(BaseHTTPRequestHandler):
    """An upstream that dies halfway through the stream."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        self.wfile.write(b"event: ping\ndata: {\"type\": \"ping\"}\n\n")
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)

    def log_message(self, *args):
        pass


def test_an_upstream_dying_mid_stream_ends_the_connection_cleanly(tmp_path):
    server, url = serve_upstream(Breaking)
    gateway = make_gateway(tmp_path, url)
    try:
        with socket.socket(socket.AF_UNIX) as raw:
            raw.settimeout(10)
            raw.connect(str(gateway.config.socket_path))
            body = message()
            raw.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            received = b""
            while chunk := raw.recv(65536):  # must end (connection closed), not hang
                received += chunk
    finally:
        gateway.stop()
        server.shutdown()
    assert received.count(b"HTTP/1.1") == 1 and b"502" not in received


def test_streamed_usage_is_not_double_counted():
    sse = (b'data: {"type":"message_start","message":{"usage":{"input_tokens":100,'
           b'"cache_read_input_tokens":5000,"output_tokens":1}}}\n\n'
           b'data: {"type":"message_delta","usage":{"input_tokens":100,"cache_read_input_tokens":5000,'
           b'"output_tokens":42}}\n\n')
    assert usage_from_body(sse, True) == {"input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 42}
