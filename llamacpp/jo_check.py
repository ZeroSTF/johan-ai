"""Real-use checks of Johan-in-OpenCode (`jo run`), one-shot, recording which
tools were called and what came back. Needs the GPU; ~10 min.

    python jo_check.py [names...]
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
NODE = HOME / ".nvm/versions/node/v24.18.0/bin"
CASES = {
    "maths": ("what's 1847 * 23?", ["johan_calculate"], "42481"),
    "jobs": ("give me 2 jobs to apply to", ["johan_build_apply_queue"], None),
    "draw": ("draw a red panda eating ramen, square", ["johan_generate_image"], ".png"),
    "change": ("make the top bar clock show seconds", ["johan_propose_action"], None),
    "chat": ("bro i just got destroyed in tf2 lmao", [], None),
}


def run(name):
    q, want, contains = CASES[name]
    env = {**os.environ, "PATH": f"{NODE}:{os.environ['PATH']}", "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "JO_KEEP": "1"}
    t0 = time.monotonic()
    p = subprocess.run([str(HOME / "bin/jo"), "run", "--format", "json", q], env=env, cwd=HOME / "projects",
                       capture_output=True, text=True, timeout=900)
    secs = round(time.monotonic() - t0)
    tools, text, errors = [], [], []
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        part = ev.get("part") or {}
        if part.get("type") == "tool":
            st = part.get("state") or {}
            tools.append((part.get("tool"), st.get("status"), str(st.get("error") or "")[:120]))
        elif part.get("type") == "text" and part.get("text"):
            text.append(part["text"])
        if ev.get("type") == "error":
            errors.append(str(ev)[:200])
    names = [t[0] for t in tools]
    ok = all(w in names for w in want) and (not want or True) and (
        contains is None or contains in " ".join(text) or any(contains in str(t) for t in tools))
    if not want:
        ok = ok and not names
    return {"case": name, "ok": ok, "seconds": secs, "tools": tools, "answer": " ".join(text)[:400],
            "errors": errors, "stderr": p.stderr[-300:] if p.returncode else ""}


if __name__ == "__main__":
    try:
        for n in sys.argv[1:] or list(CASES):
            r = run(n)
            print(json.dumps(r), flush=True)
    finally:
        subprocess.run([str(HOME / "johan/venv-embed/bin/python3"),
                        str(HOME / "johan/scripts/johan_llm.py"), "stop"], capture_output=True)
