"""esc has to stop a turn within ~2 s, whatever the turn is waiting on.

Before this, esc only took effect between streamed chunks. Everything else —
a non-streaming model call (decide, plan, learn, the answer check), the
seconds of prompt reading before a stream's first chunk, or a tool's HTTP
request — ran to completion first, so the status bar said "stopping" for
tens of seconds.

Two parts:
  * `stoppable(fn)` marks the current THREAD as stoppable by `fn()` (the TUI
    worker's cancel event). Everything that runs in that thread — however
    deep, however many layers away from the UI — checks the same signal, so
    no function signature has to grow a `should_stop` parameter.
  * `ollama_lines()` does the HTTP in a helper thread and polls the signal
    every 0.1 s. On stop it SHUTS DOWN THE SOCKET. Merely abandoning the
    request is not enough: Ollama runs one request at a time here
    (NUM_PARALLEL=1), so an abandoned generation would keep the GPU busy and
    his NEXT question would queue behind it. Closing the connection makes
    Ollama cancel the work.
"""

import contextlib
import http.client
import json
import queue
import socket
import threading
import time
from urllib.parse import urlsplit

POLL = 0.1
_TL = threading.local()


class Interrupted(Exception):
    """The user pressed esc. Raised out of a wait; callers return early."""


def set_stop(fn):
    _TL.fn = fn


def stop_requested():
    fn = getattr(_TL, "fn", None)
    try:
        return bool(fn and fn())
    except Exception:
        return False


@contextlib.contextmanager
def stoppable(fn):
    """`with stoppable(cancel.is_set): ...` — restores the previous signal."""
    prev = getattr(_TL, "fn", None)
    _TL.fn = fn or prev
    try:
        yield
    finally:
        _TL.fn = prev


def _abort(conn):
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)   # unblocks a recv() in another thread
        except OSError:
            pass
    try:
        conn.close()
    except Exception:
        pass


def ollama_lines(url, payload, idle_timeout=180):
    """POST `payload`; yield each JSON object Ollama sends (one for a
    non-streaming request, one per chunk when streaming).

    Raises Interrupted if a stop is requested while waiting, and
    TimeoutError after `idle_timeout` seconds with nothing received — the
    same per-read semantics `requests`' timeout had."""
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=idle_timeout)
    q = queue.Queue()

    def work():
        try:
            conn.request("POST", u.path or "/", body=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            if r.status >= 400:
                q.put(("err", RuntimeError(f"HTTP {r.status} from {u.path}: "
                                           f"{r.read()[:300].decode(errors='replace')}")))
                return
            while True:
                line = r.readline()
                if not line:
                    break
                if line.strip():
                    q.put(("line", line))
            q.put(("end", None))
        except BaseException as e:     # noqa: BLE001 - handed to the caller
            q.put(("err", e))

    threading.Thread(target=work, daemon=True, name="ollama-http").start()
    last = time.monotonic()
    try:
        while True:
            if stop_requested():
                raise Interrupted()
            try:
                kind, val = q.get(timeout=POLL)
            except queue.Empty:
                if time.monotonic() - last > idle_timeout:
                    raise TimeoutError(f"no reply from {u.path} in {idle_timeout}s")
                continue
            last = time.monotonic()
            if kind == "line":
                yield json.loads(val)
            elif kind == "end":
                return
            else:
                raise val
    finally:
        _abort(conn)


def ollama_post(url, payload, timeout=180):
    """Non-streaming request → the single response object."""
    body = None
    for obj in ollama_lines(url, {**payload, "stream": False}, idle_timeout=timeout):
        body = obj
    if body is None:
        raise RuntimeError("empty reply from Ollama")
    if body.get("error"):
        raise RuntimeError(f"Ollama: {body['error']}")
    return body


def run_stoppable(fn, *args, **kwargs):
    """Run a blocking function (a tool) so esc can cut in. On stop the
    function is abandoned in its thread — its result is discarded — and
    Interrupted is raised here. Without a stop signal it just runs."""
    if getattr(_TL, "fn", None) is None:
        return fn(*args, **kwargs)
    box = {}

    def work():
        try:
            box["v"] = fn(*args, **kwargs)
        except BaseException as e:     # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=work, daemon=True, name="johan-tool")
    t.start()
    while t.is_alive():
        t.join(POLL)
        if t.is_alive() and stop_requested():
            raise Interrupted()
    if "e" in box:
        raise box["e"]
    return box.get("v")
