"""Qwen3.6-35B-A3B (MoE, APEX I-Mini) on this box, measured on Johan's REAL
request shape (system prompt + all tool schemas), like bench.py did for the 4B.

    python bench_moe.py 40 32 28        # --n-cpu-moe values to try, in order

For each value: start llama-server (CUDA build in the 24.04 container, see
run_cuda.sh), time cold / new_chat / followup / gen exactly as bench.py does,
and record VRAM used and RAM still available while it is loaded. Stops at the
first value that fails to load (the card is full) or leaves under 1.5 GB of RAM.

--cache-ram 2048 caps the prompt cache: at its 8 GB default the 4B's server
filled RAM during builds and the PC ran out of memory (2026-10-21).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402  (msgs, llama, vram, PORT, CTX — same scenarios)

MODEL = "Qwen3.6-35B-A3B-APEX-I-Mini.gguf"
OUT = HERE / "results-moe.jsonl"


def ram_available_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def run(n_cpu_moe):
    args = ["llama-server", "-m", f"/models/{MODEL}", "-ngl", "99", "--n-cpu-moe", str(n_cpu_moe),
            "-c", str(bench.CTX), "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja",
            "-t", "8", "-np", "1", "--cache-ram", "2048",
            "--host", "127.0.0.1", "--port", str(bench.PORT)]
    log = open(HERE / f"server-moe-{n_cpu_moe}.log", "w")
    ram0 = ram_available_mb()
    t = time.monotonic()
    proc = subprocess.Popen([str(HERE / "run_cuda.sh")] + args, stdout=log, stderr=subprocess.STDOUT)
    res = {"engine": "llama.cpp-cuda", "model": MODEL, "n_cpu_moe": n_cpu_moe,
           "ram_available_before_mb": ram0}
    try:
        while time.monotonic() - t < 600:
            try:
                if requests.get(f"http://127.0.0.1:{bench.PORT}/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if proc.poll() is not None:
                return {**res, "error": "server exited — see " + log.name}
            time.sleep(1)
        else:
            return {**res, "error": "no health after 600 s"}
        res["load_s"] = round(time.monotonic() - t, 1)
        res["cold"] = bench.llama(bench.msgs("how many jobs are in my apply queue?"))
        res["new_chat"] = bench.llama(bench.msgs("what's today's date?"))
        first = res["new_chat"].get("text") or "Let me check."
        res["followup"] = bench.llama(bench.msgs("what's today's date?", first,
                                                 "and what day of the week is it?"))
        res["gen"] = bench.llama(bench.msgs("Write about 300 words on the history of Carthage."),
                                 tools=False, max_tokens=400)
        res["vram_mb"] = bench.vram()
        res["ram_available_loaded_mb"] = ram_available_mb()
    finally:
        subprocess.run(["docker", "stop", "-t", "5", "johan-llama-llama-server"], capture_output=True)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return res


if __name__ == "__main__":
    requests.post("http://127.0.0.1:11434/api/generate",       # the 4B must not share the card
                  json={"model": "johan-agent", "keep_alive": 0}, timeout=60)
    for n in [int(x) for x in sys.argv[1:]]:
        r = run(n)
        print(json.dumps(r, indent=1), flush=True)
        with OUT.open("a") as fh:
            fh.write(json.dumps(r) + "\n")
        if r.get("error") or r.get("ram_available_loaded_mb", 99999) < 1500:
            break
