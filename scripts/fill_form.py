"""Open a job application form, fill it from a plan, and leave it for Riadh.

Runs in ~/johan/venv-browser (Playwright). Two modes:

  --extract URL      headless: print the form's fields as JSON and exit.
                     Reads only — nothing is typed, uploaded or clicked.
  --fill URL         VISIBLE window. Prints the fields as one JSON line,
                     reads a fill plan as one JSON line on stdin, fills it,
                     highlights every field, shows a banner, prints
                     {"filled": ...}, then stays open until Riadh closes the
                     window.

THE ONE RULE: this script never clicks anything. Not Submit, not "Next",
not a cookie banner. It types into fields and attaches the CV; the Submit
button is Riadh's (CLAUDE.md, changed 2026-09-23 at his request). There is
no code path here that calls .click() on a page element.

Colours: green = filled from his profile/answers file, yellow = drafted by
the model (read it), red = needs him (required and unknown).
"""

import argparse
import json
import sys

EXTRACT_JS = r"""
() => {
  const root = document.querySelector('#application-form, form#application_form, form[action*="application"], form') || document;
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(n => n.innerText).join(' ').trim(); if (t) return t; }
    let p = el.closest('label'); if (p && p.innerText.trim()) return p.innerText.trim();
    p = el.closest('fieldset'); if (p) { const lg = p.querySelector('legend'); if (lg) return lg.innerText.trim(); }
    let c = el.parentElement;
    for (let i = 0; i < 4 && c; i++, c = c.parentElement) {
      const l = c.querySelector('label, legend, [class*="label"], [class*="question"]');
      if (l && l.innerText.trim() && l.innerText.trim().length < 400) return l.innerText.trim();
    }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const fields = []; const seenRadio = new Set();
  let n = 0;
  for (const el of root.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    // Radio siblings were already captured with their group: skip BEFORE
    // tagging, or this overwrites the per-option markers the filler uses.
    if (type === 'radio' && el.name && seenRadio.has(el.name)) continue;
    const key = `f${n++}`;
    el.setAttribute('data-johan', key);
    if (type === 'radio' || type === 'checkbox') {
      if (type === 'radio' && el.name) seenRadio.add(el.name);
      const members = type === 'radio' && el.name ? [...root.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
      const opts = members.map((m, i) => { m.setAttribute('data-johan-group', key); m.setAttribute('data-johan-idx', String(i)); return (m.closest('label')?.innerText || labelFor(m) || m.value || '').trim(); });
      const fs = el.closest('fieldset'); const q = fs?.querySelector('legend')?.innerText?.trim();
      fields.push({key, kind: type, name: el.name, label: (q || labelFor(el)).slice(0, 400), options: opts, required: el.required});
      continue;
    }
    const f = {key, kind: type === 'select' || el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : type),
               name: el.name, id: el.id, label: labelFor(el).slice(0, 400),
               required: el.required || el.getAttribute('aria-required') === 'true'};
    if (el.tagName === 'SELECT') f.options = [...el.options].map(o => o.text.trim()).filter(t => t);
    if (type === 'file') f.accept = el.getAttribute('accept') || '';
    fields.push(f);
  }
  return {title: document.title, url: location.href, fields};
}
"""

HIGHLIGHT_JS = r"""
(plan) => {
  const colour = {profile: '#2e7d32', answers: '#2e7d32', drafted: '#f9a825', needs_you: '#c62828'};
  for (const [key, how] of Object.entries(plan)) {
    const el = document.querySelector(`[data-johan="${key}"]`) || document.querySelector(`[data-johan-group="${key}"]`);
    if (!el) continue;
    const box = el.type === 'file' ? (el.closest('div') || el) : el;
    box.style.outline = `3px solid ${colour[how] || '#c62828'}`;
    box.style.outlineOffset = '2px';
  }
}
"""

HINT_JS = r"""
(hints) => {
  for (const [key, text] of Object.entries(hints)) {
    const el = document.querySelector(`[data-johan="${key}"]`) || document.querySelector(`[data-johan-group="${key}"]`);
    if (!el) continue;
    const n = document.createElement('div');
    n.textContent = 'Johan: ' + text;
    n.style.cssText = 'color:#c62828;font:12px system-ui,sans-serif;margin:4px 0 8px';
    (el.closest('div') || el).insertAdjacentElement('afterend', n);
  }
}
"""

TEST_STATE_JS = r"""
() => ({
  values: Object.fromEntries([...document.querySelectorAll('[data-johan]')].map(e =>
      [e.getAttribute('data-johan'), e.type === 'file' ? [...e.files].map(f => f.name)
       : (e.type === 'radio' || e.type === 'checkbox') ? e.checked : e.value])),
  radios: Object.fromEntries([...document.querySelectorAll('[data-johan-idx]')].map(e =>
      [e.getAttribute('data-johan-group') + '_' + e.getAttribute('data-johan-idx'), e.checked])),
  submitted: window.__submitted === true,
  banner: !!document.getElementById('johan-banner'),
  hints: [...document.querySelectorAll('div')].filter(d => d.textContent.startsWith('Johan: ')).length,
})
"""

BANNER_JS = r"""
(msg) => {
  const b = document.createElement('div');
  b.id = 'johan-banner';
  b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#1a1b26;color:#c0caf5;font:14px/1.4 system-ui,sans-serif;padding:10px 16px;border-bottom:3px solid #7aa2f7;box-shadow:0 2px 8px rgba(0,0,0,.4)';
  b.innerHTML = msg;
  document.body.appendChild(b);
  document.body.style.marginTop = (b.offsetHeight + 8) + 'px';
}
"""


def _open(p, url, headless):
    browser = p.chromium.launch(headless=headless, args=["--disable-gpu"] if headless else [])
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=False)
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    # Ashby renders the form client-side; give it a moment to appear.
    try:
        page.wait_for_selector("input, textarea, select", timeout=8000)
    except Exception:
        pass
    return browser, page


def _fill(page, fields, plan):
    """Type values and attach files. Returns {key: status}. Never clicks."""
    by_key = {f["key"]: f for f in fields}
    done = {}
    for key, item in plan.get("values", {}).items():
        f = by_key.get(key)
        if not f:
            continue
        value, how = item.get("value"), item.get("how", "profile")
        try:
            if f["kind"] == "file":
                page.set_input_files(f'[data-johan="{key}"]', value)
            elif f["kind"] == "select":
                page.select_option(f'[data-johan="{key}"]', label=value)
            elif f["kind"] in ("radio", "checkbox"):
                idx = f["options"].index(value) if value in f["options"] else None
                if idx is None:
                    done[key] = "needs_you"
                    continue
                # .check() sets the input's state; it is not a click on a button.
                page.locator(f'[data-johan-group="{key}"][data-johan-idx="{idx}"]').first.check()
            else:
                page.fill(f'[data-johan="{key}"]', str(value))
            done[key] = how
        except Exception as e:           # noqa: BLE001 — one bad field must not stop the rest
            done[key] = "needs_you"
            print(json.dumps({"warn": f"{key}: {type(e).__name__}: {str(e)[:120]}"}),
                  file=sys.stderr)
    for key in plan.get("needs_you", []):
        done.setdefault(key, "needs_you")
    # A one-line note under each red field: why Johan left it, and his saved
    # answer when there is one — so the React dropdowns Johan will not touch
    # are one click each for him.
    page.evaluate(HINT_JS, plan.get("hints", {}))
    return done


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--extract", metavar="URL")
    mode.add_argument("--fill", metavar="URL")
    ap.add_argument("--test", action="store_true",
                    help="with --fill: headless, print the filled state and exit "
                         "(for tests against a local mock form — no window)")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        if a.extract:
            browser, page = _open(p, a.extract, headless=True)
            print(json.dumps(page.evaluate(EXTRACT_JS)))
            browser.close()
            return 0

        browser, page = _open(p, a.fill, headless=a.test)
        form = page.evaluate(EXTRACT_JS)
        print(json.dumps(form), flush=True)
        plan = json.loads(sys.stdin.readline() or "{}")
        done = _fill(page, form["fields"], plan)
        page.evaluate(HIGHLIGHT_JS, done)
        counts = {k: sum(1 for v in done.values() if v == k)
                  for k in ("profile", "answers", "drafted", "needs_you")}
        page.evaluate(BANNER_JS,
            "<b>Johan filled this form — Johan never submits.</b> "
            f"<span style='color:#9ece6a'>■ {counts['profile'] + counts['answers']} from your profile</span> · "
            f"<span style='color:#e0af68'>■ {counts['drafted']} drafted, read them</span> · "
            f"<span style='color:#f7768e'>■ {counts['needs_you']} need you</span>. "
            "Check every field, then click Submit yourself. Close this window to discard.")
        print(json.dumps({"filled": done, "counts": counts}), flush=True)
        if a.test:
            print(json.dumps(page.evaluate(TEST_STATE_JS)), flush=True)
            browser.close()
            return 0
        # Stay open until he closes the window. No timeout: it is his form now.
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
