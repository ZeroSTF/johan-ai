"""run_python — execute model-written Python in a locked Docker container.

Why: small models are decent at short scripts and bad at arithmetic, parsing
and data work done "in their head". A sandbox turns "I think the average is
about..." into a computed answer, and makes charts possible at all.

The boundary is the run flags below, not the image:
  --network none            no internet, no LAN, no localhost services
  --read-only + tmpfs /tmp  nothing outside /work/out is writable
  --cap-drop ALL            no Linux capabilities
  --security-opt no-new-privileges
  --user <his uid>          never root inside; output files come back owned by him
  --memory 512m (no swap), --pids-limit 128, --cpus 2 --cpuset-cpus 8-15
                            capped, and pinned off cores 0-3 like every other
                            background job here, so a runaway script cannot
                            touch game performance
  --rm, and a hard kill after `timeout`
Host files reach it only when passed in `files`, each checked against the
same SENSITIVE pattern the shell tool uses, and mounted READ-ONLY at
/work/in/<name>.
"""

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

IMAGE = "johan-sandbox:1"
ROOT = Path.home() / "johan"
RUNS_DIR = ROOT / "brain/sandbox"
KEEP_RUNS = 30
MAX_CODE = 20_000
MAX_FILE = 20 * 1024 * 1024
MAX_FILES = 10
STDOUT_CHARS = 3000
STDERR_CHARS = 1500


def _sensitive():
    jt = sys.modules.get("johan_tools")
    if jt is None:
        import johan_tools as jt  # noqa: F811
    return jt.SENSITIVE


def _prune():
    runs = sorted(RUNS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in runs[KEEP_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)


def run_python(code, files=None, timeout=20):
    """Run `code` (Python 3.12 with numpy, pandas, matplotlib) in the sandbox.

    Files listed in `files` are readable at /work/in/<name>. Anything the
    script saves under /work/out/ comes back as a file path Zeros can open.
    Returns stdout, stderr, exit code and output files."""
    if not isinstance(code, str) or not code.strip():
        return {"error": "run_python needs `code` — a Python script as a string"}
    if len(code) > MAX_CODE:
        return {"error": f"script too long ({len(code)} chars, max {MAX_CODE})"}
    try:
        timeout = max(1, min(int(timeout or 20), 120))
    except (TypeError, ValueError):
        timeout = 20

    files = files or []
    if isinstance(files, str):
        files = [files]
    if len(files) > MAX_FILES:
        return {"error": f"at most {MAX_FILES} input files"}
    sensitive = _sensitive()
    inputs = []
    for f in files:
        p = Path(os.path.expanduser(str(f))).resolve()
        if sensitive.search(str(p)):
            return {"error": f"refused: {f} looks like a secret (keys, .env, credentials)"}
        if not p.is_file():
            return {"error": f"no such file: {f}"}
        if p.stat().st_size > MAX_FILE:
            return {"error": f"{f} is larger than {MAX_FILE // (1024 * 1024)} MB"}
        inputs.append(p)
    names = [p.name for p in inputs]
    if len(set(names)) != len(names):
        return {"error": "two input files share a name; pass them one run at a time"}

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "in").mkdir(parents=True)
    (run_dir / "out").mkdir()
    (run_dir / "main.py").write_text(code)
    for p in inputs:
        shutil.copy2(p, run_dir / "in" / p.name)

    name = f"johan-sandbox-{run_id}"
    cmd = [
        "docker", "run", "--rm", "--name", name, "--label", "johan=sandbox",
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--memory", "512m", "--memory-swap", "512m",
        "--pids-limit", "128", "--cpus", "2", "--cpuset-cpus", "8-15",
        "-v", f"{run_dir / 'main.py'}:/work/main.py:ro",
        "-v", f"{run_dir / 'in'}:/work/in:ro",
        "-v", f"{run_dir / 'out'}:/work/out:rw",
        "-w", "/work", IMAGE,
        "timeout", "-s", "KILL", str(timeout), "python", "/work/main.py",
    ]
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 30)
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        stdout, stderr, rc = "", "", 137
    except FileNotFoundError:
        return {"error": "docker is not available"}
    # `timeout -s KILL` exits 137 when it fires.
    if rc == 137:
        timed_out = True
        stderr = (stderr + f"\n[killed after {timeout}s]").strip()
    if "Unable to find image" in stderr or "No such image" in stderr:
        return {"error": f"sandbox image {IMAGE} is missing — rebuild it from ~/johan/sandbox"}

    outputs = [str(p) for p in sorted((run_dir / "out").rglob("*")) if p.is_file()]
    _prune()
    out = {
        "exit_code": rc,
        "stdout": stdout[-STDOUT_CHARS:] if len(stdout) > STDOUT_CHARS else stdout,
        "stderr": stderr[-STDERR_CHARS:] if len(stderr) > STDERR_CHARS else stderr,
        "seconds": round(time.monotonic() - started, 1),
        "files": outputs,
    }
    if timed_out:
        out["timed_out"] = True
    if len(stdout) > STDOUT_CHARS:
        out["note"] = f"stdout was {len(stdout)} chars; showing the last {STDOUT_CHARS}"
    # A traceback is the model's to fix; say where to look.
    if rc not in (0, 137) and re.search(r"Traceback|Error", stderr or ""):
        out["hint"] = "the script raised — read stderr, fix the code, run again"
    return out


TOOL = {"type": "function", "function": {
    "name": "run_python",
    "description": (
        "Run Python in a sandbox (no internet; numpy, pandas, matplotlib). "
        "Files passed in `files` appear at /work/in/; save charts to "
        "/work/out/; print() results."),
    "parameters": {"type": "object", "properties": {
        "code": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
    }, "required": ["code"]}}}
