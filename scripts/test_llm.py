"""llama.cpp models through Johan's real HTTP path, against a FAKE llama-server
(no GPU, no docker). ~2 s.

What must survive the translation, because the Bonsai shim got it wrong and
its tool eval was thrown away: tool calls paired with their results by id,
arguments as dicts, tool calls streamed in fragments stitched back whole,
logprobs for the decision layer, schema replies, cut-off replies, and
"unload" stopping the container instead of being sent as a chat.

    python test_llm.py
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_interrupt as ji     # noqa: E402
import johan_llm as jl           # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


# --- the request/response mapping -------------------------------------------
req = jl.to_openai({
    "model": "qwen36", "think": False, "stream": False, "logprobs": True, "top_logprobs": 20,
    "options": {"temperature": 0, "num_predict": 1, "num_ctx": 32768},
    "tools": [{"type": "function", "function": {"name": "files", "parameters": {}}}],
    "messages": [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "look", "images": ["QUJD"]},
                 {"role": "assistant", "content": "", "tool_calls": [
                     {"function": {"name": "files", "arguments": {"path": "a"}}},
                     {"function": {"name": "web_search", "arguments": {"query": "b"}}}]},
                 {"role": "tool", "content": "A"}, {"role": "tool", "content": "B"}]})
m = req["messages"]
check("two calls get distinct ids, results pair with them in order",
      [c["id"] for c in m[2]["tool_calls"]] == ["call_1", "call_2"]
      and m[3]["tool_call_id"] == "call_1" and m[4]["tool_call_id"] == "call_2")
check("arguments go out as JSON strings", json.loads(m[2]["tool_calls"][0]["function"]["arguments"]) == {"path": "a"})
check("an image becomes an image_url part", m[1]["content"][1]["image_url"]["url"].endswith("QUJD"))
check("num_predict -> max_tokens, think off, logprobs and tools kept",
      req["max_tokens"] == 1 and req["chat_template_kwargs"]["enable_thinking"] is False
      and req["top_logprobs"] == 20 and req["tools"])
check("a JSON-schema format becomes response_format",
      jl.to_openai({"messages": [{"role": "user", "content": "q"}], "format": {"type": "object"}})
      ["response_format"]["json_schema"]["schema"] == {"type": "object"})
check("only llama.cpp models are routed", jl.is_llm("qwen36") and not jl.is_llm("johan-agent"))

# --- a fake llama-server -----------------------------------------------------
SEEN = []
SSE = [
    {"choices": [{"delta": {"content": "Let me "}}]},
    {"choices": [{"delta": {"content": "check."}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "files", "arguments": ""}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"pa"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"/x\"}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 4480, "completion_tokens": 12}},
]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in SSE:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if body.get("logprobs"):
            out = {"choices": [{"finish_reason": "length", "message": {"content": "B"},
                                "logprobs": {"content": [{"token": "B", "logprob": -0.1, "top_logprobs": [
                                    {"token": "B", "logprob": -0.1}, {"token": "A", "logprob": -2.4}]}]}}],
                   "usage": {"prompt_tokens": 300, "completion_tokens": 1}}
        else:
            out = {"choices": [{"finish_reason": "stop", "message": {"content": "{\"title\": \"T\"}"}}],
                   "usage": {"prompt_tokens": 50, "completion_tokens": 5}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
real = (jl.BASE, jl.ensure, jl.stop)
jl.BASE = f"http://127.0.0.1:{srv.server_address[1]}"
stopped = []
jl.ensure = lambda model, should_stop=None: True
jl.stop = lambda: stopped.append(1) or True
try:
    got = []
    r = jl.chat([{"role": "user", "content": "q"}], model="qwen36", on_delta=got.append,
                tools=[{"type": "function", "function": {"name": "files"}}])
    check("streamed text arrives in pieces, in order", "".join(got) == "Let me check." and len(got) == 2)
    check("…and the returned answer holds the whole streamed text", r["message"]["content"] == "Let me check.")
    check("…a tool call streamed in fragments comes back whole, with dict arguments",
          r["message"]["tool_calls"] == [{"function": {"name": "files", "arguments": {"path": "/x"}}}])
    check("…token counts reach the context bar", r["prompt_tokens"] == 4480 and r["completion_tokens"] == 12)
    check("…the request went to llama-server as a stream with usage",
          SEEN[-1]["stream"] and SEEN[-1]["stream_options"]["include_usage"])
    r = jl.chat([{"role": "user", "content": "q"}], model="qwen36", logprobs=20, max_tokens=1)
    check("the decision layer gets top logprobs", [t["token"] for t in r["top_logprobs"]] == ["B", "A"]
          and r["cut_off"])
    r = jl.chat([{"role": "user", "content": "q"}], model="qwen36", schema={"type": "object"})
    check("a schema reply comes back as message content", json.loads(r["message"]["content"]) == {"title": "T"})
    import johan_assistant as ja
    check("release_model on a llama.cpp model stops it", ja.release_model("qwen36") and stopped)
    import johan_decide as jd
    real_chat = jl.chat
    jl.chat = lambda *a, **k: {"top_logprobs": [{"token": "B", "logprob": -0.05},
                                                {"token": "A", "logprob": -3.0}]}
    try:
        mass, _ = jd._score_once("q?", [("x", "first"), ("y", "second")], "ctx", "sys")
    finally:
        jl.chat = real_chat
    check("decide reads its letters from chat()'s logprobs", mass[1] > 0.9 and mass[0] < 0.1)
finally:
    jl.BASE, jl.ensure, jl.stop = real
    srv.shutdown()

# Two starters at once (the TUI warm-up + his first question): the second must
# WAIT for the container that is still loading, never stop it.
import threading as _th, time as _t   # noqa: E402
calls, state = [], {"served": None, "up_at": None}
real_fns = (jl._served, jl.running, jl.stop, jl.subprocess.run)
jl._served = lambda: state["served"]
jl.running = lambda timeout=2: state["up_at"] is not None and _t.monotonic() > state["up_at"]


def _fake_run(cmd, **k):
    calls.append("docker run")
    state.update(served="qwen36", up_at=_t.monotonic() + 1.0)
    return type("R", (), {"returncode": 0, "stderr": ""})()


jl.subprocess.run = _fake_run
jl.stop = lambda: calls.append("stop") or state.update(served=None, up_at=None) or True
try:
    ts = [_th.Thread(target=jl.start, args=("qwen36",)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    check("two starters at once: one docker run, no stop, model up",
          calls == ["docker run"] and jl.running())
finally:
    jl._served, jl.running, jl.stop, jl.subprocess.run = real_fns

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
