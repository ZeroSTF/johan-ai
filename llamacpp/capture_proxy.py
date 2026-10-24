"""A fake OpenAI-compatible server that records what a client sends and
replies "ok" — to see exactly what OpenCode's first request contains (and what
changes between sessions) without loading the model.

    python capture_proxy.py OUT_DIR [port]      # default port 8099
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8099


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"data": [{"id": "qwen36", "object": "model"}], "object": "list"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        (OUT / f"{time.time():.3f}.json").write_text(json.dumps(body, indent=1))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in ({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}]},
                       {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                       {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                 "total_tokens": 2}}):
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json({"id": "x", "object": "chat.completion", "model": "qwen36",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
