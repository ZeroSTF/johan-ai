"""OpenCode vs DeepSeek Harness on Johan's own project task, same local model.

Both agents talk to the 35B on llama-server (johan_llm, port 8092), each in a
fresh folder under ~/.cache/johan-hard/h2h, each run INSIDE the jail
(~/.cache/johan-jail.sh: home read-only, only ~/.cache/johan-hard writable),
then graded by eval_hard's hidden browser checks for that case.

    python h2h.py opencode dsh            # agents to run, in order
    python h2h.py --case breakout dsh
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
sys.path.insert(0, str(HOME / "johan/scripts"))
import eval_hard as eh        # noqa: E402

BASE = HOME / ".cache/johan-hard/h2h"
NODE = HOME / ".nvm/versions/node/v24.18.0/bin"
JAIL = HOME / ".cache/johan-jail.sh"
LIMIT = 900


def env_for(agent, w):
    e = {**os.environ, "PATH": f"{NODE}:{os.environ['PATH']}"}
    if agent == "opencode":
        st = BASE / "oc-home"
        e.update(OPENCODE_CONFIG=str(BASE / "opencode.json"), OPENCODE_DISABLE_MODELS_FETCH="1",
                 XDG_DATA_HOME=str(st / "data"), XDG_CONFIG_HOME=str(st / "config"),
                 XDG_CACHE_HOME=str(st / "cache"), XDG_STATE_HOME=str(st / "state"))
        return e, ["opencode", "run", "--dir", str(w)]
    dh = BASE / "dsh-home"
    dh.mkdir(parents=True, exist_ok=True)
    e.update(DSH_HOME=str(dh), DSH_TELEMETRY_DISABLED="1", LOCAL_LLM_KEY="none")
    # dsh's own sandbox makes the folder it STARTS in the only writable one, and
    # the jail always starts in its scratch folder — so cd into the project first.
    return e, ["bash", "-c", f'cd "{w}" && exec dsh headless --patch '
               f'"{HOME / ".cache/dsh/local.patch.yml"}" "$0"']


def run(agent, case):
    c = next(x for x in eh.CASES if x["name"] == case)
    w = BASE / f"{agent}-{case}-{time.strftime('%H%M%S')}"
    w.mkdir(parents=True)
    task = (c["prompt"] if isinstance(c["prompt"], str) else c["prompt"][0]).format(w=w)
    env, cmd = env_for(agent, w)
    log = open(w.parent / f"{w.name}.log", "w")
    t0 = time.monotonic()
    try:
        r = subprocess.run([str(JAIL)] + cmd + [task], cwd=w, env=env, stdout=log,
                           stderr=subprocess.STDOUT, timeout=LIMIT)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    secs = round(time.monotonic() - t0)
    ok, detail = c["grade"](w, "")
    files = sorted(str(p.relative_to(w)) for p in w.rglob("*") if p.is_file())
    return {"agent": agent, "case": case, "passed": ok, "seconds": secs, "rc": rc,
            "detail": detail[:300], "files": files[:20], "log": log.name}


if __name__ == "__main__":
    args = sys.argv[1:]
    case = "budget-app"
    if args[:1] == ["--case"]:
        case, args = args[1], args[2:]
    for a in args:
        res = run(a, case)
        print(json.dumps(res), flush=True)
        with open(BASE / "results.jsonl", "a") as fh:
            fh.write(json.dumps(res) + "\n")
