"""Render one URL in headless Chromium and print its visible text as JSON.

Runs in ~/johan/venv-browser (Playwright lives there, not in the main venv),
called as a subprocess by johan_browser.py so a browser crash or leak can
never take Johan down, and its memory is gone the moment the page is read.

Render-only by design: it loads a page and reads it. It never clicks, types,
logs in or follows anything — a 4B model driving a browser gets lost, and
this exists only to read pages that need JavaScript to show their text.

It does NOT disguise itself: Playwright's default headless user agent is
kept. A site that gates bots (Cloudflare challenge, Reddit's consent wall)
is reported as gated, never worked around — evasion is the same line this
project refused to cross for GulfTalent, TanitJobs and search engines.

    venv-browser/bin/python render_page.py URL [--timeout 20] [--max-chars 20000]
"""

import argparse
import json
import re
import sys

GATES = re.compile(
    r"(just a moment\.\.\.|verify you are human|checking your browser|"
    r"enable javascript and cookies|attention required! \| cloudflare|"
    r"welcome to reddit|you've been blocked|access denied|are you a robot)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--max-chars", type=int, default=20000)
    a = ap.parse_args()
    if not re.match(r"^https?://", a.url):
        print(json.dumps({"error": "only http(s) URLs"}))
        return 0

    from playwright.sync_api import sync_playwright, Error as PWError
    out = {"url": a.url}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
            ctx = browser.new_context(java_script_enabled=True,
                                      accept_downloads=False)
            page = ctx.new_page()
            # Text is all we want: skip the heavy bytes.
            page.route("**/*", lambda route: route.abort()
                       if route.request.resource_type in ("image", "media", "font")
                       else route.continue_())
            resp = page.goto(a.url, wait_until="domcontentloaded",
                             timeout=a.timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except PWError:
                pass                      # busy pages never go idle; read what's there
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            out.update(final_url=page.url, title=page.title(),
                       status=resp.status if resp else None,
                       chars=len(text), text=text[:a.max_chars])
            if len(text) < 2000 and GATES.search(text + " " + out["title"]):
                out["gated"] = True
            browser.close()
    except Exception as e:           # noqa: BLE001 — report, never crash the caller
        out["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
