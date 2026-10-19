"""Open a local HTML page in headless Chromium and report what happens.

Runs in ~/johan/venv-browser (Playwright lives there, never in the main
venv). Called by johan_tools.try_page with one JSON argument:
    {"path": "/home/zeros/x/game.html", "keys": ["ArrowRight", "Space"],
     "out": "/home/zeros/johan/brain/sandbox/try/<id>"}
Prints one JSON line: errors, visible text, whether the page moves on its
own and after the keys, and two screenshots (on load, after the keys).

Render-only by design: it presses keys he'd press and never clicks links,
types into forms or reaches the network — only file:// pages are opened and
every other request is refused.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SETTLE_MS = 600        # let a game loop run before the first look
KEY_GAP_MS = 120       # between key presses, like a person
AFTER_MS = 1200        # watch this long after the last key


def main():
    job = json.loads(sys.argv[1])
    path = Path(job["path"]).resolve()
    out = Path(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    res = {"path": str(path), "errors": [], "console_errors": [],
           "blocked_requests": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-gpu"])
        page = browser.new_page(viewport={"width": 1000, "height": 750})
        page.on("pageerror", lambda e: res["errors"].append(str(e)[:300]))
        page.on("console", lambda m: m.type == "error" and
                res["console_errors"].append(m.text[:300]))

        def gate(route):
            url = route.request.url
            if url.startswith(("file://", "data:", "blob:")):
                return route.continue_()
            res["blocked_requests"].append(url[:200])
            return route.abort()
        page.route("**/*", gate)

        page.goto(path.as_uri(), wait_until="load", timeout=15000)
        page.wait_for_timeout(SETTLE_MS)

        # On a game the PLAY AREA is what must change. Comparing the whole
        # page let a "Last key: ArrowRight" debug line pass a snake that
        # never moved — Johan added the line while debugging and the check
        # read the text change as the game working.
        surface = page.locator("canvas")
        use_canvas = surface.count() > 0
        if use_canvas:
            areas = page.evaluate("Array.from(document.querySelectorAll('canvas')).map(c => c.width * c.height)")
            surface = surface.nth(max(range(len(areas)), key=areas.__getitem__))

        def look():
            return surface.screenshot() if use_canvas else page.screenshot()

        a = look()
        page.wait_for_timeout(500)
        b = look()
        res["compared"] = "the game canvas" if use_canvas else "the whole page"
        res["moves_by_itself"] = a != b
        (out / "load.png").write_bytes(page.screenshot())
        res["text_on_load"] = page.inner_text("body")[:600]

        keys = [str(k) for k in (job.get("keys") or [])][:20]
        clicks = [str(c) for c in (job.get("click") or [])][:10]
        fills = [str(v) for v in (job.get("fill") or [])][:8]
        page.mouse.click(5, 5)            # focus the page, like clicking into it
        res["filled"] = []
        for i, value in enumerate(fills):
            # Typed into the page's inputs in order — a calculator or a to-do
            # list can't be tried without typing something first.
            try:
                page.locator("input, textarea").nth(i).fill(value, timeout=2000)
                res["filled"].append(value)
            except Exception:
                res["errors"].append(f"couldn't type {value!r}: the page has no input #{i + 1}")
        res["clicked"] = []
        for label in clicks:
            # Buttons/links by their visible text — a text adventure is played
            # by clicking. Links that leave the page are refused by the gate.
            target = page.get_by_role("button", name=label).or_(page.get_by_text(label)).first
            try:
                target.click(timeout=2000)
                res["clicked"].append(label)
            except Exception:
                res["errors"].append(f"couldn't click {label!r}: nothing on the page has that text")
            page.wait_for_timeout(KEY_GAP_MS * 3)
        for k in keys:
            try:
                page.keyboard.press(k)
            except Exception as e:        # an unknown key name
                res["errors"].append(f"key {k!r}: {e}"[:200])
            page.wait_for_timeout(KEY_GAP_MS)
        page.wait_for_timeout(AFTER_MS)
        c = look()
        (out / "after.png").write_bytes(page.screenshot())
        res["keys_pressed"] = keys
        res["changed_after_keys"] = (b != c) if (keys or clicks) else None
        res["text_after"] = page.inner_text("body")[:600]
        try:
            res["canvas"] = page.evaluate(
                "Array.from(document.querySelectorAll('canvas')).map(c => [c.width, c.height])")
        except Exception:
            res["canvas"] = []
        res["title"] = page.title()
        # His own checks, like try_code's asserts: JS expressions that must be
        # true now, e.g. `snake[0].x > 10` or `document.body.innerText.includes('Score: 1')`.
        res["checks"] = []
        for expr in [c for c in (job.get("checks") or []) if c.strip()][:10]:
            try:
                ok = bool(page.evaluate(f"(() => ({expr}))()"))
                res["checks"].append({"check": expr[:200], "passed": ok})
            except Exception as e:
                res["checks"].append({"check": expr[:200], "passed": False,
                                      "error": str(e).splitlines()[0][:200]})
        try:
            res["buttons"] = page.evaluate(
                "Array.from(document.querySelectorAll('button, [role=button], input[type=button]'))"
                ".map(b => (b.innerText || b.value || '').trim()).filter(Boolean).slice(0, 12)")
        except Exception:
            res["buttons"] = []
        browser.close()
    res["screenshots"] = [str(out / "load.png"), str(out / "after.png")]
    print(json.dumps(res))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                # always one JSON line
        print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:300]}"}))
