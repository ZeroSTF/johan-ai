"""esc must actually stop a turn — within ~2 s, whatever it is waiting on.

A fake Ollama server stalls every request for 30 s (like a long prompt read
or a slow decide/plan call). The real app runs against it through Textual's
pilot: send a message, press esc, and the app must be idle again within
2 s, and the fake server must see its connection closed — proof that Ollama
would cancel the work rather than keep the GPU busy for nobody. Also covers
a stalled NON-streaming call (the plan check's decide) and a stalled tool.
No real model, no GPU, nothing written under brain/.

    python test_stop.py
"""
import asyncio
import json
import select
import socket
import os
# These test the 4B (Ollama) path and the 4B-only crutches a smart model skips;
# config/default-model now says qwen36.
os.environ["JOHAN_MODEL"] = "johan-agent"
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_assistant as ja        # noqa: E402
import johan_interrupt as ji        # noqa: E402
import johan_plan as jp             # noqa: E402
import johan_tui as tui             # noqa: E402
# His REAL pending changes must never surface in a test (a stray enter on a
# waiting card could approve one): no 'waiting from earlier' cards here.
tui.JohanApp._show_backlog = lambda self, intro=True: None

STALL = 30.0
events = {"requests": 0, "closed": [], "started": []}
results = {}


class SlowOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        events["requests"] += 1
        events["started"].append(time.monotonic())
        # Stall like a long prompt read. Watch for the client hanging up:
        # readable + recv() == b"" means the peer closed the socket.
        end = time.monotonic() + STALL
        while time.monotonic() < end:
            r, _, _ = select.select([self.connection], [], [], 0.05)
            if r:
                try:
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        events["closed"].append(time.monotonic())
                        return
                except OSError:
                    events["closed"].append(time.monotonic())
                    return
        out = {"message": {"role": "assistant", "content": "too late"}, "done": True}
        data = json.dumps(out).encode() + b"\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def check(label, ok):
    results[label] = bool(ok)


async def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/chat"

    saved = {}

    def stub(obj, name, val):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)
    stub(ja, "OLLAMA_CHAT", url)
    stub(ja, "save_session", lambda m: None)          # never touch brain/
    stub(ja, "log_session", lambda m: None)
    stub(ja, "prime_with_memories", lambda s, **k: s)
    stub(ja._skills, "inject", lambda messages, q: None)
    stub(tui.JohanApp, "warm_up", lambda self: None)
    stub(tui.Sidebar, "refresh_state", lambda self, messages=None: None)
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(140, 45)) as pilot:
            async def stalled_turn(text, label):
                n = events["requests"]
                box = app.query_one("#prompt")
                box.focus()
                box.value = text
                await pilot.press("enter")
                for _ in range(100):                     # wait for the request to land
                    await asyncio.sleep(0.05)
                    if events["requests"] > n:
                        break
                check(f"{label}: the stalled request reached the server",
                      events["requests"] > n)
                await asyncio.sleep(0.5)
                closed_before = len(events["closed"])
                t0 = time.monotonic()
                await pilot.press("escape")
                idle_at = None
                while time.monotonic() - t0 < 5:
                    await asyncio.sleep(0.05)
                    if not app.busy:
                        idle_at = time.monotonic() - t0
                        break
                check(f"{label}: idle again within 2 s of esc "
                      f"({'never' if idle_at is None else f'{idle_at:.2f}s'})",
                      idle_at is not None and idle_at <= 2.0)
                await asyncio.sleep(0.3)
                check(f"{label}: the connection was closed (Ollama would cancel)",
                      len(events["closed"]) > closed_before)
                await app.workers.wait_for_complete()

            # 1. streaming chat call stalled before its first chunk
            stub(jp, "prefilter", lambda text: False)
            await stalled_turn("tell me a long story", "stream")

            # 2. a NON-streaming call: the plan check's calibrated decide
            setattr(jp, "prefilter", lambda text: True)
            await stalled_turn("install docker and set it up for me", "decide")
            setattr(jp, "prefilter", lambda text: False)

            # 3. the next question is not stuck behind the stopped one
            check("after esc, a new turn can start", not app.busy)
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)
        srv.shutdown()

    # 4. a stalled TOOL gives way to esc too (unit level)
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    t0 = time.monotonic()
    with ji.stoppable(stop.is_set):
        try:
            ji.run_stoppable(time.sleep, STALL)
            raised = False
        except ji.Interrupted:
            raised = True
    check(f"a stalled tool gives way to esc ({time.monotonic() - t0:.2f}s)",
          raised and time.monotonic() - t0 < 1.5)

    # 5. the tool thread sees the turn's context (the reader's question):
    # a fresh thread starts with an empty context, so long pages fell back
    # to their first 3,000 chars whenever esc-stoppable tools were on.
    import johan_reader as jr
    jr.set_question("how do I remap controls")
    got = {}
    with ji.stoppable(threading.Event().is_set):
        ji.run_stoppable(lambda: got.setdefault("q", jr._QUESTION.get()))
    check("a stoppable tool still sees the reader's question",
          got.get("q") == "how do I remap controls")

    # 6. without a stop signal nothing changes
    check("no stop signal: tools run normally", ji.run_stoppable(lambda: 42) == 42)


asyncio.run(main())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
