"""Read OpenCode's setup into the model before his first message.

OpenCode's first request is ~13,450 tokens (its prompt, 22 tool schemas, the
AGENTS.md persona) and this box reads a new prompt at ~65 tok/s — 3.5 minutes
before the first word. The text is identical between sessions except an <env>
block (working folder, is-it-a-git-repo, today's date; measured 2026-10-24).

So `jo` runs this in the background, from the same folder, while the UI opens:
  1. a dry run of `opencode run` against a fake server (capture_proxy.py) —
     no model, ~10-20 s — records the EXACT request OpenCode will send from
     this folder today;
  2. that request's system prompt + tools (no user message) goes to the real
     llama-server with max_tokens 1, so the model has read everything up to
     his first message when he sends it. If he types first, his request waits
     for the read to finish — never slower than without this.
The dry run uses throwaway data/state dirs, so it never shows up in his
OpenCode session list; config and AGENTS.md are the real ones (they're part
of the prompt).

    python warmup.py            # from the folder `jo` runs in
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

HOME = Path.home()
NODE = HOME / ".nvm/versions/node/v24.18.0/bin"
PROXY = HOME / "johan/llamacpp/capture_proxy.py"
PY = HOME / "johan/venv-embed/bin/python3"
LLM = "http://127.0.0.1:8092"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def capture(cwd):
    tmp = Path(tempfile.mkdtemp(prefix="jo-warmup-"))
    port = free_port()
    proxy = subprocess.Popen([str(PY), str(PROXY), str(tmp / "req"), str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        cfg = json.load(open(HOME / ".config/opencode/opencode.json"))
        cfg["provider"]["llama.cpp"]["options"]["baseURL"] = f"http://127.0.0.1:{port}/v1"
        (tmp / "cfg.json").write_text(json.dumps(cfg))
        env = {**os.environ, "PATH": f"{NODE}:{os.environ['PATH']}", "OPENCODE_DISABLE_MODELS_FETCH": "1",
               "OPENCODE_CONFIG": str(tmp / "cfg.json"),
               "XDG_DATA_HOME": str(tmp / "data"), "XDG_STATE_HOME": str(tmp / "state"),
               "XDG_CACHE_HOME": str(tmp / "cache")}
        for _ in range(20):                         # the fake server must be up first
            try:
                if requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=1).ok:
                    break
            except requests.RequestException:
                time.sleep(0.25)
        subprocess.run(["opencode", "run", "x"], cwd=cwd, env=env, timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for f in sorted((tmp / "req").glob("*.json")):
            body = json.loads(f.read_text())
            if body.get("tools"):
                return body
        return None
    finally:
        proxy.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    t0 = time.monotonic()
    body = capture(os.getcwd())
    if not body:
        print("warmup: couldn't capture OpenCode's request", file=sys.stderr)
        return 1
    system = [m for m in body["messages"] if m.get("role") == "system"]
    req = {"messages": system, "tools": body["tools"], "tool_choice": body.get("tool_choice", "auto"),
           "max_tokens": 1, "stream": False, "cache_prompt": True}
    r = requests.post(f"{LLM}/v1/chat/completions", json=req, timeout=900)
    t = (r.json().get("timings") or {}) if r.ok else {}
    print(f"warmup: read {t.get('prompt_n', '?')} tokens in {time.monotonic() - t0:.0f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
