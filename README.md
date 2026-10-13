# Johan

A self-hosted personal AI assistant that runs entirely on one PC — a
GTX 1660 Ti with **6 GB of VRAM** — with no paid APIs and nothing leaving the
machine.

Its first big capability is a job-hunting pipeline: scrape postings, decide
which ones are actually reachable, and draft a tailored, fact-checked CV for
each. It never submits anything; it hands over a link and a PDF.

## What it does

- **Chat assistant** (terminal UI + Telegram) on a local 4B model
  (`qwen3.5:4b` via Ollama) with ~27 tools: memory, web search (self-hosted
  SearXNG), a read-only shell, a Docker code sandbox, deep research with
  cited sources, image generation (ComfyUI, SDXL).
- **Changes to the machine, approved by a human** — the model proposes
  commands (installs, settings, services, files); nothing runs until a
  keypress, each step is checked against the real system first and records
  its own undo.
- **Job pipeline** — Stage 1 scrapes ATS and aggregator APIs, Stage 2 has the
  model extract facts while *code* renders the eligibility verdict, Stage 4
  assembles a CV only from a fact store, Stage 5 verifies every line traces
  back to a source and compiles it.

## Design rules that made a 4B model usable

- **The model locates evidence; code decides.** Interpretation never creates
  a positive signal on its own.
- **Measure before building.** Every engine change is scored against eval
  suites (tool choice, a calibrated decision layer, general Q&A) and logged
  per call in Postgres.
- **Decisions by token probability.** Multiple-choice prompts read from
  logprobs with contextual calibration instead of free-form reasoning.
- **Hard resource budget.** Everything is pinned to cores 8–15, models unload
  when idle, and a GameMode hook pauses the whole stack when a game starts.

## Stack

Python · Ollama · Postgres · Qdrant · bge-m3 (CPU) · SearXNG · Textual ·
Playwright · ComfyUI · Docker · Tectonic
