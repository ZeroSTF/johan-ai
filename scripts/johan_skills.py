"""johan_skills — short instruction packs, loaded only when a message needs one.

A 4B model knows *that* PipeWire exists but not this machine's way of
checking it (which commands its read-only shell allows, that audio services
are protected from propose_action, where Steam lives here). Putting all of
that in the system prompt would make every new chat slower (~230 tok/s
prefill on this card). So each recipe is a markdown file in skills/, and
before hop 0 the user's message is matched against them:

  - bge-m3 similarity on the CPU (the embed server), message vs each skill's
    description and its example `triggers` — the mean of the TWO best
    similarities is the score (one trigger sharing a word like "tf2" with
    banter made the single-best score fire on "when is the next tf2 update")
  - skill vectors are cached on disk (skills/.index.json), keyed by a hash
    of each file, so an edited skill is re-embedded and nothing else is
  - at most ONE skill, and only above THRESHOLD (measured — see
    test_skills.py --measure for the labeled set and the numbers)
  - the skill goes into the conversation as a system message just BEFORE
    his new message. Everything before it is unchanged, so the cached prefix
    (system prompt + tool schemas + earlier turns) is kept; changing the
    system prompt per turn would re-read all of it. Before, not after: with
    the skill as the LAST message the model answered an English question in
    French in 2 of 3 tries; with his message last, 0 of 2 (2026-10-13).

Cost on a turn that matches nothing: one embedding of his message (~0.1 s).
Greetings and very short messages skip even that.

Skill file format:
    ---
    name: no-sound
    description: one line saying what it is for
    triggers:
      - an example of how he'd ask
    ---
    the instructions (keep them under ~1,500 chars)
"""

import hashlib
import json
import re
from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
INDEX = SKILLS_DIR / ".index.json"
# Measured on the 40-message labeled set in test_skills.py (--measure),
# 2026-10-13. The two classes OVERLAP — bge-m3 on short chat messages is
# topical, not intent-aware — so this is a trade, not a clean cut:
#   single-best score: 22/22 matched with 4/18 false, or 19/22 with 3/18
#   top-2 mean (used): at 0.612, 20/22 matched, 1/18 false
#                      ("my speakers sound amazing" -> pc-slow, 0.661)
# A false skill costs ~1.5 s of prefill and is labeled "ignore if it doesn't
# fit"; a missed one just means the model works without the recipe.
THRESHOLD = 0.612
MIN_WORDS = 2
MARK = "Skill for this request"

_CACHE = {"sig": None, "skills": []}


def parse(path):
    """(meta, body) from a skill file, or None if it has no front matter."""
    text = Path(path).read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    if not meta.get("description"):
        return None
    meta.setdefault("name", Path(path).stem)
    meta["triggers"] = [str(t) for t in (meta.get("triggers") or [])]
    return meta, m.group(2).strip()


def _embed(texts):
    import johan_reader as jr
    return jr.embed(texts)


def load(embed=None):
    """All skills with their vectors. Re-embeds only files whose content
    changed since the cached index was written."""
    embed = embed or _embed
    files = sorted(SKILLS_DIR.glob("*.md")) if SKILLS_DIR.is_dir() else []
    hashes = {f.name: hashlib.sha1(f.read_bytes()).hexdigest()[:16] for f in files}
    sig = json.dumps(hashes, sort_keys=True)
    if _CACHE["sig"] == sig:
        return _CACHE["skills"]
    try:
        index = json.loads(INDEX.read_text())
    except Exception:
        index = {}
    skills, dirty = [], False
    for f in files:
        parsed = parse(f)
        if not parsed:
            continue
        meta, body = parsed
        entry = index.get(f.name)
        if not entry or entry.get("hash") != hashes[f.name]:
            texts = [meta["description"]] + meta["triggers"]
            entry = {"hash": hashes[f.name], "vectors": embed(texts)}
            index[f.name] = entry
            dirty = True
        skills.append({"name": meta["name"], "description": meta["description"],
                       "body": body, "vectors": entry["vectors"], "file": f.name})
    for gone in set(index) - set(hashes):
        index.pop(gone)
        dirty = True
    if dirty:
        try:
            INDEX.write_text(json.dumps(index))
        except OSError:
            pass                  # a read-only checkout still works, just slower
    _CACHE.update(sig=sig, skills=skills)
    return skills


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def scores(message, embed=None):
    """[(score, skill)] best first. Score = mean of the two best cosines
    over the skill's description and triggers."""
    embed = embed or _embed
    skills = load(embed)
    if not skills:
        return []
    qv = embed([message])[0]
    out = []
    for s in skills:
        top = sorted((_cos(qv, v) for v in s["vectors"]), reverse=True)[:2]
        out.append((sum(top) / len(top), s))
    return sorted(out, key=lambda x: -x[0])


def match(message, threshold=None, embed=None):
    """The one skill this message needs, or None. Never raises."""
    threshold = THRESHOLD if threshold is None else threshold
    words = re.findall(r"\w+", message or "")
    if len(words) < MIN_WORDS:
        return None
    try:
        ranked = scores(message, embed)
    except Exception:
        return None               # embed server down: answer without a skill
    if ranked and ranked[0][0] >= threshold:
        s = dict(ranked[0][1])
        s["score"] = round(ranked[0][0], 3)
        return s
    return None


# "remove unnecessary stuff from it", right after reading his TF2 config,
# matched free-disk-space (0.72) on its own words and Johan went off running
# du on his home folder. A message that points back has no topic of its own:
# it is matched together with his previous message (-> game-config, 0.65).
# ("there" was dropped: "is there any space left?" is a new topic.)
BACK_REF = re.compile(r"\b(it|its|this|that|these|those|them)\b", re.I)


def _match_text(messages, message):
    if not BACK_REF.search(message or ""):
        return message
    users = [m.get("content") for m in messages
             if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if users and users[-1] == message:
        users = users[:-1]
    return f"{users[-1]}\n{message}" if users else message


def inject(messages, message, embed=None):
    """Insert the matching skill just before his message (once per
    conversation per skill). Returns the skill's name, or None."""
    s = match(_match_text(messages, message), embed=embed)
    if not s:
        return None
    tag = f"{MARK} — {s['name']}"
    if any(m.get("role") == "system" and str(m.get("content", "")).startswith(tag)
           for m in messages):
        return None               # already in this conversation's context
    at = len(messages) - 1 if messages and messages[-1].get("role") == "user" else len(messages)
    messages.insert(at, {"role": "system", "content":
                     f"{tag} (a recipe for this machine; follow it where it fits "
                     f"what he asked, ignore it if it doesn't). Run the read-only "
                     f"checks YOURSELF with run_command — don't ask him to type "
                     f"commands you can run:\n{s['body']}"})
    return s["name"]
