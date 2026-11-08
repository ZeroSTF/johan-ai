"""Read OpenCode's setup into the model before his first message.

OpenCode's first request is ~13,450 tokens (its prompt, 22 tool schemas, the
AGENTS.md persona) and this box reads a new prompt at ~65 tok/s — 3.5 minutes
before the first word. The text is identical between sessions except an <env>
block (working folder, is-it-a-git-repo, today's date; measured 2026-10-24).

So `jo` runs this in the background, from the same folder, while the UI opens:
  1. a dry run of `opencode serve` + one message against a fake server (capture_proxy.py) —
     no model, ~10-20 s — records the EXACT request OpenCode will send from
     this folder today;
  2. that request's system prompt + tools (no user message) goes to the real
     llama-server with max_tokens 1, so the model has read everything up to
     his first message when he sends it. If he types first, his request waits
     for the read to finish — never slower than without this.
  3. that read is saved to disk (llamacpp/slots, ~300 MB); the next jo from
     the same folder loads it in ~5 s instead of re-reading (~60 s), as long as
     the prompt is unchanged (date, config, AGENTS.md).
The dry run gets an empty stdin: `opencode run` read a non-terminal stdin to
the end, so an open one hung it until the 180 s timeout.
The dry run uses throwaway data/state dirs, so it never shows up in his
OpenCode session list; config and AGENTS.md are the real ones (they're part
of the prompt).

    python warmup.py            # from the folder `jo` runs in
"""
import hashlib
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
# The model's memory of a read prompt is saved here (llama-server --slot-save-path,
# mounted at /slots). One file per folder; a sidecar holds a hash of the prompt it
# holds, because the prompt changes with the date and OpenCode's config.
SLOTS = HOME / "johan/llamacpp/slots"


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
        # `opencode serve`, not `opencode run`: run leaves out the `question` tool
        # (nobody can answer it), so its request differed from the interface's at
        # 43% and his first message re-read ~5,400 tokens (2026-11-08).
        sp = free_port()
        srv = subprocess.Popen(["opencode", "serve", "--port", str(sp)], cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            api, q = f"http://127.0.0.1:{sp}", {"directory": cwd}
            for _ in range(240):
                try:
                    requests.get(f"{api}/path", params=q, timeout=1)
                    break
                except requests.RequestException:
                    time.sleep(0.25)
            sid = requests.post(f"{api}/session", params=q, json={}, timeout=30).json()["id"]
            requests.post(f"{api}/session/{sid}/message", params=q, timeout=60,
                          json={"parts": [{"type": "text", "text": "x"}]})
        finally:
            srv.kill()
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
    key = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()[:16]
    name = "jo-" + hashlib.sha256(os.getcwd().encode()).hexdigest()[:12]
    saved, sidecar = SLOTS / f"{name}.bin", SLOTS / f"{name}.key"
    restored = False
    if saved.exists() and sidecar.exists() and sidecar.read_text().strip() == key:
        try:
            restored = requests.post(f"{LLM}/slots/0?action=restore", json={"filename": f"{name}.bin"},
                                     timeout=120).ok
        except requests.RequestException:
            pass
    t1 = time.monotonic()
    r = requests.post(f"{LLM}/v1/chat/completions", json=req, timeout=900)
    t = (r.json().get("timings") or {}) if r.ok else {}
    print(f"warmup: {'restored, ' if restored else ''}read {t.get('prompt_n', '?')} tokens in "
          f"{time.monotonic() - t1:.0f} s ({time.monotonic() - t0:.0f} s total)", file=sys.stderr)
    if r.ok and not restored:
        try:
            if requests.post(f"{LLM}/slots/0?action=save", json={"filename": f"{name}.bin"}, timeout=120).ok:
                sidecar.write_text(key)
        except requests.RequestException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
