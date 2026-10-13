"""Slash commands from files (commands/*.md) — loading, expansion, name
clashes, live reload, and the TUI path end to end with the model stubbed.

No model, no GPU, no database writes, nothing under brain/ is read or
written: every data source the UI touches is stubbed or pointed at a temp
dir before the app starts.

    python test_commands.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_commands as jc        # noqa: E402

results = {}


def check(name, ok, detail=""):
    results[name] = bool(ok)
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))


def write(d, name, text):
    (d / name).write_text(text, encoding="utf-8")


def unit(tmp):
    write(tmp, "tldr.md", "---\ndescription: summarise a page\nargs: <url>\n---\n"
                          "Read $ARGS and give me a TL;DR.\n")
    write(tmp, "standup.md", "Draft my standup from my open tasks.\n")
    write(tmp, "braces.md", "---\ndescription: \"quoted\"\n---\nSay {{ args }} twice: {{args}}\n")
    write(tmp, "draw.md", "this must never run\n")                 # clashes with a built-in
    write(tmp, "Bad Name.md", "spaces are not a command name\n")
    write(tmp, "empty.md", "---\ndescription: nothing below\n---\n")
    write(tmp, "notes.txt", "not markdown\n")

    cmds = jc.load(tmp)
    check("loads valid .md files only", set(cmds) == {"tldr", "standup", "braces", "draw"},
          sorted(cmds))
    check("front matter: description and args hint",
          cmds["tldr"]["description"] == "summarise a page" and cmds["tldr"]["args"] == "<url>")
    check("front matter is not part of the template", "---" not in cmds["tldr"]["template"])
    check("quoted front-matter values are unquoted", cmds["braces"]["description"] == "quoted")

    check("$ARGS is replaced", jc.expand(cmds["tldr"], "https://x.org")
          == {"prompt": "Read https://x.org and give me a TL;DR."})
    check("{{args}} (any spacing, every occurrence) is replaced",
          jc.expand(cmds["braces"], "hi")["prompt"] == "Say hi twice: hi")
    check("args are inserted literally (no regex escapes)",
          jc.expand(cmds["tldr"], r"a\1b $0")["prompt"] == r"Read a\1b $0 and give me a TL;DR.")
    check("no placeholder: args appended as a paragraph",
          jc.expand(cmds["standup"], "focus on jobs")["prompt"]
          == "Draft my standup from my open tasks.\n\nfocus on jobs")
    check("no placeholder, no args: template alone",
          jc.expand(cmds["standup"])["prompt"] == "Draft my standup from my open tasks.")
    check("missing required args → usage, not an empty prompt",
          jc.expand(cmds["tldr"], "  ") == {"error": "usage: /tldr <url>"})

    builtins = {"draw", "new", "help"}
    check("a built-in name is excluded from the list", "draw" not in jc.load(tmp, exclude=builtins))
    check("…with or without its slash", "draw" not in jc.load(tmp, exclude={"/draw"}))
    check("resolve: built-in name → None (built-in wins)",
          jc.resolve("/draw a castle", builtins=builtins, directory=tmp) is None)
    check("resolve: file command → expanded prompt",
          jc.resolve("/TLDR https://a.b", builtins=builtins, directory=tmp)
          == {"name": "tldr", "prompt": "Read https://a.b and give me a TL;DR."})
    check("resolve: Telegram's /cmd@BotName form",
          jc.resolve("/tldr@johanbot u", builtins=builtins, directory=tmp)["prompt"]
          == "Read u and give me a TL;DR.")
    check("resolve: unknown → None", jc.resolve("/nosuch", directory=tmp) is None)
    check("resolve: not a slash command → None", jc.resolve("tldr x", directory=tmp) is None)
    check("usage string", jc.usage(cmds["tldr"]) == "/tldr <url>" and jc.usage(cmds["standup"]) == "/standup")

    # live reload: a new file and an edited one show up without a restart
    write(tmp, "explain.md", "Explain $ARGS simply.\n")
    p = tmp / "standup.md"
    p.write_text("Draft a SHORT standup.\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))   # coarse-mtime filesystems
    again = jc.load(tmp)
    check("a new file appears without a restart", "explain" in again)
    check("an edited file is re-read", again["standup"]["template"] == "Draft a SHORT standup.")
    (tmp / "explain.md").unlink()
    check("a deleted file disappears", "explain" not in jc.load(tmp))

    shipped = jc.load()
    check("the shipped examples load", {"explain", "translate", "tldr"} <= set(shipped), sorted(shipped))
    check("every shipped example has a description and an args hint",
          all(shipped[n]["description"] and shipped[n]["args"] for n in ("explain", "translate", "tldr")))


# ---------------------------------------------------------------- TUI pilot

def stub_world(tmp):
    """Point every data source the UI reads at stubs or temp files."""
    import johan_assistant as ja
    import johan_tools as jt
    import johan_actions as jx
    import apply_queue as aq
    jx.PENDING, jx.LOG = tmp / "pa.json", tmp / "al.json"
    jt.PENDING_EDITS = tmp / "pe.json"
    ja.prime_with_memories = lambda s, *a, **k: s
    ja.save_session = ja.log_session = lambda *a, **k: None
    ja.list_sessions = lambda *a, **k: []
    ja.get_pipeline_stats = lambda: {"by_verdict": {}, "cvs_ready": 0}
    ja.applied_ids = lambda: set()
    ja.release_model = lambda: None
    jt.list_tasks = lambda *a, **k: {"count": 0, "tasks": []}
    jt.system_status = lambda: {}
    aq.latest_queue = lambda: None
    try:
        import johan_images as ji
        ji.list_images = lambda *a, **k: []
    except Exception:
        pass
    return ja


async def pilot(tmp):
    ja = stub_world(tmp)
    import johan_tui as tui
    from textual.widgets import Input
    tui._searx_ok = lambda: False
    sent = []

    def fake_run_turn(messages, on_tool=None, on_delta=None, should_stop=None, **kw):
        sent.append(messages[-1]["content"])
        on_delta("ok")
        return "ok"
    ja.run_turn = fake_run_turn

    class App(tui.JohanApp):
        def warm_up(self):
            pass

    app = App()
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        box = app.query_one("#prompt")
        box.focus()
        await p.press("slash", "t", "l")
        await p.pause(0.3)
        check("/ completion offers a file command", box._suggestion == "/tldr ", repr(box._suggestion))
        await p.press("tab")
        await p.pause(0.1)
        check("tab completes it", box.value == "/tldr ", repr(box.value))
        box.value = "/dr"
        box.cursor_position = 3
        await p.pause(0.3)
        check("completion still offers the built-in /draw", box._suggestion.startswith("/draw"),
              repr(box._suggestion))

        box.value = "/tldr https://example.org/a"
        await p.press("enter")
        for _ in range(30):
            await p.pause(0.1)
            if sent and not app.busy:
                break
        check("running it sends the expanded prompt",
              sent == ["Read https://example.org/a and give me a TL;DR."], repr(sent))
        shown = [str(w.render()) for w in app.query(tui.UserMsg)]
        check("the chat shows what he typed, not the template",
              any("/tldr https://example.org/a" in s for s in shown), repr(shown))
        check("the model got the expanded prompt in history",
              any(m.get("role") == "user"
                  and m.get("content") == "Read https://example.org/a and give me a TL;DR."
                  for m in app.messages))

        sent.clear()
        box.value = "/tldr"
        await p.press("enter")
        await p.pause(0.3)
        notes = [str(n.render()) for n in app.query(tui.Note)]
        check("missing args explains usage, sends nothing",
              not sent and any("usage: /tldr <url>" in n for n in notes), repr(notes[-1:]))

        box.value = "/draw"
        await p.press("enter")
        await p.pause(0.3)
        notes = [str(n.render()) for n in app.query(tui.Note)]
        check("a built-in beats a file with its name", not sent and any("/draw needs tags" in n for n in notes))

        await p.press("f1")
        await p.pause(0.4)
        md = tui.help_markdown()
        check("F1 help lists file commands", "/tldr <url>" in md and "summarise a page" in md)
        check("…but not one shadowing a built-in", "never run" not in md)
        await p.press("escape")
        await p.pause(0.2)

        provider = tui.JohanCommands(app.screen)
        hits = [h async for h in provider.discover()]
        labels = [str(getattr(h, "display", "")) for h in hits]
        check("the command palette lists file commands", any("/tldr <url>" in x for x in labels), labels[-4:])
        tldr = next(h for h in hits if "/tldr" in str(h.display))
        tldr.command()
        await p.pause(0.2)
        check("picking one from the palette puts it in the prompt", box.value == "/tldr ", repr(box.value))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="johan-cmds-"))
    real_dir = jc.COMMANDS_DIR
    try:
        unit(tmp)
        jc.COMMANDS_DIR = tmp
        asyncio.run(pilot(tmp))
    finally:
        jc.COMMANDS_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [k for k, ok in results.items() if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} pass" + (f" — FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
