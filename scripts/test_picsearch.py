"""find_images, offline: a fake SearXNG and a fake image server — no network,
no model (the look-check is stubbed), nothing written under brain/.

Ranking, junk engines, duplicate links and duplicate pictures, the size cap,
wrong content types, animated/vector/tiny files, a slow server within the
time budget, the look-check (drop / keep / fail once → unchecked), sidecars,
safesearch per mode, error paths, and esc.

    python test_picsearch.py
"""
import io
import json
import random
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from PIL import Image                # noqa: E402
import johan_picsearch as jps        # noqa: E402

results = {}
STATE = {"search": [], "params": [], "dead": []}


def check(label, ok):
    results[label] = bool(ok)


def jpeg(seed, size=(640, 480)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _jpg(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def gif_animated():
    frames = [Image.new("RGB", (300, 300), c) for c in ("red", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


FILES = {}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/search":
            STATE["params"].append(parse_qs(u.query))
            body = json.dumps({"results": STATE["search"],
                               "unresponsive_engines": STATE["dead"]}).encode()
            return self._send(200, "application/json", body)
        name = u.path.rsplit("/", 1)[-1]
        if name == "slow.jpg":
            time.sleep(6)
        if name == "huge.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            try:
                for _ in range(12):
                    self.wfile.write(b"\xff" * (1024 * 1024))
            except OSError:
                pass
            return
        ctype, data = FILES.get(name, ("text/plain", b"missing"))
        self._send(200 if name in FILES else 404, ctype, data)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def row(base, name, title, engine="bing images", res="800x600", page=None, **kw):
    return {"img_src": f"{base}/img/{name}", "title": title, "engine": engine,
            "engines": kw.pop("engines", [engine]), "resolution": res,
            "url": page or f"https://example.com/{name}.html", **kw}


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = Path(tempfile.mkdtemp())
    saved = {k: getattr(jps, k) for k in ("SEARX_URL", "OUT", "look", "_safe",
                                          "THUMB_TIMEOUT", "THUMB_BUDGET")}
    jps.SEARX_URL, jps.OUT = f"{base}/search", tmp / "web"
    jps._safe = lambda: 1
    looked = []

    def look(im, q):                                  # the model, stubbed: a
        looked.append(im.size)                        # 640×480 is a match,
        return (0.9, True) if im.size == (640, 480) else (0.1, True)   # 641 isn't
    jps.look = look
    FILES.update({
        "a.jpg": ("image/jpeg", jpeg(1)), "b.jpg": ("image/jpeg", jpeg(2)),
        "c.jpg": ("image/jpeg", jpeg(3)), "d.jpg": ("image/jpeg", jpeg(4)),
        "e.jpg": ("image/jpeg", jpeg(5)),
        "same-as-a.jpg": ("image/jpeg", FILES.get("a.jpg", (0, jpeg(1)))[1]),
        "wrong.jpg": ("image/jpeg", jpeg(6, (641, 480))),
        "tiny.jpg": ("image/jpeg", jpeg(7, (60, 60))),
        "page.jpg": ("text/html", b"<html>hi</html>"),
        "anim.gif": ("image/gif", gif_animated()),
        "anim-as-jpg.jpg": ("image/jpeg", gif_animated()),
        "broken.jpg": ("image/jpeg", b"not really a jpeg"),
    })
    FILES["same-as-a.jpg"] = FILES["a.jpg"]
    try:
        # ------------------------------------------------------------ rank
        rows = [
            row(base, "a.jpg", "Red panda in a tree"),
            row(base, "a.jpg?w=200", "Red panda in a tree (copy)"),
            row(base, "art.jpg", "Red panda, 1890 etching", engine="artic"),
            row(base, "icon.jpg", "panda", engine="lucide"),
            row(base, "v.svg", "Red panda vector"),
            row(base, "x.jpg", "Red panda thumbnail", res="120x90"),
            row(base, "b.jpg", "A cute animal", engines=["bing images", "flickr"]),
            row(base, "c.jpg", "red panda", res="3000x2000"),
        ]
        r = jps.rank(rows, "red panda")
        srcs = [x["img_src"].rsplit("/", 1)[-1] for x in r]
        check("rank: museum/icon engines, svg, thumbnails and repeated links dropped",
              srcs.count("a.jpg") + srcs.count("a.jpg?w=200") == 1
              and not {"art.jpg", "icon.jpg", "v.svg", "x.jpg"} & set(srcs))
        check("rank: titles naming it come first, big ones before small",
              srcs[:2] == ["c.jpg", "a.jpg"] and srcs[-1] == "b.jpg")
        check("title match is whole words ('ti' is not in 'tiles')",
              abs(jps.title_match("gtx 1660 ti", {"title": "1660 tiles"}) - 1 / 3) < 1e-9
              and jps.title_match("gtx 1660 ti", {"title": "GTX 1660 Ti OC"}) == 1.0)

        # ------------------------------------------------------------ full run
        STATE["search"] = [
            row(base, "huge.jpg", "red panda huge"),
            row(base, "page.jpg", "red panda page"),
            row(base, "tiny.jpg", "red panda tiny"),     # says 800x600, is 60x60
            row(base, "anim.gif", "red panda gif"),
            row(base, "anim-as-jpg.jpg", "red panda gif in disguise"),
            row(base, "broken.jpg", "red panda broken"),
            row(base, "a.jpg", "red panda one", page="https://zoo.org/pandas"),
            row(base, "same-as-a.jpg", "red panda one again"),
            row(base, "wrong.jpg", "red panda?"),
            row(base, "b.jpg", "red panda two"),
            row(base, "c.jpg", "red panda three"),
            row(base, "d.jpg", "red panda four"),
            row(base, "e.jpg", "red panda five"),
        ]
        STATE["dead"] = [["openverse", "timeout"], ["artic", "timeout"]]
        t0 = time.monotonic()
        out = jps.find_images("red panda", count=3)
        took = time.monotonic() - t0
        pics = out.get("pictures", [])
        check(f"keeps {3} real matches in rank order ({took:.1f}s)",
              out.get("count") == 3 and [p["title"] for p in pics]
              == ["red panda one", "red panda two", "red panda three"])
        d = out.get("dropped", {})
        check("dropped: the 10 MB+, html, tiny, animated and broken files",
              d.get("download") == 5)
        check("dropped: the same picture at another link", d.get("duplicate") == 1)
        check("dropped: the one the model said isn't it", d.get("not_a_match") == 1)
        side = json.loads(Path(pics[0]["path"]).with_suffix(".json").read_text()) if pics else {}
        check("saved under the web folder with a sidecar naming the source page",
              pics and Path(pics[0]["path"]).parent == tmp / "web"
              and side.get("source") == "https://zoo.org/pandas"
              and side.get("query") == "red panda" and side.get("image_url", "").endswith("a.jpg"))
        check("…the model is told the source sites", pics and pics[0]["site"] == "zoo.org"
              and "source" in out.get("note", ""))
        import johan_steps as js
        check("the answer check skips it (code shows the pictures; 3/3 false rewrites)",
              js._shown_by_code(json.dumps(out, default=str))
              and not js._shown_by_code(json.dumps({"count": 2, "results": []})))
        check("pictures came back: no engine list crowding the titles out of the fact check",
              "engines_unavailable" not in out
              and json.dumps(out).index('"title"') < json.dumps(out).index('"path"'))
        check("safesearch moderate in normal mode", STATE["params"][-1].get("safesearch") == ["1"])
        jps._safe = lambda: 0
        jps.find_images("red panda", count=1)
        check("…off in uncensored mode", STATE["params"][-1].get("safesearch") == ["0"])
        jps._safe = lambda: 1

        # ------------------------------------------------------------ thumbnail first, original after
        big = Image.open(io.BytesIO(jpeg(11, (1200, 800))))
        FILES["full.jpg"] = ("image/jpeg", _jpg(big))
        FILES["full-thumb.jpg"] = ("image/jpeg", _jpg(big.resize((300, 200))))
        FILES["other-full.jpg"] = ("image/jpeg", jpeg(12, (1200, 800)))   # not the thumb's picture
        FILES["other-thumb.jpg"] = ("image/jpeg", _jpg(Image.open(io.BytesIO(jpeg(13, (1200, 800)))).resize((300, 200))))
        STATE["search"] = [
            row(base, "full.jpg", "red panda big", thumbnail_src=f"{base}/img/full-thumb.jpg"),
            row(base, "other-full.jpg", "red panda swapped", thumbnail_src=f"{base}/img/other-thumb.jpg")]
        jps.look = lambda im, q: (0.9, True)
        out = jps.find_images("red panda", count=2)
        first = [Image.open(p["path"]).size for p in out.get("pictures", [])]
        jps.wait_full([p["path"] for p in out.get("pictures", [])], timeout=10)
        after = [Image.open(p["path"]).size for p in out.get("pictures", [])]
        sides = [json.loads(Path(p["path"]).with_suffix(".json").read_text())
                 for p in out.get("pictures", [])]
        check("answers from the thumbnail (it doesn't wait for the original)",
              first and first[0] == (300, 200))
        check("…then the original replaces it on disk, sidecar says full",
              after and after[0] == (1200, 800) and sides[0].get("full") is True)
        check("…but an 'original' that isn't the same picture is not swapped in",
              len(after) == 2 and after[1] == (300, 200) and sides[1].get("full") is False)
        check("wikimedia gets the descriptive User-Agent its policy asks for",
              jps._ua("https://upload.wikimedia.org/x.jpg") is jps.POLICY_UA
              and jps._ua("https://i.pinimg.com/x.jpg") is jps.UA
              and jps._ua("https://notwikimedia.org.evil.com/x") is jps.UA)
        jps.look = look

        # ------------------------------------------------------------ the model can't look
        calls = []

        def broken_look(im, q):
            calls.append(1)
            raise RuntimeError("HTTP 500: cudaMalloc failed: out of memory")
        jps.look = broken_look
        out = jps.find_images("red panda", count=2)
        check("model down: pictures still come back, marked unsure",
              out.get("count") == 2 and all(p.get("unsure") for p in out["pictures"]))
        check("…it tried once, not once per picture", len(calls) == 1)
        check("…and the model is told they're unchecked",
              "unchecked" in out and "out of memory" in out.get("look_check_failed", ""))
        jps.look = look

        # ------------------------------------------------------------ slow server
        jps.THUMB_TIMEOUT, jps.THUMB_BUDGET = 2, 3
        STATE["search"] = [row(base, "slow.jpg", "red panda slow"),
                           row(base, "b.jpg", "red panda two")]
        t0 = time.monotonic()
        out = jps.find_images("red panda", count=2)
        took = time.monotonic() - t0
        check(f"a slow server is skipped within the budget ({took:.1f}s)",
              took < 5 and out.get("count") == 1 and out["dropped"].get("download") == 1)
        jps.THUMB_TIMEOUT, jps.THUMB_BUDGET = saved["THUMB_TIMEOUT"], saved["THUMB_BUDGET"]

        # ------------------------------------------------------------ esc
        STATE["search"] = [row(base, n, f"red panda {n}") for n in ("a.jpg", "b.jpg", "c.jpg")]
        stop = {"now": False}

        def stopping_look(im, q):
            stop["now"] = True
            return 0.9, True
        jps.look = stopping_look
        out = jps.find_images("red panda", count=3, should_stop=lambda: stop["now"])
        check("esc: stops after the current picture", "stopped" in out.get("error", "")
              and len(out.get("pictures", [])) <= 1)
        jps.look = look

        # ------------------------------------------------------------ errors
        STATE["search"], STATE["dead"] = [], [["bing images", "timeout"], ["artic", "timeout"]]
        out = jps.find_images("red panda")
        check("nothing back = 'search degraded', naming the engines, never 'doesn't exist'",
              "degraded" in out.get("error", "") and "bing images" in out["error"]
              and "artic" not in out["error"])
        STATE["search"], STATE["dead"] = [row(base, "page.jpg", "red panda")], []
        out = jps.find_images("red panda")
        check("nothing downloadable = an explicit error", out.get("error") and out.get("count") == 0)
        jps.SEARX_URL = "http://127.0.0.1:9/search"
        check("searxng down = says so", "unreachable" in jps.find_images("x").get("error", ""))
        check("empty query refused", jps.find_images("  ").get("error"))
        jps.SEARX_URL = f"{base}/search"

        # ------------------------------------------------------------ look → decide
        import johan_decide as jd
        import johan_interrupt as ji
        sent = []

        def fake_post(url, payload, timeout=180):
            sent.append(payload)
            grey = payload["messages"][-1].get("images") == [jd._grey_b64()]
            lean = -0.7 if grey else -0.05          # the grey picture: no real lean
            return {"logprobs": [{"top_logprobs": [{"token": "A", "logprob": lean},
                                                   {"token": "B", "logprob": -0.7 if grey else -3.0}]}]}
        real_jd = (ji.ollama_post, jd._prior_path, dict(jd._PRIORS), jd._log)
        ji.ollama_post, jd._prior_path = fake_post, (lambda: tmp / "priors.json")
        jd._PRIORS.clear()
        jd._log = lambda *a, **k: None
        try:
            p, sure = saved["look"](Image.open(io.BytesIO(FILES["a.jpg"][1])), "red panda")
        finally:
            ji.ollama_post, jd._prior_path, _, jd._log = real_jd
            jd._PRIORS.clear()
            jd._PRIORS.update(real_jd[2])
        user = sent[0]["messages"][-1] if sent else {}
        prior_user = sent[-1]["messages"][-1] if sent else {}
        check("look: the picture goes to the model, shrunk, context first",
              len(sent) == 2 and len(user.get("images", [])) == 1
              and user["content"].startswith("A picture is attached.") and p > 0.5 and sure)
        check("…calibrated against a plain grey picture, not against no picture",
              len(prior_user.get("images", [])) == 1 and prior_user["images"][0] == jd._grey_b64())

        STATE["search"] = [row(base, "a.jpg", "red panda")]
        check("a junk count falls back to 4", jps.find_images("red panda", count="lots").get("count") == 1)
    finally:
        for k, v in saved.items():
            setattr(jps, k, v)
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def tui_checks():
    """The card in the real app: pick a picture, save it, open it and its
    page, and hand it to /draw — without a model, a gallery or ~/Pictures."""
    import johan_assistant as ja
    import johan_tui as tui
    tui.JohanApp._show_backlog = lambda self, intro=True: None
    tmp = Path(tempfile.mkdtemp())
    pics = []
    for n in range(3):
        p = tmp / f"{n}.jpg"
        Image.open(io.BytesIO(jpeg(n, (300, 200)))).save(p)
        pics.append({"path": str(p), "site": f"site{n}.org", "title": f"pic {n}",
                     "source": f"https://site{n}.org/page"})
    opened = []
    saved = {(tui, "open_target"): tui.open_target, (tui, "SAVED_PICTURES"): tui.SAVED_PICTURES,
             (tui.JohanApp, "warm_up"): tui.JohanApp.warm_up,
             (tui.Sidebar, "refresh_state"): tui.Sidebar.refresh_state,
             (ja, "save_session"): ja.save_session, (ja, "log_session"): ja.log_session}
    tui.open_target = lambda t: opened.append(str(t)) or True
    tui.SAVED_PICTURES = tmp / "saved"
    tui.JohanApp.warm_up = lambda self: None
    tui.Sidebar.refresh_state = lambda self, messages=None: None
    ja.save_session = ja.log_session = lambda m: None
    try:
        app = tui.JohanApp()
        async with app.run_test(size=(150, 46)) as pilot:
            app._show_webpics({"pictures": pics})
            await pilot.pause()
            cards = list(app.query(tui.WebPicsCard))
            check("the card shows every picture with its site",
                  len(cards) == 1 and len(cards[0]._cells()) == 3)
            check("…and ctrl+o can open them", all(p["path"] in app.openables for p in pics))
            card = cards[0]
            card.focus()
            await pilot.press("right")
            check("→ picks the next picture", card.sel == 1)
            await pilot.press("s")
            check("s saves it", (tmp / "saved" / "1.jpg").exists())
            await pilot.press("o", "w")
            check("o opens the picture, w its source page",
                  opened[-2:] == [pics[1]["path"], "https://site1.org/page"])
            await pilot.press("3")
            await pilot.press("r")
            box = app.query_one("#prompt")
            check("r puts /draw <that picture> in the prompt",
                  box.value == f"/draw {pics[2]['path']} " and box.has_focus)
    finally:
        for (obj, name), val in saved.items():
            setattr(obj, name, val)
        shutil.rmtree(tmp, ignore_errors=True)


main()
import asyncio                         # noqa: E402
asyncio.run(tui_checks())
for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
