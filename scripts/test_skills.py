"""Tests for johan_skills — skills on demand.

    python test_skills.py            # unit tests, embeddings stubbed (no server)
    python test_skills.py --measure  # live: score the labeled set, pick a threshold
    python test_skills.py --live     # live: a matching message gets its skill,
                                     # banter does not

--measure prints, per labeled message, the best skill and its score, then the
lowest score among messages that SHOULD match their skill and the highest
score among those that should match nothing (or matched the wrong skill).
THRESHOLD in johan_skills.py sits between the two.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_skills as js       # noqa: E402

results = {}

# message -> the skill it should load (None = no skill). Written as he talks:
# lowercase, slang, typos. The None cases are deliberately close in topic
# (games, sound, docker, disk) without asking for that skill's procedure.
LABELED = [
    ("my super+t shortcut doesn't open the terminal anymore", "gnome-shortcut"),
    ("can you bind ctrl+alt+f to open firefox", "gnome-shortcut"),
    ("what does super v do on my desktop", "gnome-shortcut"),
    ("i want a hotkey for screenshots of a window", "gnome-shortcut"),
    ("cant hear anything from my headset", "no-sound"),
    ("audio is coming out of the monitor instead of my headset", "no-sound"),
    ("bro my mic isn't working in discord", "no-sound"),
    ("sound just died after waking from suspend", "no-sound"),
    ("open my team fortress autoexec and tell me what binds i have", "game-config"),
    ("where does tf2 keep my binds", "game-config"),
    ("find the settings file for elden ring under proton", "game-config"),
    ("show me what's in my autoexec", "game-config"),
    ("why is my pc so laggy right now", "pc-slow"),
    ("everything is stuttering, what's eating my cpu", "pc-slow"),
    ("my fans are going crazy", "pc-slow"),
    ("the computer feels really slow today", "pc-slow"),
    ("my disk is almost full, help me clean it", "free-disk-space"),
    ("which folders are hogging my ssd", "free-disk-space"),
    ("i need like 50gb free for a game", "free-disk-space"),
    ("searxng seems dead, is the container up?", "docker-trouble"),
    ("immich won't load, check the containers", "docker-trouble"),
    ("postgres container keeps restarting", "docker-trouble"),
    # --- should load nothing -------------------------------------------
    ("bro i got destroyed in tf2 lmao", None),
    ("what's the best class in tf2 for beginners", None),
    ("recommend me some good headphones under 100 dollars", None),
    ("what is docker in simple words", None),
    ("give me 2 jobs to apply to", None),
    ("what's the weather in tunis tomorrow", None),
    ("write a short poem about my cat", None),
    ("how many likely jobs do I have?", None),
    ("what is 1847 * 23?", None),
    ("install htop on my pc", None),
    ("what's the difference between tcp and udp", None),
    ("my cat knocked my coffee over", None),
    ("remember that I prefer PostgreSQL over MySQL", None),
    ("what are the keyboard controls for Lisa the Painful?", None),
    ("is 16gb of ram enough for gaming", None),
    ("my speakers sound amazing with this song", None),
    ("how big is a tf2 install", None),
    ("when is the next tf2 update coming out", None),
]


def check(name, cond):
    results[name] = bool(cond)


# --------------------------------------------------------------------------
# unit tests — deterministic fake embeddings
# --------------------------------------------------------------------------

VOCAB = ["sound", "audio", "headphones", "shortcut", "keybinding", "hotkey",
         "lmao", "tf2", "config"]


def fake_embed(texts):
    """Bag-of-words over a tiny vocabulary, normalised. Calls are counted so
    the cache can be tested."""
    fake_embed.calls += len(texts)
    out = []
    for t in texts:
        words = t.lower().replace(",", " ").split()
        v = [float(sum(1 for w in words if w.startswith(k))) for k in VOCAB]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


fake_embed.calls = 0

SKILL_A = """---
name: no-sound
description: no sound or audio
triggers:
  - my headphones have no sound
---
check wpctl status
"""
SKILL_B = """---
name: gnome-shortcut
description: keyboard shortcut or hotkey
triggers:
  - fix a keybinding
  - set a hotkey shortcut
---
use gsettings
"""


def unit():
    tmp = Path(tempfile.mkdtemp())
    real_dir, real_index = js.SKILLS_DIR, js.INDEX
    js.SKILLS_DIR, js.INDEX = tmp, tmp / ".index.json"
    js._CACHE.update(sig=None, skills=[])
    try:
        (tmp / "no-sound.md").write_text(SKILL_A)
        (tmp / "gnome-shortcut.md").write_text(SKILL_B)
        (tmp / "broken.md").write_text("no front matter here")

        sk = js.load(fake_embed)
        check("loads valid skills, skips a file without front matter",
              sorted(s["name"] for s in sk) == ["gnome-shortcut", "no-sound"])
        check("index written to disk", js.INDEX.exists()
              and set(json.loads(js.INDEX.read_text())) == {"no-sound.md", "gnome-shortcut.md"})

        calls = fake_embed.calls
        js._CACHE.update(sig=None, skills=[])           # new process, same files
        js.load(fake_embed)
        check("unchanged files are not re-embedded", fake_embed.calls == calls)

        (tmp / "no-sound.md").write_text(SKILL_A.replace("check wpctl", "check pactl"))
        js.load(fake_embed)
        check("an edited file is re-embedded (description + 1 trigger)",
              fake_embed.calls == calls + 2)
        check("edited body is the one served",
              any("pactl" in s["body"] for s in js.load(fake_embed)))

        (tmp / "gnome-shortcut.md").unlink()
        js.load(fake_embed)
        check("a deleted skill leaves the index",
              set(json.loads(js.INDEX.read_text())) == {"no-sound.md"})
        (tmp / "gnome-shortcut.md").write_text(SKILL_B)

        m = js.match("my headphones have no sound at all", threshold=0.5, embed=fake_embed)
        check("a matching message gets its skill", m and m["name"] == "no-sound")
        m = js.match("set a hotkey for the terminal", threshold=0.5, embed=fake_embed)
        check("best skill wins", m and m["name"] == "gnome-shortcut")
        check("banter matches nothing",
              js.match("bro i got destroyed in tf2 lmao", threshold=0.5, embed=fake_embed) is None)
        check("short messages skip matching entirely",
              js.match("sound?", threshold=0.0, embed=fake_embed) is None)

        def boom(texts):
            raise ConnectionError("embed server down")
        js._CACHE.update(sig=None, skills=[])
        js.INDEX.unlink()
        check("embed server down -> no skill, no exception",
              js.match("my headphones have no sound", embed=boom) is None)

        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "my headphones have no sound"}]
        js._CACHE.update(sig=None, skills=[])
        real_t = js.THRESHOLD
        js.THRESHOLD = 0.5
        try:
            name = js.inject(msgs, msgs[-1]["content"], embed=fake_embed)
            check("inject appends ONE system message after his message",
                  name == "no-sound" and len(msgs) == 3 and msgs[-1]["role"] == "system"
                  and msgs[-1]["content"].startswith(js.MARK) and msgs[0]["content"] == "sys")
            msgs.append({"role": "user", "content": "my headphones have no sound still"})
            again = js.inject(msgs, msgs[-1]["content"], embed=fake_embed)
            check("the same skill is not injected twice in one conversation",
                  again is None and len(msgs) == 4)
        finally:
            js.THRESHOLD = real_t
    finally:
        js.SKILLS_DIR, js.INDEX = real_dir, real_index
        js._CACHE.update(sig=None, skills=[])
        shutil.rmtree(tmp, ignore_errors=True)

    # The real skills folder parses, and every skill is short enough to append.
    real = [js.parse(f) for f in sorted(js.SKILLS_DIR.glob("*.md"))]
    check("every real skill has front matter + description", real and all(real))
    check("every real skill body is under 1,700 chars",
          all(len(b) <= 1700 for _, b in real if b))
    check("every real skill has at least 4 example triggers",
          all(len(m["triggers"]) >= 4 for m, _ in real))


# --------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------

def measure():
    js._CACHE.update(sig=None, skills=[])
    pos, neg = [], []
    for msg, want in LABELED:
        ranked = js.scores(msg)
        best, skill = ranked[0]
        right = skill["name"] == want
        print(f"{best:.3f}  {skill['name']:<16} want {str(want):<16} "
              f"{'ok ' if right or want is None else 'WRONG'} {msg}")
        if want is None:
            neg.append(best)
        elif right:
            pos.append(best)
        else:
            neg.append(best)      # a wrong-skill top score must stay below the line
    lo, hi = min(pos), max(neg)
    print(f"\nshould-match: min {lo:.3f}  (n={len(pos)})")
    print(f"should-not:   max {hi:.3f}  (n={len(neg)})")
    for t in (0.55, 0.58, 0.60, 0.612, 0.62, 0.64, 0.66, 0.68, 0.70):
        tp = sum(p >= t for p in pos)
        fp = sum(n >= t for n in neg)
        print(f"threshold {t:.3f}: {tp}/{len(pos)} matched, {fp}/{len(neg)} false")
    print(f"current THRESHOLD = {js.THRESHOLD}")


def live():
    import time
    js._CACHE.update(sig=None, skills=[])
    js.load()
    t = time.monotonic()
    hit = js.match("my headphones have no sound since this morning")
    ms = (time.monotonic() - t) * 1000
    check("live: a sound problem gets the no-sound skill", hit and hit["name"] == "no-sound")
    check("live: banter gets no skill", js.match("bro i got destroyed in tf2 lmao") is None)
    check("live: a game-config ask gets game-config",
          (js.match("can you check my tf2 custom config?") or {}).get("name") == "game-config")
    print(f"      match latency, warm index: {ms:.0f} ms")


def main():
    if "--measure" in sys.argv:
        measure()
        return 0
    unit()
    if "--live" in sys.argv:
        live()
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    bad = [k for k, v in results.items() if not v]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
