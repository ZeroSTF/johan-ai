"""Models served by llama.cpp instead of Ollama, and their lifecycle.

Why: Qwen3.6-35B-A3B (a mixture of experts) is fast on this 6 GB card only
with llama.cpp's --n-cpu-moe, which keeps most experts in RAM; Ollama can't do
that. Measured 2026-10-23 with Johan's real prompt: new chat 1.1 s, writing
28 tok/s, reading ~78 tok/s, 4.5 GB VRAM at n_cpu_moe=34; and on the hard
eval's deciding cases 7/8 where the 4B averages ~3.7/8.

The rest of Johan still speaks Ollama's /api/chat dialect. The translation
lives at the one HTTP choke point every model call already goes through
(johan_interrupt.ollama_lines), so chat, decide, plan, learn and build need no
changes, and esc still cancels mid-request. A model is a llama.cpp model when
its name is in MODELS; anything else goes to Ollama as before.

Lifecycle, which Ollama used to do for us:
  start()   docker run the CUDA build (Ubuntu 24.04 container — the release
            needs glibc 2.38, Pop!_OS 22.04 has 2.35); waits for /health
  stop()    docker stop: frees the GPU and RAM — on quit, before drawing,
            before /refresh's Stage 2 (granite still runs in Ollama), and in
            johan-pause (GameMode)
  ensure()  start if not running — a call to a stopped model starts it
"""
import json
import os
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path.home() / "johan"
LLAMA = ROOT / "llamacpp"
NV = ROOT / "venv-comfy/lib/python3.10/site-packages/nvidia"   # CUDA 12.8 runtime libs
CONTAINER = "johan-llm"
PORT = 8092
BASE = f"http://127.0.0.1:{PORT}"
BUILD = LLAMA / "cuda/llama-b11829"

MODELS = {
    # n_cpu_moe 34: 4.5 GB VRAM. 32 measured slightly faster but left ~0.8 GB
    # for the desktop and a browser — too tight. 26 didn't fit at all.
    "qwen36": {"gguf": "Qwen3.6-35B-A3B-APEX-I-Mini.gguf", "mmproj": "mmproj.gguf",
               "n_cpu_moe": 34, "smart": True},
}


def smart(model=None):
    """A model big enough not to need the 4B's crutches — the extra calibrated
    checks that steer it (each one is a fresh ~5-7 s prompt on a llama.cpp
    MoE, not ~0.2 s), and the 9-step limit per message."""
    if model is None:
        import johan_assistant as ja
        model = ja.MODEL
    return bool(MODELS.get(str(model).split(":")[0], {}).get("smart"))


def is_llm(model):
    return str(model or "").split(":")[0] in MODELS


def installed(model):
    m = MODELS.get(str(model).split(":")[0])
    return bool(m) and (LLAMA / "models" / m["gguf"]).is_file()


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

import threading

_STATE = {"model": None}
# The TUI's warm-up and his first question both called start() at once; the
# second saw a container still loading, took it for broken and stopped it —
# killing the model mid-load ("(no answer)" in 3 s). One starter at a time, and
# a container that is still loading the right model is waited for, not replaced.
_START = threading.Lock()


def running(timeout=2):
    try:
        return requests.get(f"{BASE}/health", timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def _served():
    """Which model the running container serves (its name label)."""
    r = subprocess.run(["docker", "inspect", "-f", '{{index .Config.Labels "johan-llm"}}', CONTAINER],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def start(model, wait=600, should_stop=None):
    """Serve `model`. Returns True once /health answers."""
    with _START:
        return _start(model, wait, should_stop)


def _wait_healthy(name, wait, should_stop):
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if should_stop and should_stop():
            return False
        if running():
            _STATE["model"] = name
            return True
        if _served() is None:       # the container exited
            raise RuntimeError(f"{name} stopped while loading (out of VRAM or RAM?)")
        time.sleep(1)
    raise RuntimeError(f"{name} didn't answer within {wait} s")


def _start(model, wait, should_stop):
    name = str(model).split(":")[0]
    m = MODELS[name]
    served = _served()
    if served == name:
        return _wait_healthy(name, wait, should_stop)     # up, or still loading
    if served is not None:
        stop()
    L = "/usr/lib/x86_64-linux-gnu"
    cmd = ["docker", "run", "-d", "--rm", "--name", CONTAINER, "--label", f"johan-llm={name}",
           "--network", "host", "--cpuset-cpus", "8-15",
           "--device", "/dev/nvidia0", "--device", "/dev/nvidiactl",
           "--device", "/dev/nvidia-uvm", "--device", "/dev/nvidia-uvm-tools",
           "-v", f"{L}/libcuda.so.1:{L}/libcuda.so.1:ro",
           "-v", f"{L}/libnvidia-ptxjitcompiler.so.1:{L}/libnvidia-ptxjitcompiler.so.1:ro",
           "-v", f"{L}/libgomp.so.1:{L}/libgomp.so.1:ro",
           "-v", f"{BUILD}:/llama:ro", "-v", f"{NV}:/nv:ro", "-v", f"{LLAMA / 'models'}:/models:ro",
           # the card has no precompiled kernels in the release: the driver
           # JIT-compiles them once (~75 s) and keeps them here
           "-v", f"{LLAMA / 'cuda-cache'}:/cache",
           "-e", "LD_LIBRARY_PATH=/llama:/nv/cuda_runtime/lib:/nv/cublas/lib",
           "-e", "CUDA_CACHE_PATH=/cache", "-e", "CUDA_CACHE_MAXSIZE=2147483648",
           "ubuntu:24.04", "/llama/llama-server", "-m", f"/models/{m['gguf']}",
           "-ngl", "99", "--n-cpu-moe", str(m["n_cpu_moe"]), "-c", "32768",
           "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja", "-t", "8", "-np", "1",
           # The prompt cache lives in RAM, 8 GB by default — a long build
           # filled it and the PC ran out of memory. 2 GB keeps new chats fast.
           "--cache-ram", "2048",
           # Thinking off for every client (Johan asks per request; outside
           # agents like OpenCode don't): at ~28 tok/s it doubles a reply.
           "--chat-template-kwargs", '{"enable_thinking": false}',
           "--host", "127.0.0.1", "--port", str(PORT)]
    if m.get("mmproj") and (LLAMA / "models" / m["mmproj"]).is_file():
        # Vision on the CPU: on a GTX 16 the projector's GPU compute buffer
        # doesn't fit beside the model (measured with Bonsai, 2026-10-19).
        cmd[-4:-4] = ["--mmproj", f"/models/{m['mmproj']}", "--no-mmproj-offload"]
    (LLAMA / "cuda-cache").mkdir(exist_ok=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"couldn't start {name}: {r.stderr.strip()[-300:]}")
    return _wait_healthy(name, wait, should_stop)


def stop():
    """Unload: frees VRAM and the RAM the experts sit in. Never raises."""
    try:
        subprocess.run(["docker", "stop", "-t", "5", CONTAINER], capture_output=True, timeout=30)
    except Exception:
        pass
    _STATE["model"] = None
    return True


def ensure(model, should_stop=None):
    if _STATE.get("model") == str(model).split(":")[0] and running(timeout=1):
        return True
    return start(model, should_stop=should_stop)


def status():
    """For system_status: what is loaded."""
    served = _served()
    return {"served": served, "up": bool(served) and running()}


# --------------------------------------------------------------------------
# Ollama /api/chat <-> llama-server /v1/chat/completions
# --------------------------------------------------------------------------

def to_openai(body):
    """Ollama chat request -> OpenAI chat request (what llama-server speaks)."""
    msgs, pending, n = [], [], 0
    for m in body.get("messages") or []:
        role = m.get("role")
        out = {"role": role, "content": m.get("content") or ""}
        if m.get("images") and role == "user":
            out["content"] = [{"type": "text", "text": m.get("content") or ""}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}}
                for b in m["images"]]
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                n += 1
                cid = f"call_{n}"
                fn = c.get("function") or {}
                args = fn.get("arguments")
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
                pending.append(cid)
            out["tool_calls"] = calls
        if role == "tool":
            # Ollama pairs tool results with calls by order; OpenAI by id.
            out["tool_call_id"] = pending.pop(0) if pending else f"call_{n or 1}"
        msgs.append(out)
    opts = body.get("options") or {}
    req = {"messages": msgs, "stream": bool(body.get("stream")), "cache_prompt": True,
           "temperature": opts.get("temperature", 0.3),
           "chat_template_kwargs": {"enable_thinking": bool(body.get("think", False))}}
    if req["stream"]:
        req["stream_options"] = {"include_usage": True}
    if opts.get("num_predict"):
        req["max_tokens"] = int(opts["num_predict"])
    if "seed" in opts:
        req["seed"] = opts["seed"]
    if body.get("tools"):
        req["tools"] = body["tools"]
    fmt = body.get("format")
    if isinstance(fmt, dict):
        req["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": fmt}}
    elif fmt == "json":
        req["response_format"] = {"type": "json_object"}
    if body.get("logprobs"):
        req["logprobs"] = True
        req["top_logprobs"] = int(body.get("top_logprobs") or 1)
    return req


def _args(a):
    try:
        return json.loads(a) if isinstance(a, str) else (a or {})
    except ValueError:
        return {}


def from_openai(r, model):
    """OpenAI chat reply -> Ollama chat reply."""
    ch = (r.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    out = {"role": "assistant", "content": msg.get("content") or ""}
    calls = [{"function": {"name": (c.get("function") or {}).get("name", ""),
                           "arguments": _args((c.get("function") or {}).get("arguments"))}}
             for c in msg.get("tool_calls") or []]
    if calls:
        out["tool_calls"] = calls
    usage = r.get("usage") or {}
    res = {"model": model, "message": out, "done": True,
           "done_reason": "length" if ch.get("finish_reason") == "length" else "stop",
           "prompt_eval_count": usage.get("prompt_tokens", 0),
           "eval_count": usage.get("completion_tokens", 0)}
    lp = (ch.get("logprobs") or {}).get("content")
    if lp:
        res["logprobs"] = [{"token": t.get("token", ""), "logprob": t.get("logprob", 0.0),
                            "top_logprobs": [{"token": x.get("token", ""), "logprob": x.get("logprob", 0.0)}
                                             for x in t.get("top_logprobs") or []]} for t in lp]
    return res


class StreamTranslator:
    """OpenAI server-sent events -> Ollama-style stream chunks. Tool calls
    arrive in fragments (name first, arguments in pieces) and are handed
    over whole in the final chunk, the way Ollama does it."""

    def __init__(self, model):
        self.model, self.calls, self.finish, self.usage = model, {}, None, {}

    def feed(self, line):
        """One raw line -> a list of Ollama chunks (often empty)."""
        line = line.strip()
        if not line.startswith(b"data:" if isinstance(line, bytes) else "data:"):
            return []
        data = line[5:].strip()
        if data in (b"[DONE]", "[DONE]"):
            return [self.final()]
        d = json.loads(data)
        if d.get("error"):
            return [{"error": str(d["error"])[:400]}]
        if d.get("usage"):
            self.usage = d["usage"]
        out = []
        for ch in d.get("choices") or []:
            delta = ch.get("delta") or {}
            for tc in delta.get("tool_calls") or []:
                c = self.calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                c["name"] += fn.get("name") or ""
                c["arguments"] += fn.get("arguments") or ""
            if delta.get("content"):
                out.append({"model": self.model, "done": False,
                            "message": {"role": "assistant", "content": delta["content"]}})
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]
        return out

    def final(self):
        msg = {"role": "assistant", "content": ""}
        if self.calls:
            msg["tool_calls"] = [{"function": {"name": c["name"], "arguments": _args(c["arguments"])}}
                                 for _, c in sorted(self.calls.items())]
        return {"model": self.model, "done": True, "message": msg,
                "done_reason": "length" if self.finish == "length" else "stop",
                "prompt_eval_count": self.usage.get("prompt_tokens", 0),
                "eval_count": self.usage.get("completion_tokens", 0)}


def chat_url():
    return f"{BASE}/v1/chat/completions"


# --------------------------------------------------------------------------
# the one call every part of Johan makes
# --------------------------------------------------------------------------

OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"   # until Ollama is retired


def chat(messages, *, model=None, tools=None, schema=None, stream=False, on_delta=None,
         max_tokens=None, temperature=0.3, logprobs=0, keep_alive=None, num_ctx=None,
         timeout=180):
    """One model call. Callers say WHAT they want; only this function knows
    how the server wants it said. Returns
        {"message": {"role", "content", "tool_calls": [{"function": {"name", "arguments"}}]},
         "cut_off": bool, "prompt_tokens": int, "completion_tokens": int,
         "top_logprobs": [{"token", "logprob"}, ...]}      # first token, when asked
    Streams text through `on_delta` when given. esc raises
    johan_interrupt.Interrupted (from the HTTP wait), as before."""
    import johan_interrupt as ji
    import johan_assistant as ja
    model = model or ja.MODEL
    if is_llm(model):
        ensure(model, should_stop=ji.stop_requested)
        req = _request(messages, tools, schema, bool(on_delta or stream), max_tokens,
                       temperature, logprobs)
        if not req["stream"]:
            d = json.loads(b"".join(ji._raw_lines(chat_url(), req, timeout)))
            if d.get("error"):
                raise RuntimeError(f"llama-server: {str(d['error'])[:400]}")
            return _result(from_openai(d, model))
        tr = StreamTranslator(model)
        last, text = None, []
        for line in ji._raw_lines(chat_url(), req, timeout):
            for c in tr.feed(line):
                if c.get("error"):
                    raise RuntimeError(f"llama-server: {c['error']}")
                piece = (c.get("message") or {}).get("content")
                if piece:
                    text.append(piece)
                    if on_delta:
                        on_delta(piece)
                if c.get("done"):
                    last = c
        last = last or tr.final()
        # The streamed pieces ARE the answer: the final chunk only carries tool
        # calls and counts. Returning it alone gave every streamed reply "".
        last = dict(last, message=dict(last.get("message") or {}, content="".join(text)))
        return _result(last, streamed=True)
    # Ollama (the 4B) — goes when Ollama does.
    payload = {"model": model, "messages": messages, "think": False,
               "stream": bool(on_delta or stream),
               "keep_alive": keep_alive or ja.KEEP_ALIVE,
               "options": {"temperature": temperature, "num_ctx": num_ctx or ja.NUM_CTX,
                           **({"num_predict": max_tokens} if max_tokens else {})}}
    if tools:
        payload["tools"] = tools
    if schema is not None:
        payload["format"] = schema
    if logprobs:
        payload.update(logprobs=True, top_logprobs=int(logprobs))
    url = ja.OLLAMA_CHAT        # johan_assistant's, so tests can point it at a fake server
    if not payload["stream"]:
        return _result(ji.ollama_post(url, payload, timeout=timeout))
    content, calls, last = [], [], {}
    for c in ji.ollama_lines(url, payload, idle_timeout=timeout):
        if c.get("error"):
            raise RuntimeError(f"Ollama: {c['error']}")
        msg = c.get("message") or {}
        if msg.get("content"):
            content.append(msg["content"])
            if on_delta:
                on_delta(msg["content"])
        calls.extend(msg.get("tool_calls") or [])
        if c.get("done"):
            last = c
    last = dict(last, message={"role": "assistant", "content": "".join(content),
                               **({"tool_calls": calls} if calls else {})})
    return _result(last, streamed=True)


def _request(messages, tools, schema, stream, max_tokens, temperature, logprobs):
    return to_openai({"messages": messages, "tools": tools, "format": schema, "stream": stream,
                      "think": False, "logprobs": bool(logprobs), "top_logprobs": logprobs,
                      "options": {"temperature": temperature,
                                  **({"num_predict": max_tokens} if max_tokens else {})}})


def _result(body, streamed=False):
    msg = dict(body.get("message") or {"role": "assistant", "content": ""})
    msg.setdefault("role", "assistant")
    if not msg.get("tool_calls"):
        msg.pop("tool_calls", None)
    return {"message": msg, "cut_off": body.get("done_reason") == "length",
            "prompt_tokens": body.get("prompt_eval_count", 0),
            "completion_tokens": body.get("eval_count", 0),
            "top_logprobs": ((body.get("logprobs") or [{}])[0].get("top_logprobs") or [])}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        print(start(sys.argv[2] if len(sys.argv) > 2 else "qwen36"))
    elif cmd == "stop":
        print(stop())
    else:
        print(json.dumps(status()))
