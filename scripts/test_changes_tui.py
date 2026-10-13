"""Headless pilot check of the Changes keys and rewind — no model, no root.

Drives the real JohanApp with Textual's pilot. Every store is swapped to a
temp file (pending changes, the actions log, allow rules, edits), the model
warm-up and session saving are stubbed out, and the only things that
actually run are mkdirs inside a scratch folder under ~/.cache that is
deleted afterwards.

    python test_changes_tui.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_tui as tui              # noqa: E402
import johan_actions as jx           # noqa: E402
import johan_tools as jt             # noqa: E402
import johan_assistant as ja         # noqa: E402

out = {}


async def wait_for(pilot, cond, secs=10):
    for _ in range(int(secs / 0.2)):
        await pilot.pause(0.2)
        if cond():
            return True
    return cond()


def notes(app):
    return " ".join(str(n.render()) for n in app.query(tui.Note))


async def drive(scratch):
    app = tui.JohanApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(0.5)

        prompt = app.query_one("#prompt")

        # --- a change waiting from earlier shows up as a card in the chat
        r = jx.propose_action(f"mkdir {scratch}/box", why="tidy the test folder")
        app._show_backlog()
        await pilot.pause(0.3)
        card = app.backlog_card
        out["a waiting change shows as a card in the chat"] = (
            card is not None and card.earlier and not (scratch / "box").exists())
        out["…offering 'always allow' for a rule-able change"] = card is not None and len(card.options) == 3
        out["…starting on No, so a habitual enter can't approve it"] = (
            card is not None and card.choices.index == len(card.options) - 1)
        await pilot.press("2")
        out["2 runs it"] = await wait_for(
            pilot, lambda: (scratch / "box").is_dir() and not jx.list_actions())
        out["…and saves a rule for that folder"] = [jx.rule_text(x) for x in jx.load_rules()] == [
            f"create folders in {scratch}"]
        r = jx.propose_action(f"mkdir {scratch}/box2", why="t")
        out["the next matching change runs without asking"] = (
            r.get("auto_allowed") and (scratch / "box2").is_dir())

        # --- several waiting: one after another; 3 = no
        jx.remove_rule(1)
        jx.propose_action(f"mkdir {scratch}/a1", why="t")
        jx.propose_action(f"mkdir {scratch}/a2", why="t")
        app._show_backlog()
        await pilot.pause(0.3)
        await pilot.press("3")
        await wait_for(pilot, lambda: app.backlog_card is not None
                       and app.backlog_card.item.get("steps", [{}])[0].get("shown", "").endswith("a2"))
        out["3 declines it and the next one is shown"] = (
            not (scratch / "a1").exists() and app.backlog_card is not None)

        # --- typing instead leaves it waiting for next time
        prompt.focus()
        prompt.value = "/rules"
        await pilot.press("enter")
        await pilot.pause(0.3)
        out["typing leaves the change waiting"] = (
            app.backlog_card is None and len(jx.list_actions()) == 1
            and not (scratch / "a2").exists())
        jx.discard_action(jx.list_actions()[0]["id"])

        # --- /rules lists them; forget removes
        r = jx.propose_action(f"mkdir {scratch}/box3", why="t")
        app._show_backlog()
        await pilot.pause(0.3)
        await pilot.press("2")
        await wait_for(pilot, lambda: (scratch / "box3").is_dir())
        prompt.focus()
        prompt.value = "/rules"
        await pilot.press("enter")
        await pilot.pause(0.3)
        out["/rules lists the rule"] = f"create folders in {scratch}" in notes(app)
        prompt.value = "/rules forget 1"
        await pilot.press("enter")
        await pilot.pause(0.3)
        out["/rules forget 1 removes it"] = not jx.load_rules()

        # --- /changes lists, /changes undo asks and reverts
        prompt.value = "/changes"
        await pilot.press("enter")
        await pilot.pause(0.3)
        out["/changes lists recent changes"] = "recent changes" in notes(app)
        prompt.value = "/changes undo"
        await pilot.press("enter")
        await pilot.pause(0.5)
        out["/changes undo asks with a card"] = app.backlog_card is not None
        await pilot.press("1")
        out["…1 reverts the last change"] = await wait_for(pilot, lambda: not (scratch / "box3").exists())

        # --- rewind with nothing changed: instant, question back in the box
        await wait_for(pilot, lambda: app.backlog_card is None)
        sys_msg = app.messages[0]
        app.messages = [sys_msg]
        tid = jx.new_turn()
        app.turn_ids[1] = tid
        app.messages += [{"role": "user", "content": "hello there"},
                         {"role": "assistant", "content": "hi!"}]
        jx.end_turn()
        prompt.value = ""
        await pilot.press("ctrl+z")
        await pilot.pause(0.3)
        out["ctrl+z with no changes: instant, no dialog"] = (
            not isinstance(app.screen, tui.RewindScreen) and app.messages == [app.messages[0]]
            and len(app.messages) == 1)
        out["…the question is back in the box"] = prompt.value == "hello there"

        # --- rewind a turn that changed something: one confirmation
        def turn_with_change(name):
            tid = jx.new_turn()
            app.turn_ids[len(app.messages)] = tid
            app.messages.append({"role": "user", "content": f"make {name}"})
            r = jx.propose_action(f"mkdir {scratch}/{name}", why="t")
            jx.end_turn()
            jx.run_action(r["action_id"])
            app.messages.append({"role": "assistant", "content": "done"})

        turn_with_change("rw")
        prompt.value = ""
        await pilot.press("ctrl+z")
        await pilot.pause(0.3)
        out["ctrl+z after a change asks once"] = isinstance(app.screen, tui.RewindScreen)
        shown = " ".join(str(w.render()) for w in app.screen.query(tui.Static))
        out["…listing exactly what will be reverted"] = f"rmdir {scratch}/rw" in shown
        await pilot.press("n")
        await pilot.pause(0.3)
        out["n cancels: nothing reverted, chat kept"] = (
            (scratch / "rw").is_dir() and len(app.messages) == 3)
        await pilot.press("ctrl+z")
        await pilot.pause(0.3)
        await pilot.press("y")
        out["y reverts the change"] = await wait_for(pilot, lambda: not (scratch / "rw").exists())
        await pilot.pause(0.3)
        out["…drops the turn and puts the question back"] = (
            len(app.messages) == 1 and prompt.value == "make rw")

        prompt.value = "/rewind"
        await pilot.press("enter")
        await pilot.pause(0.3)
        out["/rewind with nothing left says so, doesn't crash"] = len(app.messages) == 1
        await pilot.press("ctrl+q")


def main():
    tmp = Path(tempfile.mkdtemp())
    saved = (jx.PENDING, jx.LOG, jx.ALLOW_RULES, jx.EDITS_LOG, jt.PENDING_EDITS,
             jt.EDIT_BACKUPS, ja.save_session, ja.log_session, ja.prime_with_memories,
             tui.JohanApp.warm_up)
    jx.PENDING, jx.LOG, jx.ALLOW_RULES, jx.EDITS_LOG = (
        tmp / "p.json", tmp / "l.json", tmp / "rules.json", tmp / "el.json")
    jt.PENDING_EDITS, jt.EDIT_BACKUPS = tmp / "pe.json", tmp / "eb"
    ja.save_session = ja.log_session = lambda *a, **k: None
    ja.prime_with_memories = lambda p: p
    tui.JohanApp.warm_up = lambda self: None
    scratch = Path.home() / ".cache/johan-changes-tui-test"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        asyncio.run(drive(scratch))
    finally:
        (jx.PENDING, jx.LOG, jx.ALLOW_RULES, jx.EDITS_LOG, jt.PENDING_EDITS,
         jt.EDIT_BACKUPS, ja.save_session, ja.log_session, ja.prime_with_memories,
         tui.JohanApp.warm_up) = saved
        jx.end_turn()
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    for k, v in out.items():
        print(f"{'ok  ' if v else 'FAIL'}  {k}")
    bad = [k for k, v in out.items() if not v]
    print(f"\n{len(out) - len(bad)}/{len(out)} pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
