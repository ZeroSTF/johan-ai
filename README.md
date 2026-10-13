# Johan

My own AI assistant, running entirely on one PC: a GTX 1660 Ti with **6 GB of
VRAM**. No paid APIs, nothing leaves the machine. You can talk to it, ask it
things, have it look around the computer, change things (only after I say
yes), draw pictures, and run my job hunt.

It started as a job-hunting pipeline (find postings, figure out which ones I
can actually get hired for, write a tailored CV for each) and grew into a
general assistant with that as one of its jobs.

---

## Using it

```
johan                     open the app
johan "how much disk space is left?"   ask once and exit
johan --continue          reopen the last conversation
```

Type and press **enter** to send. **shift+enter** (or ctrl+j) starts a new
line, and pasting a multi-line block keeps its line breaks. **↑ / ↓** bring
back earlier questions. **esc** stops an answer immediately.

### Screens

| Key | Screen | What it's for |
|---|---|---|
| — | **Chat** | talking to Johan |
| **F2** | **History** | browse, reopen or delete past chats (`d` twice deletes) |
| **F3** | **Jobs** | the apply queue |
| **F4** | **Changes** | everything waiting for my OK: changes to the PC and code edits |
| **F5** | **Images** | everything Johan has drawn |
| **F1** | Help | all of this, inside the app |

Other keys: **ctrl+p** searches every action, **ctrl+n** new chat,
**ctrl+o** opens the last link or file Johan mentioned, **ctrl+y** copies the
last answer, **ctrl+z** rewinds the last turn, **ctrl+b** hides the sidebar,
**ctrl+t** changes the theme.

### Slash commands

Type `/` and press **tab** to complete.

| Command | Does |
|---|---|
| `/draw …` | draw a picture (see *Drawing*) |
| `/research …` | a few minutes of real research, with sources |
| `/retry` | ask the last question again |
| `/rewind` | undo the last question, answer and anything it changed |
| `/copy` | copy the last answer |
| `/new`, `/history` | fresh chat, past chats |
| `/jobs`, `/images`, `/edits` | jump to a screen |
| `/refresh` | fetch new job postings (~20 min, in the background) |
| `/review` | Johan looks at its own recent mistakes and proposes fixes |
| `/rules` | list "always allow" rules (`/rules forget N` removes one) |
| `/forget <id>` | delete something Johan learned or remembered |
| `/uncensored`, `/normal` | swap to an uncensored model for this chat, and back (same tools; a new chat starts normal) |
| `/theme`, `/help` | |

**Your own commands:** drop a markdown file in `commands/` and it becomes a
command. `commands/tldr.md` → `/tldr <url>`. Write the prompt you'd normally
type and put `$ARGS` where the argument goes. It works in the app and on
Telegram without restarting. Comes with `/explain`, `/translate` (English ↔
French ↔ Arabic) and `/tldr`.

---

## What it can do

### Talk, look things up, remember

Just ask. It answers general questions itself and uses tools for anything
about my machine, my files, my jobs, today's date, maths or recent events:
web search (a self-hosted SearXNG, so searches stay private), reading web
pages, Wikipedia, package versions, a calculator, and a Python sandbox for
real computation.

- **Memory.** "Remember that I prefer short answers." It keeps facts about me
  across every chat. "What do you know about me?" lists them.
- **Learns from corrections.** "Stop ending answers with a question" becomes
  a rule it follows from then on. A small note shows what it learned, with
  `/forget N` to undo it.
- **Tasks.** "Add a task to renew my passport", "what's on my list?"
- **Skills.** Built-in know-how for common PC problems (no sound, a broken
  GNOME shortcut, finding a game's config, a slow PC, freeing disk space,
  Docker trouble). They're markdown files in `skills/`, used automatically
  when the question matches, and you can add your own.
- **Fact check.** When an answer relied on tools, Johan checks every fact
  against what the tools returned. If something wasn't backed, it rewrites
  the answer and says so under it ("corrected after a fact check").

### Look around the PC

It can read files anywhere in my home folder, check what's running, look at
logs, disk, GPU, Docker, installed apps and so on, through a **read-only**
shell. Read-only means an allowlist of safe programs, no shell tricks,
and secrets like `~/.ssh` and `.env` files refused.

### Change things on the PC, only when I say yes

"Install htop", "make super+v open the clipboard history", "restart the
embed service", "move these files into a folder": Johan **proposes** the
exact commands and nothing runs. The change waits in **F4 Changes**, showing
what each step does, the command, whether it needs root, and how it will be
undone.

- **y** runs it, **n** throws it away, **u** sets up an undo of the last change.
- **Y** runs it *and* always allows that exact kind of change from now on
  (only low-risk kinds: one setting, one user service, one container).
- Root steps open the system's own password dialog. Johan never sees the
  password.
- Before running, every step is checked against the real system (does that
  package exist, is that setting real, is that theme installed), and after
  running, each change is **verified** to have actually taken effect.
- Files are never deleted, only moved to the Trash.
- The desktop, drivers, kernel, my keys and Johan's own data are off limits
  even with approval.

Code edits work the same way: Johan proposes a diff in F4 and nothing is
written until I press **y**.

### Big requests: plan first

For something with several steps ("set up mpv as my default player and
install it"), Johan shows a short plan first. **enter** approves it,
typing changes it, **esc** cancels. While it works, the plan becomes a live
checklist that ticks off steps.

When a request is genuinely unclear, it asks with a few options instead of
guessing. Pick one with a number key, or type your own answer.

### Rewind

**ctrl+z** (or `/rewind`) undoes the last turn: the question and answer
disappear, any change it made on the PC or in code is reverted (it lists
exactly what will and won't be undone first), and the question goes back
in the input box to edit and resend.

### Drawing

```
/draw a knight in black armor, neon city at night, rain
/draw landscape cozy cabin in the snow
```

- Anime/illustration style, written as short comma-separated tags.
- Add **`landscape`** or **`square`** for the shape (portrait by default).
- Or just ask in chat: "draw me a cat astronaut".
- About 30–60 s per image. The chat model steps out of the GPU while it draws.

**Redraw a picture:**
```
/draw ~/Pictures/photo.jpg 0.6 golden hour, warm light
```
Drag a file into the terminal to paste its path. The number (0.2–0.95,
default 0.6) is how much changes: 0.3 keeps it almost the same, 0.7+ for big
changes like day → night.

**F5 Images:** `o` open, `v` variation, `u` reuse the prompt, `i` redraw
this one, `x` twice to delete.

### Research

`/research <your question>`

A few minutes of searching and reading whole pages, then a short answer with
the sources' own words quoted under it, and links. Saved so I can reread it.

### Job hunting

The pipeline scrapes company job boards (Greenhouse, Lever, Ashby) and remote
job sites, works out which postings I can actually be hired for from where I live,
and builds a tailored CV for the best ones. **It never applies for me.**

- "Give me 5 jobs to apply to" → each with the apply link, a ready CV (PDF)
  and anything to watch out for.
- **F3 Jobs:** `o` open the posting, `p` open the CV, `f` pre-fill the
  application form in a browser window (I always click Submit myself), `a`
  mark applied, `r` rebuild the queue, `R` fetch new postings.
- CVs are built only from a fact store of things I've actually done, and every
  line is checked back against it. Nothing invented, no padded numbers.
- Roles asking for far more experience than I have are filtered out; stretch
  roles are labelled.

### Telegram

The same assistant from my phone (only my account can use it):

| | |
|---|---|
| just type | chat, with all the same tools |
| `/jobs`, `/cv <id>`, `/applied <id>`, `/done` | the job queue |
| `/draw [landscape] …` | draw; send a photo with a `/draw …` caption to redraw it |
| `/remember`, `/memory`, `/forget <id>` | memory |
| `/tasks`, `/task <text>`, `/finish <id>` | tasks |
| `/uncensored`, `/normal` | swap models, same as in the app |
| `/status`, `/refresh`, `/new`, `/help` | |

Options come as buttons. Changes to the PC can be *proposed* from the phone
but only approved at the computer (except always-allowed ones).

---

## How it's built

- **Model:** `qwen3.5:4b` through Ollama (tools and vision), a 32K context,
  and a separate small model for job-posting extraction.
- **Interface:** Textual in the terminal, plus a Telegram bot.
- **Data:** Postgres (jobs, memories, tasks, every tool call logged), Qdrant
  with bge-m3 embeddings on the CPU (memory, CV facts, skills).
- **Images:** ComfyUI with SDXL models and a 4-step speed-up.
- **Sandbox:** Docker with no network for running code.

A few rules that made a 4B model actually useful:

- **The model finds evidence; code decides.** For anything that matters
  (can I get hired here, is this CV line true, is this command safe) the
  model only extracts and code makes the call.
- **Decisions by probability.** Yes/no and multiple-choice decisions are read
  from the model's token probabilities and calibrated, instead of trusting
  free-form reasoning.
- **Measure everything.** Every change is scored against eval suites (tool
  choice, decisions, general knowledge, the full UI with the real model)
  before it ships, and every tool call is logged so failures can be traced.
- **Nothing dangerous on its own.** Anything that changes the machine, edits
  code or touches an application waits for a human keypress.

**Stack:** Python · Ollama · Postgres · Qdrant · bge-m3 · SearXNG · Textual ·
Playwright · ComfyUI · Docker · Tectonic
