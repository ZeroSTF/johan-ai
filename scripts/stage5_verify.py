"""Stage 5 — verify a generated CV before it goes anywhere near a human eye.

Checks (all must pass, no exceptions):
  1. Every bullet_id in the manifest exists in bullets.yaml.
  2. The manifest's recorded text for each bullet is byte-identical to the
     fact store's text for that id right now (catches drift if bullets.yaml
     was edited after generation, or a future rephrase-by-model version
     introduced a change Stage 4 didn't record honestly).
  3. Every \\resumeItem{...} actually rendered in cv.tex traces back to a
     manifest entry — nothing was hand-inserted without a citation.
  4. No dropped_metric value (25%, 95%, 99.9%, etc.) appears anywhere in the
     rendered CV, regardless of which bullet it might have leaked from.
  5. No known_gaps token (kubernetes, kafka, react, go, rust, ...) appears
     anywhere in the rendered CV.

Usage:
    python stage5_verify.py job-hunter/staged/applications/837-camunda-.../
    python stage5_verify.py --all       # verify everything under applications/
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path.home() / "johan"
FACTS = ROOT / "job-hunter/facts/bullets.yaml"
APPS = ROOT / "job-hunter/staged/applications"
TECTONIC = Path.home() / "bin/tectonic"

ITEM_START = re.compile(r"\\resumeItem\{")


def extract_resume_items(tex):
    """Find every \\resumeItem{...} and return its inner text, respecting
    nested braces (e.g. \\textbf{...} inside the item) via brace counting —
    a non-greedy regex stops at the first '}' and silently matches nothing
    useful once bolding is involved, which is exactly what happened here
    the first time this was written."""
    items = []
    for m in ITEM_START.finditer(tex):
        depth = 1
        start = m.end()
        i = start
        while i < len(tex) and depth > 0:
            if tex[i] == "{":
                depth += 1
            elif tex[i] == "}":
                depth -= 1
            i += 1
        items.append(tex[start:i - 1])
    return items


def unescape_latex(s):
    return (s.replace(r"\&", "&").replace(r"\%", "%").replace(r"\$", "$")
             .replace(r"\#", "#").replace(r"\_", "_").replace(r"\{", "{")
             .replace(r"\}", "}"))


def strip_bold(s):
    return re.sub(r"\\textbf\{([^{}]*)\}", r"\1", s)


def load_facts():
    facts = yaml.safe_load(FACTS.read_text())
    by_id = {b["id"]: b for b in facts["bullets"]}
    return facts, by_id


def verify_one(app_dir, facts, by_id):
    problems = []
    manifest_path = app_dir / "manifest.json"
    cv_path = app_dir / "cv.tex"
    if not manifest_path.exists() or not cv_path.exists():
        return [f"missing manifest.json or cv.tex in {app_dir}"]

    manifest = json.loads(manifest_path.read_text())
    cv_text = cv_path.read_text()

    # 1 & 2: every manifest bullet exists and text matches the fact store now.
    for entry in manifest["bullets"]:
        bid = entry["bullet_id"]
        if bid not in by_id:
            problems.append(f"unknown bullet id '{bid}' — not in bullets.yaml")
            continue
        if by_id[bid]["text"] != entry["text"]:
            problems.append(
                f"{bid}: manifest text does not match current bullets.yaml "
                f"(fact store text changed since generation, or text was "
                f"rephrased without going through a verified path) — "
                f"manifest={entry['text']!r} facts={by_id[bid]['text']!r}"
            )

    # 3: every rendered \resumeItem traces to a manifest entry.
    manifest_texts = {e["text"] for e in manifest["bullets"]}
    rendered = [strip_bold(unescape_latex(raw)).strip()
                for raw in extract_resume_items(cv_text)]
    # Drop the education \resumeItem (Specialization line) — not fact-store sourced.
    rendered = [r for r in rendered if not r.startswith("Specialization:")]
    for r in rendered:
        if r not in manifest_texts:
            problems.append(f"rendered line has no traceable bullet_id: {r!r}")

    # 4: dropped_metric values must never appear. Match the value tied to its
    # unit (e.g. "25\%", "99.9\%") with a non-digit boundary, so a plain "25"
    # can't false-positive against something unrelated like the year "2025".
    for b in facts["bullets"]:
        dm = b.get("dropped_metric")
        if not dm:
            continue
        value = re.escape(str(dm["value"]))
        if dm["unit"] == "percent":
            pattern = re.compile(rf"(?<![\d.]){value}\\?%")
        else:
            pattern = re.compile(rf"(?<![\d.]){value}(?!\d)\s*{re.escape(dm['unit'])}", re.IGNORECASE)
        if pattern.search(cv_text):
            problems.append(
                f"dropped_metric leaked into output: {dm['value']}{'%' if dm['unit']=='percent' else ' ' + dm['unit']} "
                f"({dm['subject']}) from {b['id']} — this number was explicitly removed as indefensible"
            )

    # 5: known_gaps tokens must never appear.
    cv_lower = cv_text.lower()
    for gap in facts["known_gaps"]:
        pattern = re.compile(r"\b" + re.escape(gap) + r"\b", re.IGNORECASE)
        if pattern.search(cv_lower):
            problems.append(f"known_gaps token '{gap}' appears in output — refused skill leaked in")

    if problems:
        return problems  # don't bother compiling something already known to be wrong

    # 6: it must actually compile. A CV that fails to render is not "verified,"
    # and the review/apply step downstream needs a real PDF to upload.
    if TECTONIC.exists():
        r = subprocess.run([str(TECTONIC), "cv.tex"], cwd=app_dir,
                            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not (app_dir / "cv.pdf").exists():
            tail = "\n".join(r.stderr.strip().splitlines()[-10:])
            problems.append(f"cv.tex failed to compile to PDF:\n{tail}")
    else:
        problems.append(f"tectonic not found at {TECTONIC} — cannot confirm cv.tex compiles")

    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", help="one application directory")
    ap.add_argument("--all", action="store_true", help="verify every dir under applications/")
    args = ap.parse_args()

    facts, by_id = load_facts()
    if args.all:
        targets = sorted(p for p in APPS.iterdir() if p.is_dir())
    elif args.dir:
        targets = [Path(args.dir)]
    else:
        ap.error("give a directory or --all")

    any_fail = False
    for t in targets:
        problems = verify_one(t, facts, by_id)
        if problems:
            any_fail = True
            print(f"FAIL  {t.name}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"PASS  {t.name}")

    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
