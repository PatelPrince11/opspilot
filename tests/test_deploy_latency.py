import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts import deploy_latency


class Fake(BaseHTTPRequestHandler):
    posts = 0

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        Fake.posts += 1
        if Fake.posts == 1:  # the first request is rate limited
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        assert self.headers["X-API-Key"] == "k"
        self.send_response(200)
        self.send_header("X-Investigation-Id", "abc")
        self.end_headers()
        for e in ("start", "tool_call", "diagnosis"):
            self.wfile.write(f"event: {e}\ndata: {{}}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        body = json.dumps(
            {
                "status": "done",
                "cost_usd": 0.01,
                "tool_calls": [{}, {}],
                "llm_calls": [{"cached": False}, {"cached": True}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_client_against_a_fake_server(monkeypatch, tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("OPSPILOT_API_KEY", "k")
    monkeypatch.setattr(deploy_latency, "OUT", tmp_path / "out.json")
    monkeypatch.setattr(deploy_latency.time, "sleep", lambda s: None)
    try:
        assert deploy_latency.main([host, "--commit", "c", "--app-digest", "d", "--n", "2"]) == 0
    finally:
        srv.shutdown()
    out = json.loads((tmp_path / "out.json").read_text())
    first, second = out["runs"]
    assert first["rate_limit_waits_s"] == [1] and second["rate_limit_waits_s"] == []
    assert [e for e, _ in first["events"]] == ["start", "tool_call", "diagnosis"]
    assert first["status"] == "done" and first["tool_calls"] == 2
    assert out["n"] == 2 and out["errors"] == 0 and out["cached_llm_calls"] == 2
    assert out["cost_usd"]["total"] == 0.02 and len(out["question_ids"]) == 2
