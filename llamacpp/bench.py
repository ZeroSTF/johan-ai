"""Ollama vs llama-server on Johan's REAL request shape (system prompt + all
tool schemas), same model family and quant (Qwen3.5-4B Q4_K_M), same context
(32768), flash attention and q8_0 KV cache on both.

    python bench.py ollama
    python bench.py llama <label> <server-binary-dir> <device>   e.g. vulkan ... Vulkan1

Scenarios, each timed by the engine's own counters AND wall clock:
  cold      first message right after the model loads (what Telegram and
            `johan "q"` pay today, ~13-22 s)
  new_chat  a DIFFERENT first message, same system+tools prefix (every ctrl+n)
  followup  next turn in the same conversation
  gen       400 tokens of prose, no tools: generation speed
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path.home() / "johan/scripts"))
import johan_assistant as ja  # noqa: E402

OUT = Path(__file__).parent / "results.jsonl"
SYSTEM = ja.prime_with_memories(ja.SYSTEM_PROMPT)
TOOLS = ja.TOOLS
CTX = 32768
MODEL_GGUF = Path(__file__).parent / "models/Qwen3.5-4B-Q4_K_M.gguf"


def vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def msgs(*turns):
    m = [{"role": "system", "content": SYSTEM}]
    for i, t in enumerate(turns):
        m.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return m


# ------------------------------------------------------------------ ollama

def ollama(messages, tools=True, max_tokens=200):
    t = time.monotonic()
    r = requests.post("http://127.0.0.1:11434/api/chat", json={
        "model": "johan-agent", "messages": messages, "stream": False, "think": False,
        **({"tools": TOOLS} if tools else {}), "keep_alive": "5m",
        "options": {"temperature": 0.3, "num_ctx": CTX, "num_predict": max_tokens}},
        timeout=600).json()
    wall = time.monotonic() - t
    msg = r.get("message", {})
    return {"wall_s": round(wall, 2),
            "prompt_n": r.get("prompt_eval_count"),
            "prompt_s": round(r.get("prompt_eval_duration", 0) / 1e9, 2),
            "gen_n": r.get("eval_count"),
            "gen_tps": round(r.get("eval_count", 0) / max(r.get("eval_duration", 1) / 1e9, 1e-9), 1),
            "load_s": round(r.get("load_duration", 0) / 1e9, 2),
            "tool": [c["function"]["name"] for c in msg.get("tool_calls") or []],
            "text": (msg.get("content") or "")[:80]}


def run_ollama():
    requests.post("http://127.0.0.1:11434/api/generate",
                  json={"model": "johan-agent", "keep_alive": 0}, timeout=60)
    time.sleep(3)
    t = time.monotonic()
    requests.post("http://127.0.0.1:11434/api/chat", json={
        "model": "johan-agent", "messages": [], "keep_alive": "5m",
        "options": {"num_ctx": CTX}}, timeout=300)
    load = time.monotonic() - t
    res = {"engine": "ollama", "load_s": round(load, 2)}
    res["cold"] = ollama(msgs("how many jobs are in my apply queue?"))
    res["new_chat"] = ollama(msgs("what's today's date?"))
    first = res["new_chat"]["text"] or "Let me check."
    res["followup"] = ollama(msgs("what's today's date?", first, "and what day of the week is it?"))
    res["gen"] = ollama(msgs("Write about 300 words on the history of Carthage."), tools=False, max_tokens=400)
    res["vram_mb"] = vram()
    requests.post("http://127.0.0.1:11434/api/generate",
                  json={"model": "johan-agent", "keep_alive": 0}, timeout=60)
    return res


# ------------------------------------------------------------------ llama-server

PORT = 8091


def llama(messages, tools=True, max_tokens=200):
    t = time.monotonic()
    r = requests.post(f"http://127.0.0.1:{PORT}/v1/chat/completions", json={
        "messages": messages, **({"tools": TOOLS} if tools else {}),
        "temperature": 0.3, "max_tokens": max_tokens, "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False}}, timeout=600).json()
    wall = time.monotonic() - t
    if "error" in r:
        return {"error": str(r["error"])[:300], "wall_s": round(wall, 2)}
    tm = r.get("timings", {})
    msg = r["choices"][0]["message"]
    return {"wall_s": round(wall, 2),
            "prompt_n": tm.get("prompt_n"), "cached_n": tm.get("cache_n"),
            "prompt_s": round(tm.get("prompt_ms", 0) / 1000, 2),
            "gen_n": tm.get("predicted_n"),
            "gen_tps": round(tm.get("predicted_per_second", 0), 1),
            "tool": [c["function"]["name"] for c in msg.get("tool_calls") or []],
            "text": (msg.get("content") or "")[:80]}


def run_llama(label, bindir, device):
    bindir = Path(bindir)
    env = {**os.environ, "LD_LIBRARY_PATH": str(bindir)}
    args = ["--device", device, "-ngl", "99", "-c", str(CTX), "-fa", "on",
            "-ctk", "q8_0", "-ctv", "q8_0", "--jinja", "-t", "8", "-np", "1",
            "--host", "127.0.0.1", "--port", str(PORT)]
    if label.startswith("cuda"):
        # The CUDA build needs glibc 2.38; Pop!_OS 22.04 has 2.35. Run the
        # same binaries in an Ubuntu 24.04 container with the GPU passed in
        # by hand (no nvidia container runtime installed).
        L = "/usr/lib/x86_64-linux-gnu"
        rt = bindir.parent / "cudart-llama-b11583-bin-ubuntu-cuda-12.8-x64"
        drv = "580.173.02"
        cmd = ["docker", "run", "--rm", "--name", "johan-llamabench", "--network", "host",
               "--cpuset-cpus", "8-15",
               "--device", "/dev/nvidia0", "--device", "/dev/nvidiactl", "--device", "/dev/nvidia-uvm",
               "-v", f"{bindir}:/app:ro", "-v", f"{rt}:/rt:ro",
               "-v", f"{MODEL_GGUF.parent}:/models:ro",
               "-v", f"{L}/libcuda.so.{drv}:/drv/libcuda.so.1:ro",
               "-v", f"{L}/libnvidia-ptxjitcompiler.so.{drv}:/drv/libnvidia-ptxjitcompiler.so.1:ro",
               "-v", f"{L}/libnvidia-ml.so.{drv}:/drv/libnvidia-ml.so.1:ro",
               # The release has no precompiled kernels for this card (sm_75):
               # the driver JIT-compiles them on first use (69 s load, 99 s
               # first prompt). Keep that cache across runs.
               "-v", f"{Path(__file__).parent / 'nvcache'}:/nvcache",
               "-e", "CUDA_CACHE_PATH=/nvcache", "-e", "CUDA_CACHE_MAXSIZE=4294967296",
               "-e", "LD_LIBRARY_PATH=/app:/rt:/drv", "johan-llamacpp-cuda:bench",
               "/app/llama-server", "-m", f"/models/{MODEL_GGUF.name}"] + args
    else:
        cmd = ["taskset", "-c", "8-15", str(bindir / "llama-server"), "-m", str(MODEL_GGUF)] + args
    log = open(Path(__file__).parent / f"server-{label}.log", "w")
    t = time.monotonic()
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
    res = {"engine": f"llama.cpp-{label}"}
    try:
        while time.monotonic() - t < 300:
            try:
                if requests.get(f"http://127.0.0.1:{PORT}/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if proc.poll() is not None:
                return {**res, "error": "server exited — see server log"}
            time.sleep(0.5)
        res["load_s"] = round(time.monotonic() - t, 2)
        res["cold"] = llama(msgs("how many jobs are in my apply queue?"))
        res["new_chat"] = llama(msgs("what's today's date?"))
        first = res["new_chat"].get("text") or "Let me check."
        res["followup"] = llama(msgs("what's today's date?", first, "and what day of the week is it?"))
        res["gen"] = llama(msgs("Write about 300 words on the history of Carthage."), tools=False, max_tokens=400)
        res["vram_mb"] = vram()
    finally:
        if label.startswith("cuda"):
            subprocess.run(["docker", "stop", "-t", "5", "johan-llamabench"], capture_output=True)
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
    return res


if __name__ == "__main__":
    if sys.argv[1] == "ollama":
        out = run_ollama()
    else:
        out = run_llama(sys.argv[2], sys.argv[3], sys.argv[4])
    print(json.dumps(out, indent=1))
    with OUT.open("a") as fh:
        fh.write(json.dumps(out) + "\n")
