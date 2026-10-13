"""Johan on Telegram — talk to the assistant from your phone.

Why this shape:
  - **Long polling**, not a webhook. A webhook needs a public HTTPS endpoint;
    this machine is behind a router and shut down nightly. `getUpdates` with
    a 30s timeout costs nothing and needs no inbound port.
  - **An allowlist is mandatory, not optional.** This bot can read his
    memories, his job pipeline, his tasks and files under ~/johan. A bot
    token is a bearer credential: anyone who has it — or who simply finds the
    bot's @name and messages it — reaches this code. Unknown chat ids get one
    refusal and a log line, never a tool call. First run prints the chat id
    so pairing is a copy-paste, not a guess.
  - **No new dependency.** The Bot API is plain HTTP; `requests` is already
    installed for everything else here.
  - **Respects johan-pause.** If Ollama is stopped (GameMode), it says so
    instead of hanging or erroring — the GPU belongs to the game.
  - Nothing here submits an application. /apply marks what HE already did.

Setup:
  1. Telegram → @BotFather → /newbot → copy the token
  2. add to ~/johan/compose/.env:   TELEGRAM_TOKEN=123456:ABC...
  3. run this, message the bot once; it replies with your chat id
  4. add   TELEGRAM_CHAT_ID=<that id>   to .env and restart

    python johan_telegram.py            # run the bridge
    python johan_telegram.py --check    # verify config/token without polling
"""

import argparse
import html
import json
import sys
import time
import traceback
from pathlib import Path

import requests

ROOT = Path.home() / "johan"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import johan_assistant as ja      # noqa: E402
import johan_tools as jt          # noqa: E402
import apply_queue as aq          # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4000                   # Telegram hard-caps a message at 4096
POLL_TIMEOUT = 30
STATE = ROOT / "brain/telegram-state.json"
HISTORY_TURNS = 24                # per-chat messages kept for context


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def env():
    out = {}
    path = ROOT / "compose/.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


class Bot:
    def __init__(self, token, allowed):
        self.token = token
        self.allowed = allowed          # set of int chat ids, empty = pairing
        self.session = requests.Session()
        self.histories = {}
        self.pending_ref = {}          # chat -> picture he sent without a caption
        self.offset = self._load_offset()

    # ---------------------------------------------------------- transport

    def call(self, method, http_timeout=40, files=None, **params):
        url = API.format(token=self.token, method=method)
        try:
            if files:
                r = self.session.post(url, data=params, files=files,
                                      timeout=http_timeout)
            else:
                r = self.session.post(url, json=params, timeout=http_timeout)
            body = r.json()
            if not body.get("ok"):
                print(f"[tg] {method} failed: {body.get('description')}", flush=True)
            return body
        except requests.RequestException as e:
            print(f"[tg] {method} network error: {type(e).__name__}", flush=True)
            return {"ok": False}

    def send(self, chat, text, reply_to=None):
        """Send text, split to fit Telegram's limit.

        Markdown is attempted then abandoned on failure: model output contains
        stray asterisks and underscores often enough that a parse error would
        silently drop the whole message, which looks like the bot ignoring
        you."""
        sent = None
        for chunk in split_message(text):
            body = self.call("sendMessage", chat_id=chat, text=chunk,
                             parse_mode="Markdown",
                             link_preview_options={"is_disabled": True},
                             reply_to_message_id=reply_to)
            if not body.get("ok"):
                body = self.call("sendMessage", chat_id=chat, text=chunk,
                                 link_preview_options={"is_disabled": True})
            sent = body.get("result", {}).get("message_id") or sent
            reply_to = None
        return sent

    def edit(self, chat, message_id, text):
        if not message_id:
            return
        text = text[:TG_LIMIT]
        body = self.call("editMessageText", chat_id=chat, message_id=message_id,
                         text=text, parse_mode="Markdown",
                         link_preview_options={"is_disabled": True})
        if not body.get("ok"):
            self.call("editMessageText", chat_id=chat, message_id=message_id,
                      text=text, link_preview_options={"is_disabled": True})

    def typing(self, chat):
        self.call("sendChatAction", chat_id=chat, action="typing", http_timeout=10)

    def send_pdf(self, chat, path, caption=""):
        p = Path(path)
        if not p.exists():
            return False
        with p.open("rb") as fh:
            body = self.call("sendDocument", http_timeout=120,
                             files={"document": (p.name, fh, "application/pdf")},
                             chat_id=chat, caption=caption[:1000])
        return bool(body.get("ok"))

    def download_photo(self, photos):
        """Save the largest size of a photo he sent; returns the path or None.
        Lands in brain/uploads — NOT brain/images, so it never counts as a
        picture Johan drew (sexual edits are only allowed on those)."""
        try:
            best = max(photos, key=lambda ph: ph.get("file_size") or 0)
            info = self.call("getFile", file_id=best["file_id"]).get("result", {})
            r = requests.get(f"https://api.telegram.org/file/bot{self.token}/"
                             f"{info['file_path']}", timeout=60)
            r.raise_for_status()
            import johan_images as ji
            ji.UPLOADS.mkdir(parents=True, exist_ok=True)
            out = ji.UPLOADS / f"tg-{time.strftime('%Y%m%d-%H%M%S')}{Path(info['file_path']).suffix or '.jpg'}"
            out.write_bytes(r.content)
            return out
        except Exception:
            traceback.print_exc()
            return None

    def send_photo(self, chat, path, caption=""):
        p = Path(path)
        if not p.exists():
            return False
        with p.open("rb") as fh:
            body = self.call("sendPhoto", http_timeout=120,
                             files={"photo": (p.name, fh, "image/png")},
                             chat_id=chat, caption=caption[:1000])
        return bool(body.get("ok"))

    # ---------------------------------------------------------- state

    def _load_offset(self):
        try:
            return json.loads(STATE.read_text()).get("offset", 0)
        except (OSError, ValueError):
            return 0

    def _save_offset(self):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"offset": self.offset}))

    def history(self, chat):
        """Per-chat conversation, primed with his stored memories like the UI."""
        if chat not in self.histories:
            self.histories[chat] = [
                {"role": "system", "content": ja.prime_with_memories(ja.SYSTEM_PROMPT)}]
        return self.histories[chat]


def split_message(text):
    """Split on paragraph, then line, then hard — never mid-word if avoidable."""
    text = (text or "").strip() or "(empty reply)"
    if len(text) <= TG_LIMIT:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        for line in ([para] if len(para) <= TG_LIMIT else
                     [para[i:i + TG_LIMIT] for i in range(0, len(para), TG_LIMIT)]):
            if len(buf) + len(line) + 2 > TG_LIMIT:
                if buf:
                    out.append(buf.strip())
                buf = line
            else:
                buf = f"{buf}\n\n{line}" if buf else line
    if buf.strip():
        out.append(buf.strip())
    return out


def ollama_up():
    try:
        return requests.get("http://127.0.0.1:11434/api/tags", timeout=3).ok
    except requests.RequestException:
        return False


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

HELP = """*Johan*

Just talk to me — I have the same tools as the desktop app.

*Jobs*
/jobs — your apply queue, best first
/cv `<id>` — send me that job's CV as a PDF
/applied `<id>` — record that you applied (I never send anything)
/done — what you've applied to

*Assistant*
/draw `[real] [landscape|square] <description>` — draw now (anime unless `real`); send a photo with `/draw 0.4 <changes>` as its caption to redraw it
/remember `<text>` — keep a fact about you
/memory — what I know about you
/tasks · /task `<text>` · /finish `<id>`
/status — VRAM, services, disk
/refresh — fetch new job postings, drop closed ones (~20 min)
/new — start a fresh conversation
/help — this
"""


def cmd_jobs(bot, chat, arg):
    q = aq.latest_queue()
    if not q or not q.get("jobs"):
        bot.send(chat, "No queue built yet. On the desktop: `johan` → F3 → `r`.")
        return
    applied = ja.applied_ids()
    lines = [f"*Apply queue* — {q['generated']}", ""]
    for i, r in enumerate(q["jobs"][:10], 1):
        mark = "✅ " if r["job_id"] in applied else ""
        warn = ""
        if r.get("gaps_in_title"):
            warn = f"  ⚠️ built around {', '.join(r['gaps_in_title'])}"
        elif r.get("experience_note"):
            warn = f"  ⚠️ {r['experience_note']}"
        lines.append(f"{mark}*{i}. {r['company']} — {r['title']}*")
        lines.append(f"[apply]({r['url']})  ·  fit {r['fit_score']:g}  ·  "
                     f"`/cv {r['job_id']}`{warn}")
        lines.append("")
    lines.append("_`/cv <id>` sends the PDF · `/applied <id>` records it_")
    bot.send(chat, "\n".join(lines))


def _find_job(job_id):
    q = aq.latest_queue() or {}
    for r in q.get("jobs", []):
        if str(r["job_id"]) == str(job_id):
            return r
    return None


def cmd_cv(bot, chat, arg):
    if not arg.strip().isdigit():
        bot.send(chat, "Give me a job id: `/cv 142`  (see /jobs)")
        return
    row = _find_job(arg.strip())
    if not row:
        bot.send(chat, f"Job {arg.strip()} isn't in the current queue. /jobs")
        return
    if not row.get("pdf") or not Path(row["pdf"]).exists():
        bot.send(chat, "No CV built for that one yet.")
        return
    bot.typing(chat)
    caption = (f"{row['company']} — {row['title']}\n{row['url']}")
    if not bot.send_pdf(chat, row["pdf"], caption):
        bot.send(chat, f"Couldn't upload it. Path: `{row['pdf']}`")


def cmd_applied(bot, chat, arg):
    if not arg.strip().isdigit():
        bot.send(chat, "Give me a job id: `/applied 142`")
        return
    res = ja.mark_applied(int(arg.strip()))
    if res.get("error"):
        bot.send(chat, res["error"])
    else:
        bot.send(chat, f"✅ recorded: *{res['company']} — {res['title']}*\n"
                       f"_nothing was sent by me; this just writes it down_")


def cmd_done(bot, chat, arg):
    res = ja.list_applied()
    if not res["count"]:
        bot.send(chat, "Nothing recorded as applied yet.")
        return
    lines = [f"*Applied* ({res['count']})", ""]
    for r in res["applied"]:
        lines.append(f"· {r['applied_on']} — {r['company']}, {r['title']}")
    bot.send(chat, "\n".join(lines))


def cmd_remember(bot, chat, arg):
    if not arg.strip():
        bot.send(chat, "What should I remember? `/remember I prefer X`")
        return
    res = jt.remember(arg.strip(), "fact")
    bot.send(chat, f"Kept it (#{res['memory_id']}).")


def cmd_memory(bot, chat, arg):
    res = jt.memory()
    if not res.get("memories"):
        bot.send(chat, "I don't know anything about you yet.")
        return
    lines = ["*What I know about you*", ""]
    for m in res["memories"]:
        lines.append(f"`{m['memory_id']}` {m['text']}")
    bot.send(chat, "\n".join(lines))


def cmd_tasks(bot, chat, arg):
    res = jt.tasks("list")
    if not res["tasks"]:
        bot.send(chat, "No open tasks.")
        return
    lines = ["*Tasks*", ""] + [f"`{r['task_id']}` {r['text']}" for r in res["tasks"]]
    bot.send(chat, "\n".join(lines))


def cmd_task(bot, chat, arg):
    if not arg.strip():
        bot.send(chat, "`/task buy milk`")
        return
    res = jt.tasks("add", text=arg.strip())
    bot.send(chat, f"Added `{res['task_id']}`.")


def cmd_finish(bot, chat, arg):
    if not arg.strip().isdigit():
        bot.send(chat, "`/finish 3`")
        return
    res = jt.tasks("done", task_id=int(arg.strip()))
    bot.send(chat, res.get("error") or f"Done: {res['text']}")


def cmd_status(bot, chat, arg):
    s = jt.system_status()
    gpu = s.get("gpu", {})
    lines = ["*Machine*", ""]
    if "vram_free_mb" in gpu:
        lines.append(f"VRAM {gpu['vram_used_mb']:,} / {gpu['vram_total_mb']:,} MB")
    loaded = s.get("models_loaded")
    model = loaded[0] if isinstance(loaded, list) and loaded else "unloaded"
    lines.append(f"model {model}")
    for name in ("postgres", "qdrant", "embed_server"):
        lines.append(f"{name}: {s.get(name, '?')}")
    disk = s.get("disk", {})
    if disk:
        lines.append(f"disk {disk['free_gb']} GB free")
    stats = ja.get_pipeline_stats().get("by_verdict", {})
    lines += ["", f"jobs: {stats.get('likely', 0)} likely, "
                  f"{stats.get('unclear', 0)} unclear, "
                  f"{len(ja.applied_ids())} applied"]
    bot.send(chat, "\n".join(lines))


def cmd_new(bot, chat, arg):
    bot.histories.pop(chat, None)
    bot.send(chat, "Fresh conversation. (Memories kept.)")


def cmd_refresh(bot, chat, arg):
    """Same refresh as the desktop's /refresh, in a background thread so the
    bot keeps answering. A second /refresh while one runs shows progress."""
    import threading
    import johan_refresh as jr
    state = getattr(bot, "refresh_state", None)
    if state and state.get("running"):
        bot.send(chat, f"Refresh running: {state['progress']}.")
        return
    bot.refresh_state = {"running": True, "progress": "starting"}
    bot.send(chat, "Fetching new postings and checking them — about 20 min. "
                   "I'll message you when it's done.")

    def work():
        def progress(m):
            bot.refresh_state["progress"] = m
        try:
            res = jr.refresh(progress)
        except Exception as e:
            res = {"error": f"refresh failed: {e}"}
        bot.refresh_state = {"running": False}
        bot.send(chat, jr.describe(res) + ("" if res.get("error") else " /jobs shows them."))
    threading.Thread(target=work, daemon=True).start()


def parse_draw(arg):
    """/draw [real|anime] [landscape|square|portrait] [0.x strength] text —
    the desktop's /draw grammar, either modifier order."""
    size, style, strength = None, "anime", None
    arg = arg.strip()
    for _ in range(3):
        first, _, rest = arg.partition(" ")
        word = first.lower()
        if word in ("landscape", "square", "portrait"):
            size, arg = word, rest.strip()
        elif word in ("real", "realistic", "photo", "anime"):
            style, arg = ("anime" if word == "anime" else "real"), rest.strip()
        else:
            try:
                val = float(first)
            except ValueError:
                break
            if not 0 < val < 1:
                break
            strength, arg = val, rest.strip()
    return size, style, strength, arg


def cmd_draw(bot, chat, arg):
    """Draw straight away, no model call — in a thread so chat keeps working.
    Refused while Ollama is stopped: that means johan-pause/GameMode, and
    starting ComfyUI then would put ~5 GB in VRAM under a running game."""
    import threading
    size, style, strength, prompt = parse_draw(arg)
    if not prompt:
        bot.send(chat, "What should I draw? `/draw [real] [landscape] <description>`"
                 + (" — your picture is kept for it." if chat in bot.pending_ref else ""))
        return
    if not ollama_up():
        bot.send(chat, "Not drawing now — Johan is paused (GameMode/`johan-pause`), "
                       "the GPU belongs to the game. Try again after.")
        return
    if getattr(bot, "drawing", False):
        bot.send(chat, "Still drawing the last one — one at a time on a 6 GB card.")
        return
    ref = bot.pending_ref.pop(chat, None)
    bot.drawing = True
    what = f"{'redrawing your picture' if ref else 'drawing'} ({style}, {size or 'portrait'})"
    bot.send(chat, f"{what[0].upper()}{what[1:]} — about 40 s.")

    def work():
        import johan_images as ji
        try:
            r = ji.generate_image(prompt, size=size, reference=ref,
                                  strength=strength, style=style)
        except Exception as e:
            traceback.print_exc()
            r = {"error": f"{type(e).__name__}: {e}"}
        finally:
            bot.drawing = False
        if r.get("error"):
            bot.send(chat, f"Couldn't draw that: {r['error']}")
        elif not bot.send_photo(chat, r["path"],
                                caption=f"{prompt[:200]} · seed {r.get('seed')}"):
            bot.send(chat, f"Drew it but couldn't send it — saved at `{r['path']}`.")
    threading.Thread(target=work, daemon=True).start()


def cmd_help(bot, chat, arg):
    bot.send(chat, HELP)


COMMANDS = {
    "jobs": cmd_jobs, "cv": cmd_cv, "applied": cmd_applied, "done": cmd_done,
    "remember": cmd_remember, "memory": cmd_memory, "tasks": cmd_tasks,
    "task": cmd_task, "finish": cmd_finish, "status": cmd_status,
    "new": cmd_new, "help": cmd_help, "start": cmd_help, "refresh": cmd_refresh,
    "draw": cmd_draw,
}


# --------------------------------------------------------------------------
# chat turn
# --------------------------------------------------------------------------

def handle_chat(bot, chat, text):
    """Free-form message → the same tool loop the desktop UI uses.

    The placeholder message is edited as tokens arrive, throttled to ~1.5s
    because Telegram rate-limits edits and a per-token edit gets the bot
    throttled into silence."""
    if not ollama_up():
        bot.send(chat, "Ollama is stopped — probably GameMode/`johan-pause`. "
                       "The GPU is busy with a game. Try again after.")
        return

    history = bot.history(chat)
    history.append({"role": "user", "content": text})
    bot.typing(chat)

    placeholder = bot.send(chat, "…")
    state = {"buf": "", "last": 0.0, "tools": []}

    def on_delta(piece):
        state["buf"] += piece
        now = time.monotonic()
        if now - state["last"] >= 1.5:
            state["last"] = now
            bot.edit(chat, placeholder, state["buf"] + " ▌")

    def on_tool(name, args, result):
        state["tools"].append(name)
        bot.typing(chat)
        if name == "generate_image" and isinstance(result, dict) and result.get("path"):
            bot.send_photo(chat, result["path"],
                           caption=f"{result.get('prompt', '')[:200]} · seed {result.get('seed')}")
        if not state["buf"]:
            bot.edit(chat, placeholder, f"_running {name}…_")

    try:
        reply = ja.run_turn(history, on_tool=on_tool, on_delta=on_delta)
    except Exception as e:
        traceback.print_exc()
        bot.edit(chat, placeholder, f"Something broke: {type(e).__name__}: {e}")
        return

    final = (reply or state["buf"] or "(no answer)").strip()

    # Trim history so a long day of chatting can't outgrow the context window
    if len(history) > HISTORY_TURNS:
        bot.histories[chat] = [history[0]] + history[-HISTORY_TURNS:]

    if len(final) <= TG_LIMIT:
        bot.edit(chat, placeholder, final)
    else:
        bot.edit(chat, placeholder, "…")
        bot.send(chat, final)

    # If the answer names CVs, offer them as real files — the whole point of
    # having this on a phone is being able to apply from it.
    for row in (aq.latest_queue() or {}).get("jobs", []):
        if row.get("pdf") and str(row["job_id"]) in final and Path(row["pdf"]).exists():
            bot.send(chat, f"_`/cv {row['job_id']}` sends that PDF_")
            break


def handle_update(bot, update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photos = msg.get("photo") or []
    if chat is None or not (text or photos):
        return

    # ---- the allowlist. Everything below this line can read his data. ----
    if bot.allowed and chat not in bot.allowed:
        who = msg.get("from", {})
        print(f"[tg] REFUSED chat {chat} "
              f"(@{who.get('username')} {who.get('first_name')})", flush=True)
        bot.call("sendMessage", chat_id=chat,
                 text="This bot is private.")
        return
    if not bot.allowed:
        # Pairing mode: no allowlist configured yet. Answer nothing useful,
        # just hand over the id so he can lock it down.
        print(f"[tg] PAIRING: chat id {chat}", flush=True)
        bot.call("sendMessage", chat_id=chat, parse_mode="Markdown", text=(
            f"Not paired yet.\n\nAdd this to `~/johan/compose/.env`:\n"
            f"`TELEGRAM_CHAT_ID={chat}`\n\nthen restart the bridge."))
        return

    # A picture he sent (only downloaded now, after the allowlist).
    if photos:
        path = bot.download_photo(photos)
        if not path:
            bot.send(chat, "Couldn't download that picture.")
            return
        if not text:
            bot.pending_ref[chat] = path
            bot.send(chat, "Got the picture. What should I do with it? "
                           "(e.g. \"redraw it as anime, at night\")")
            return
        bot.pending_ref[chat] = path
    ref = None if text.startswith("/") else bot.pending_ref.pop(chat, None)
    if ref:
        text = (f"{text}\n\n[He sent a picture, saved at {ref}. To redraw or change "
                f"it, call generate_image with reference=\"{ref}\".]")

    if text.startswith("/"):
        head, _, arg = text[1:].partition(" ")
        head = head.split("@")[0].lower()        # /jobs@BotName in groups
        fn = COMMANDS.get(head)
        if fn:
            fn(bot, chat, arg)
        else:
            bot.send(chat, f"No command `/{head}`. /help")
        return

    handle_chat(bot, chat, text)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(bot):
    me = bot.call("getMe").get("result", {})
    print(f"[tg] connected as @{me.get('username')}", flush=True)
    print(f"[tg] allowlist: {sorted(bot.allowed) or 'EMPTY — pairing mode'}",
          flush=True)
    while True:
        # `timeout` here is Telegram's long-poll seconds (a real API param);
        # http_timeout must exceed it or requests aborts every poll.
        body = bot.call("getUpdates", http_timeout=POLL_TIMEOUT + 15,
                        offset=bot.offset, timeout=POLL_TIMEOUT,
                        allowed_updates=["message"])
        for update in body.get("result", []) or []:
            bot.offset = update["update_id"] + 1
            try:
                handle_update(bot, update)
            except Exception:
                traceback.print_exc()
            bot._save_offset()
        if not body.get("ok"):
            time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="validate config and token, then exit")
    args = ap.parse_args()

    cfg = env()
    token = cfg.get("TELEGRAM_TOKEN")
    if not token:
        print("No TELEGRAM_TOKEN in ~/johan/compose/.env\n"
              "  1. Telegram → @BotFather → /newbot\n"
              "  2. add TELEGRAM_TOKEN=<token> to compose/.env", file=sys.stderr)
        return 1

    allowed = {int(x) for x in (cfg.get("TELEGRAM_CHAT_ID") or "").replace(",", " ").split()
               if x.strip().lstrip("-").isdigit()}
    bot = Bot(token, allowed)

    if args.check:
        me = bot.call("getMe")
        if not me.get("ok"):
            print("Token rejected by Telegram.", file=sys.stderr)
            return 1
        u = me["result"]
        print(f"token ok — @{u.get('username')} ({u.get('first_name')})")
        print(f"allowlist: {sorted(allowed) or 'EMPTY (pairing mode)'}")
        print(f"ollama: {'up' if ollama_up() else 'down'}")
        return 0

    run(bot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
