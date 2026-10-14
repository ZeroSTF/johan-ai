"""General assistant tools for Johan — memory, web/docs lookup, and the
deterministic basics a 4B model gets wrong on its own.

Design constraints that shape everything here:

  - **8192-token context on a 4B model.** One fat tool result drowns it, so
    every function below returns something small on purpose, and
    `johan_assistant.py` additionally truncates any result before it reaches
    the model. Fetching a whole documentation page and pasting it in would
    make the assistant WORSE, not better.
  - **6 GB VRAM, gaming is a hard requirement.** Nothing here loads a model.
    Memory uses the bge-m3 embed server, which runs on CPU (systemd user unit,
    `MemoryMax=4G`), so semantic recall costs zero VRAM.
  - **Deterministic beats clever.** A 4B model cannot reliably do arithmetic
    or know today's date. Those are code, not prompts.
"""

import ast
import html
import json
import operator
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

import stage4_assemble as s4

EMBED_URL = "http://127.0.0.1:8100/embed"
QDRANT = "http://127.0.0.1:6333"
MEM_COLLECTION = "johan_memory"
EMBED_DIM = 1024
PROJECT_ROOT = Path.home() / "johan"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) johan-assistant/0.1"}


def _conn():
    return psycopg2.connect(s4.dsn())


def _embed(text):
    r = requests.post(EMBED_URL, json={"texts": [text]}, timeout=60)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def _ensure_collection():
    r = requests.get(f"{QDRANT}/collections/{MEM_COLLECTION}", timeout=10)
    if r.status_code == 200:
        return
    requests.put(f"{QDRANT}/collections/{MEM_COLLECTION}", timeout=30, json={
        "vectors": {"size": EMBED_DIM, "distance": "Cosine"}}).raise_for_status()


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------

def remember(text, kind="fact"):
    """Store something durably. Postgres holds the text (source of truth),
    Qdrant holds the vector so recall is semantic rather than keyword."""
    text = (text or "").strip()
    if not text:
        return {"error": "nothing to remember"}
    # 'feedback' = a behaviour rule learned from his corrections (johan_learn)
    if kind not in ("fact", "preference", "personal", "project", "feedback"):
        kind = "fact"

    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("insert into memories (text, kind) values (%s, %s) "
                    "returning id, created_at", (text, kind))
        mem_id, created = cur.fetchone()
    conn.close()

    _ensure_collection()
    requests.put(
        f"{QDRANT}/collections/{MEM_COLLECTION}/points?wait=true", timeout=60,
        json={"points": [{"id": mem_id, "vector": _embed(text),
                           "payload": {"text": text, "kind": kind,
                                       "created_at": created.isoformat()}}]},
    ).raise_for_status()
    return {"stored": True, "memory_id": mem_id, "kind": kind, "text": text}


def recall(query, top_k=5):
    """Semantic search over stored memories."""
    r = requests.get(f"{QDRANT}/collections/{MEM_COLLECTION}", timeout=10)
    if r.status_code != 200:
        return {"memories": [], "note": "nothing has been remembered yet"}
    res = requests.post(
        f"{QDRANT}/collections/{MEM_COLLECTION}/points/search", timeout=60,
        json={"vector": _embed(query), "limit": min(int(top_k or 5), 10),
              "with_payload": True},
    ).json().get("result", [])
    return {"memories": [
        {"memory_id": h["id"], "score": round(h["score"], 3),
         "kind": h["payload"].get("kind"),
         "text": h["payload"].get("text")} for h in res]}


def list_memories(limit=15):
    """Everything remembered, newest first — for 'what do you know about me'."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("select id, text, kind, created_at from memories "
                "order by created_at desc limit %s", (min(int(limit or 15), 50),))
    rows = [{"memory_id": r["id"], "kind": r["kind"], "text": r["text"],
             "stored": r["created_at"].strftime("%Y-%m-%d")} for r in cur.fetchall()]
    conn.close()
    return {"count": len(rows), "memories": rows}


def forget(memory_id):
    """Delete a memory. Matters because a wrong stored fact is worse than no
    fact — it gets confidently repeated forever."""
    conn = _conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("delete from memories where id = %s returning text",
                    (int(memory_id),))
        row = cur.fetchone()
    conn.close()
    if not row:
        return {"error": f"no memory with id {memory_id}"}
    requests.post(f"{QDRANT}/collections/{MEM_COLLECTION}/points/delete?wait=true",
                  json={"points": [int(memory_id)]}, timeout=30)
    return {"forgotten": True, "memory_id": int(memory_id), "was": row[0]}


# --------------------------------------------------------------------------
# Web / documentation lookup
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_RE = re.compile(r"\n{3,}")


def _visible_text(markup):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, "html.parser")
    for bad in soup(["script", "style", "nav", "footer", "header", "aside",
                      "noscript", "svg", "form"]):
        bad.decompose()
    text = soup.get_text("\n")
    text = WS_RE.sub(" ", text)
    return BLANK_RE.sub("\n\n", text).strip()


# Web search goes through the SELF-HOSTED SearXNG at 127.0.0.1:8888, not a
# scraped search engine. Scraping was tried and rejected: DuckDuckGo's HTML
# endpoint answers 200 to curl but 202 + a captcha page to requests (header
# fingerprinting), its Instant Answer API also 202s, Mojeek/Ecosia 403, Brave
# 429. Beating those checks is evasion, which this project already refused to
# build for GulfTalent/TanitJobs. SearXNG is designed to be self-hosted and
# exposes a documented JSON API, so this is the legitimate route — and it's
# free with no query cap and no queries leaving the machine.
SEARX_URL = "http://127.0.0.1:8888/search"

PACKAGE_REGISTRIES = ("pypi", "npm", "crates", "maven")


def web_search(query, count=6, site=None):
    """Search the web via local SearXNG. Returns titles, URLs and snippets.

    Snippets rather than pages on purpose: for a 4B model, six short results
    beat one page that consumes the whole context window. Follow up with
    fetch_page on the URL that looks right.

    `site` restricts to one domain (e.g. 'reddit.com', 'f95zone.to') — useful
    when the answer is known to live on a particular forum."""
    q = f"site:{site} {query}" if site else query
    try:
        r = requests.get(SEARX_URL, params={"q": q, "format": "json"},
                         headers=UA, timeout=30)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        return {"error": f"searxng unreachable ({type(e).__name__}) — is the "
                          f"johan-searxng container up?"}
    except ValueError:
        return {"error": "searxng returned non-JSON; check that "
                          "search.formats includes 'json' in searxng/settings.yml"}

    raw = d.get("results", [])
    dead = [e[0] for e in d.get("unresponsive_engines", [])]

    # Prefer results that actually carry a snippet.
    #
    # Taking the first N by SearXNG's own score returned six Reddit rows with
    # `content: ""` for "Lisa the Painful keyboard controls" — title and URL
    # and nothing else — while results 3-6 from Brave and Google CSE held the
    # literal answer ("the control options ... F3, it turns out"). Reddit is
    # also the one source fetch_page cannot read (consent gate), so those rows
    # are a dead end twice over. A snippet-less result is nearly useless to
    # the model; rank it last instead of letting it crowd out the answer.
    with_text = [x for x in raw if (x.get("content") or "").strip()]
    without = [x for x in raw if not (x.get("content") or "").strip()]
    ordered = with_text + without

    n = min(int(count or 6), 10)
    results = [{"title": (x.get("title") or "")[:110],
                 "url": x.get("url"),
                 "snippet": (x.get("content") or "")[:280]}
                for x in ordered[:n]]
    out = {"query": q, "count": len(results), "results": results}
    if dead:
        out["engines_unavailable"] = dead

    if not results:
        # Zero results and "the web has nothing" must not look the same.
        # They did, and the model concluded a well-known 2014 game "might be
        # a very niche or new app I don't have data on yet" when really every
        # upstream engine had refused that minute. Say which it is.
        out["error"] = (
            "SEARCH RETURNED NOTHING — this usually means the search backend "
            "is degraded, not that the subject doesn't exist. "
            + (f"These engines failed: {', '.join(dead)}. " if dead else "")
            + "Tell Riadh the search failed and offer to retry; do NOT claim "
              "the thing he asked about is obscure or nonexistent.")
    return out


def package_latest(name, ecosystem="pypi"):
    """Current released version of a library, plus its real docs URL.

    This is what "check the latest documentation" usually means in practice:
    the model's training data has a stale version number, and the registry is
    authoritative and free."""
    eco = (ecosystem or "pypi").lower()
    if eco not in PACKAGE_REGISTRIES:
        return {"error": f"ecosystem must be one of {PACKAGE_REGISTRIES}"}
    try:
        if eco == "pypi":
            d = requests.get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json",
                             headers=UA, timeout=25).json()
            info = d["info"]
            urls = {k: v for k, v in (info.get("project_urls") or {}).items() if v}
            return {"ecosystem": "pypi", "name": info["name"],
                    "latest_version": info["version"],
                    "summary": (info.get("summary") or "")[:200],
                    "requires_python": info.get("requires_python"),
                    "docs": urls.get("Documentation") or info.get("home_page")
                            or f"https://pypi.org/project/{info['name']}/",
                    "links": dict(list(urls.items())[:4])}
        if eco == "npm":
            d = requests.get(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}/latest",
                             headers=UA, timeout=25).json()
            return {"ecosystem": "npm", "name": d.get("name"),
                    "latest_version": d.get("version"),
                    "summary": (d.get("description") or "")[:200],
                    "docs": d.get("homepage") or f"https://www.npmjs.com/package/{name}"}
        if eco == "crates":
            d = requests.get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}",
                             headers=UA, timeout=25).json()
            c = d["crate"]
            return {"ecosystem": "crates.io", "name": c["name"],
                    "latest_version": c["max_version"],
                    "summary": (c.get("description") or "")[:200],
                    "docs": c.get("documentation") or f"https://docs.rs/{c['name']}"}
        d = requests.get("https://search.maven.org/solrsearch/select", headers=UA,
                         timeout=25,
                         params={"q": f'a:"{name}"', "rows": 3, "wt": "json"}).json()
        docs = d["response"]["docs"]
        if not docs:
            return {"error": f"no maven artifact named {name}"}
        return {"ecosystem": "maven", "results": [
            {"group": x.get("g"), "artifact": x.get("a"),
             "latest_version": x.get("latestVersion")} for x in docs]}
    # RequestException, not HTTPError: these registries time out and reset
    # connections in practice (search.maven.org especially), and a bare
    # HTTPError catch let those propagate as tracebacks.
    except requests.RequestException as e:
        return {"error": f"{eco} unreachable: {type(e).__name__}"}
    except (KeyError, ValueError):
        return {"error": f"'{name}' not found on {eco}"}


def github_latest_release(repo):
    """Latest release of a GitHub repo: tag, date, and notes excerpt. Covers
    'what changed in the newest version of X'."""
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo or ""):
        return {"error": "repo must look like 'owner/name'"}
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                         headers=UA, timeout=25)
        if r.status_code == 404:
            # No releases published — fall back to the newest tag.
            t = requests.get(f"https://api.github.com/repos/{repo}/tags",
                             headers=UA, timeout=25)
            if t.ok and t.json():
                return {"repo": repo, "latest_tag": t.json()[0]["name"],
                        "note": "repo publishes tags, not releases"}
            return {"error": f"no releases or tags for {repo}"}
        if not r.ok:
            return {"error": f"github returned {r.status_code} for {repo}"}
        d = r.json()
    except requests.RequestException as e:
        return {"error": f"github unreachable: {type(e).__name__}"}
    return {"repo": repo, "tag": d.get("tag_name"), "name": d.get("name"),
            "published": (d.get("published_at") or "")[:10],
            "url": d.get("html_url"),
            "notes": (d.get("body") or "")[:900]}


def wikipedia(query):
    """One-paragraph summary for general knowledge questions."""
    try:
        s = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=25,
                         params={"action": "opensearch", "search": query,
                                 "limit": 1, "namespace": 0, "format": "json"})
        titles = s.json()[1] if s.ok else []
        if not titles:
            return {"error": f"nothing on wikipedia for '{query}'"}
        title = titles[0]
        d = requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                         + urllib.parse.quote(title.replace(" ", "_")),
                         headers=UA, timeout=25).json()
    except requests.RequestException as e:
        return {"error": f"wikipedia unreachable: {type(e).__name__}"}
    return {"title": d.get("title"), "summary": (d.get("extract") or "")[:900],
            "url": (d.get("content_urls") or {}).get("desktop", {}).get("page")}


def _reddit_text(markup, limit_comments=8):
    """Pull just the post and top comments out of an old.reddit page.

    Generic extraction on a 320 KB Reddit page spends the whole truncation
    budget on sidebar and nav before reaching an answer."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, "html.parser")
    parts = []
    title = soup.select_one("a.title") or soup.select_one("p.title")
    if title:
        parts.append("POST: " + title.get_text(" ", strip=True))
    body = soup.select_one("div.expando div.usertext-body")
    if body:
        parts.append(body.get_text(" ", strip=True))
    for i, c in enumerate(soup.select("div.commentarea div.entry div.usertext-body")):
        if i >= limit_comments:
            break
        txt = c.get_text(" ", strip=True)
        if txt:
            parts.append(f"COMMENT {i + 1}: {txt}")
    return "\n\n".join(parts) if parts else ""


def fetch_page(url, max_chars=1800):
    """Read one page as plain text, hard-truncated. Use with a URL from
    web_search to read documentation, a forum thread, etc."""
    if not re.match(r"^https?://", url or ""):
        return {"error": "url must start with http:// or https://"}

    # www.reddit.com is JS-rendered: BeautifulSoup gets literally the 6 chars
    # "Reddit" from it, and the .json API now 403s for non-OAuth clients.
    # old.reddit.com serves the same thread server-rendered and returns 200.
    reddit = False
    if re.match(r"^https?://(www\.)?reddit\.com/", url):
        url = re.sub(r"^https?://(www\.)?reddit\.com/", "https://old.reddit.com/", url)
        reddit = True
    elif "old.reddit.com" in url:
        reddit = True

    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        return {"error": f"could not fetch {url}: {type(e).__name__} "
                          f"(many forums sit behind Cloudflare and refuse bots)"}
    ctype = r.headers.get("content-type", "")

    if reddit:
        # Reddit serves a ~320 KB "Welcome to Reddit" consent/bot gate to
        # non-browser clients on old.reddit too, and the .json API 403s
        # without OAuth. Detect it and say so, rather than handing the model
        # 39 characters of chrome that it will treat as the thread contents.
        # Getting past the gate would be evasion; the legitimate route is a
        # free Reddit API app (OAuth client id/secret).
        if "<title>Welcome to Reddit</title>" in r.text[:4000]:
            return {"error": "Reddit served its bot/consent gate instead of the "
                              "thread. Reddit content is searchable via "
                              "web_search snippets but not readable without "
                              "Reddit API credentials (OAuth). Use the snippet, "
                              "or ask Riadh to add a Reddit API key.",
                     "url": url}
        text = _reddit_text(r.text) or _visible_text(r.text)
        return _page_out(url, text, max_chars)
    if "html" in ctype:
        text = _visible_text(r.text)
        # Client-rendered pages (Ashby job boards: "Supabase Jobs", 13 chars)
        # come back as a shell. Only then, render in headless Chromium —
        # never by default: PCGamingWiki reads fine plainly but serves the
        # headless browser a Cloudflare challenge.
        try:
            import johan_browser as jb
            if jb.available() and jb.looks_like_js_shell(text):
                rendered = jb.render(url)
                if rendered.get("gated"):
                    return {"error": "the page needs JavaScript and serves a bot "
                                     "check to the headless browser. Johan does "
                                     "not get around bot checks — use the search "
                                     "snippet.", "url": url}
                if rendered.get("text") and len(rendered["text"]) > len(text):
                    return _page_out(url, rendered["text"], max_chars, rendered=True)
        except Exception:
            pass                      # the plain text is still an answer
    elif "json" in ctype:
        text = json.dumps(r.json())[:max_chars * 2]
    else:
        text = r.text
    return _page_out(url, text, max_chars)


def _page_out(url, text, max_chars, **extra):
    """A long page is read by relevance to his question (johan_reader), not
    cut at the first N chars — answers deep in a page used to be missed."""
    import johan_reader as jr
    if jr.question() and len(text) <= jr.READER_CHARS:
        return {"url": url, **extra, "chars": len(text), "truncated": False, "text": text}
    sel = jr.read(text)
    if sel:
        return {"url": url, **extra, "chars": len(text), "note": sel["note"],
                "text": sel["text"]}
    cap = min(int(max_chars or 1800), 4000)
    return {"url": url, **extra, "chars": len(text), "truncated": len(text) > cap,
            "text": text[:cap]}


# --------------------------------------------------------------------------
# Things a 4B model reliably gets wrong
# --------------------------------------------------------------------------

def current_datetime():
    now = datetime.now().astimezone()
    return {"local": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "weekday": now.strftime("%A"), "iso": now.isoformat(timespec="seconds")}


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError("only numbers")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


def calculate(expression):
    """Arithmetic via AST, not eval() — no name lookups, no calls, no
    attribute access, so a crafted 'expression' can't execute anything."""
    try:
        value = _eval(ast.parse(str(expression), mode="eval").body)
    except ZeroDivisionError:
        return {"error": "division by zero"}
    except Exception as e:
        return {"error": f"cannot evaluate: {e}"}
    return {"expression": str(expression), "result": value}


# --------------------------------------------------------------------------
# The machine itself
# --------------------------------------------------------------------------

def system_status():
    """Machine specs AND service health.

    Asked "what are my system specs" this returned GPU, disk and services and
    nothing else — no OS, no CPU, no RAM — so the model had to follow up with
    three `run_command` calls and got the first one wrong (it tried `&&`).
    A tool named system_status should answer that question by itself."""
    out = {}
    try:
        rel = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                rel[k] = v.strip().strip('"')
        out["os"] = rel.get("PRETTY_NAME") or rel.get("NAME")
    except OSError:
        pass
    try:
        uname = os.uname()
        out["kernel"] = f"{uname.sysname} {uname.release}"
        out["arch"] = uname.machine
        out["hostname"] = uname.nodename
    except Exception:
        pass
    try:
        model, cores, threads = None, None, None
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name") and not model:
                model = line.split(":", 1)[1].strip()
            if line.startswith("cpu cores") and not cores:
                cores = line.split(":", 1)[1].strip()
        threads = os.cpu_count()
        out["cpu"] = {"model": model, "physical_cores": cores,
                       "logical_cpus": threads}
    except Exception:
        pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines()[:3]:
            k, v = line.split(":", 1)
            mem[k] = v.strip()
        total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
        avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
        out["ram"] = {"total_gb": round(total_kb / 1048576, 1),
                       "available_gb": round(avail_kb / 1048576, 1)}
    except Exception:
        pass
    try:
        q = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        used, total, util = [x.strip() for x in q.stdout.strip().split(",")]
        out["gpu"] = {"vram_used_mb": int(used), "vram_total_mb": int(total),
                       "vram_free_mb": int(total) - int(used),
                       "gpu_util_pct": int(util)}
    except Exception as e:
        out["gpu"] = {"error": str(e)}

    try:
        models = requests.get("http://127.0.0.1:11434/api/ps", timeout=5).json()
        out["models_loaded"] = [m.get("name") for m in models.get("models", [])]
    except Exception:
        out["models_loaded"] = "ollama not reachable"

    for name, url in (("embed_server", "http://127.0.0.1:8100/health"),
                      ("qdrant", f"{QDRANT}/collections")):
        try:
            out[name] = "up" if requests.get(url, timeout=5).ok else "error"
        except Exception:
            out[name] = "down"

    try:
        conn = _conn(); conn.close(); out["postgres"] = "up"
    except Exception:
        out["postgres"] = "down"

    total, used, free = shutil.disk_usage(str(Path.home()))
    out["disk"] = {"free_gb": round(free / 1e9, 1), "total_gb": round(total / 1e9, 1)}
    return out


# --------------------------------------------------------------------------
# Files, scoped to the johan project
# --------------------------------------------------------------------------

def _safe_path(rel):
    """Resolve inside PROJECT_ROOT only. Without this, a path like
    '../../.ssh/id_rsa' or an absolute path would read anything the user can,
    and tool output goes into a model's context."""
    p = (PROJECT_ROOT / str(rel).lstrip("/")).resolve()
    if not str(p).startswith(str(PROJECT_ROOT.resolve())):
        raise ValueError("path escapes the johan project directory")
    return p


def read_project_file(path, max_chars=1500):
    try:
        p = _safe_path(path)
    except ValueError as e:
        return {"error": str(e)}
    if not p.is_file():
        return {"error": f"no such file: {path}"}
    text = p.read_text(errors="replace")
    cap = min(int(max_chars or 1500), 4000)
    return {"path": str(p.relative_to(PROJECT_ROOT)), "chars": len(text),
            "truncated": len(text) > cap, "text": text[:cap]}


def search_project_files(query, max_results=8):
    """grep the johan project. Returns file:line + the matching line only."""
    if not query:
        return {"error": "empty query"}
    cmd = ["grep", "-rIn", "--exclude-dir=venv-embed", "--exclude-dir=.git",
           "--exclude-dir=__pycache__", "--exclude-dir=edit-backups",
           "--exclude-dir=sessions", "-e", str(query), str(PROJECT_ROOT)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    hits = []
    for line in r.stdout.splitlines()[:min(int(max_results or 8), 20)]:
        parts = line.split(":", 2)
        if len(parts) == 3:
            try:
                rel = str(Path(parts[0]).relative_to(PROJECT_ROOT))
            except ValueError:
                rel = parts[0]
            hits.append({"file": rel, "line": int(parts[1]),
                          "text": parts[2][:200]})   # indentation preserved
    return {"query": query, "count": len(hits), "matches": hits}


# --------------------------------------------------------------------------
# Pipeline capabilities that were built but unreachable from chat
# --------------------------------------------------------------------------

def verify_staged_cvs():
    """Re-run Stage 5 over every staged application.

    Worth exposing because the fact store can change after a CV is generated:
    edit or remove a bullet and previously-verified CVs silently stop matching
    their source. This is how you find that out before sending one."""
    import stage5_verify as s5
    apps = PROJECT_ROOT / "job-hunter/staged/applications"
    if not apps.exists():
        return {"checked": 0, "note": "no staged applications yet"}
    facts, by_id = s5.load_facts()
    passed, failed = [], []
    for d in sorted(p for p in apps.iterdir() if p.is_dir()):
        problems = s5.verify_one(d, facts, by_id)
        (failed if problems else passed).append(
            {"application": d.name, "problems": problems[:3]} if problems
            else d.name)
    return {"checked": len(passed) + len(failed), "passed": len(passed),
            "failed": len(failed), "failures": failed,
            "note": "failed CVs must not be sent" if failed else "all staged CVs verify"}


def search_conversations(query, limit=6):
    """Search past conversations with Riadh.

    Distinct from `recall`: memories are facts he asked to be kept, this is
    what was actually said in earlier sessions — "what did we decide about
    X". Greps the saved session files; no embedding needed, and it stays
    accurate as sessions accumulate."""
    import johan_assistant as ja
    if not query:
        return {"error": "empty query"}
    if not ja.SESSIONS_DIR.exists():
        return {"matches": [], "note": "no saved conversations yet"}
    needle = str(query).lower()
    hits = []
    for path in sorted(ja.SESSIONS_DIR.glob("*.json"), reverse=True):
        try:
            msgs = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        for m in msgs:
            if m.get("role") not in ("user", "assistant"):
                continue
            text = m.get("content") or ""
            if needle in text.lower():
                hits.append({"session": path.stem, "who": m["role"],
                              "text": text[:220]})
                break        # one hit per session keeps the result small
        if len(hits) >= min(int(limit or 6), 10):
            break
    return {"query": query, "count": len(hits), "matches": hits,
            "note": None if hits else "nothing found in past conversations"}


def check_company_hiring(company, ats=None):
    """Can this company actually hire someone in Tunisia? Reads their job
    board and reports how many postings are worldwide / EMEA / bare-remote
    versus tied to a named country.

    This is the project's central question as a tool: what decides whether
    Riadh is hirable is *how* a company hires — home-based worldwide as
    policy, or only where it holds a legal entity. `company` is the board
    token (usually the lowercase name, e.g. 'supabase', 'canonical')."""
    import probe_worldwide as pw
    token = re.sub(r"[^a-z0-9-]", "", (company or "").lower())
    if not token:
        return {"error": "give a board token, e.g. 'supabase'"}
    probes = [(ats, dict(pw.PROBES)[ats])] if ats in dict(pw.PROBES) else pw.PROBES
    for source, fn in probes:
        try:
            rows = fn(token)
        except Exception:
            continue
        if not rows:
            continue
        s = pw.score(rows)
        return {"company": token, "ats": source, "total_postings": s["total"],
                "engineering_roles": s["eng"],
                "worldwide": s["worldwide"], "emea": s["emea"],
                "bare_remote": s["bare_remote"], "named_country": s["named_place"],
                "reachable_pct": s["reach_pct"],
                "example_locations": s["examples"],
                "verdict": ("looks hirable from Tunisia"
                             if s["reachable"] else
                             "entity-bound — every posting names a country")}
    return {"error": f"no public Greenhouse/Lever/Ashby board found for "
                      f"'{token}'. Many companies use another ATS; try the "
                      f"exact token from their careers URL."}


# --------------------------------------------------------------------------
# Core-memory scratchpad
# --------------------------------------------------------------------------
#
# A few hundred characters that are ALWAYS in the system prompt, that the
# model can rewrite mid-conversation.
#
# Why this and not `remember`: memories are durable facts about Riadh and
# require the model to *decide* to call `memory` to see them — which a 4B
# often doesn't. The scratchpad is different in kind: working state for the
# task at hand ("debugging the searxng timeout; he prefers I check the
# container logs first"), injected unconditionally so forgetting to look it
# up is impossible. Bounded on purpose — it costs context on every single
# turn, so it must stay small enough that it never competes with the
# conversation.

SCRATCHPAD = PROJECT_ROOT / "brain/scratchpad.txt"
SCRATCHPAD_MAX = 600


def scratchpad_read():
    try:
        return SCRATCHPAD.read_text().strip()
    except OSError:
        return ""


def scratchpad_write(text, append=False):
    """Set or extend the working-notes scratchpad."""
    text = (text or "").strip()
    if not text:
        return {"error": "nothing to write — use scratchpad(clear=true) to empty it"}
    current = scratchpad_read()
    new = f"{current}\n{text}".strip() if (append and current) else text
    truncated = len(new) > SCRATCHPAD_MAX
    if truncated:
        # Keep the END: the newest note is the one still relevant.
        new = new[-SCRATCHPAD_MAX:]
    SCRATCHPAD.parent.mkdir(parents=True, exist_ok=True)
    SCRATCHPAD.write_text(new)
    return {"saved": True, "chars": len(new), "limit": SCRATCHPAD_MAX,
            "truncated_oldest": truncated, "scratchpad": new}


def scratchpad_clear():
    SCRATCHPAD.parent.mkdir(parents=True, exist_ok=True)
    SCRATCHPAD.write_text("")
    return {"cleared": True}


# --------------------------------------------------------------------------
# Running commands — read-only, allowlisted, paged
# --------------------------------------------------------------------------
#
# THREAT MODEL, stated plainly: this defends against the model doing
# something stupid or being talked into it by a web page it just read. It is
# not a sandbox and would not stop a determined attacker who could choose
# arbitrary argv. That is the right bar here — the realistic risk is a 4B
# model confabulating `rm -rf` or `cat ~/.ssh/id_rsa` into a summary, not a
# targeted exploit.
#
# Four layers:
#   1. ALLOWLIST of binaries. Denylists lose: `rm` vs `/bin/rm` vs
#      `find -delete` vs `python -c`. Only named-safe programs run.
#   2. NO SHELL. argv list, shell=False — `;` `|` `&&` `>` backticks and glob
#      expansion are inert characters, not syntax.
#   3. Subcommand gating for the multi-tools (systemctl/docker/git/journalctl
#      /ollama): `systemctl status` yes, `systemctl stop` no.
#   4. Sensitive paths refused, because tool output lands in a model's context
#      and could be repeated back into a chat log or a web request.

# Allowlist derived from GTFOBins (github.com/GTFOBins/GTFOBins.github.io,
# 478 binaries), not from memory. Rule: exclude anything GTFOBins records as
# capable of `shell`, `command`, `reverse-shell`, `bind-shell` or
# `library-load` — those break the no-shell guarantee outright — and anything
# with `file-write`/`upload`/`download` unless the offending flag is banned
# below. `file-read` alone is fine: reading is the point, and SENSITIVE still
# applies.
#
# THIS AUDIT FOUND TWO REAL HOLES IN THE PREVIOUS HAND-WRITTEN LIST:
#   awk  -> /usr/bin/mawk on this box; `awk 'BEGIN{system("...")}'` ran and
#           printed the user id. GTFOBins lists `awk` as empty, but gawk and
#           mawk both carry `shell` — judge by what the symlink resolves to.
#   sed  -> `sed 's/x/cmd/e'` executes; only `-i` had been banned.
# Both are removed. Verified by actually running the escape, not by reading
# about it.
SAFE_COMMANDS = {
    # --- reading and filtering text (GTFOBins: file-read at most) ---------
    "cat", "head", "tail", "nl", "tac", "strings", "od", "base64", "cut",
    "paste", "join", "comm", "fold", "expand", "unexpand", "rev", "cmp",
    "diff", "wc", "grep", "rg", "zgrep", "jq", "yq", "column", "tr", "uniq",
    # --- filesystem inspection -------------------------------------------
    "ls", "stat", "file", "tree", "realpath", "readlink", "basename",
    "dirname", "du", "df", "lsblk", "blkid", "findmnt", "dust", "fdfind",
    # --- processes --------------------------------------------------------
    "ps", "pgrep", "pidof", "pstree", "uptime", "free", "vmstat", "iostat",
    "mpstat", "sar", "lsof", "htop",
    # --- hardware ---------------------------------------------------------
    "lscpu", "lsusb", "lspci", "lsmod", "nproc", "arch", "sensors",
    "nvidia-smi", "getconf", "numactl", "hwinfo", "inxi", "upower", "acpi",
    # --- identity and time -------------------------------------------------
    "uname", "hostname", "whoami", "id", "groups", "date", "locale",
    "timedatectl", "hostnamectl", "who", "w", "last", "logname", "tty",
    # --- display (this is what "what's my refresh rate" needs) ------------
    "xrandr", "wlr-randr", "xdpyinfo", "glxinfo", "xset", "xinput",
    "wayland-info",
    # --- network, read-only ------------------------------------------------
    "ss", "ping", "dig", "host", "nslookup", "traceroute", "mtr", "arp",
    "route", "ethtool", "netstat", "iwconfig",
    # --- small utilities ---------------------------------------------------
    "echo", "printf", "seq", "md5sum", "sha256sum", "cksum", "numfmt", "bc",
    "units", "cal", "apt-cache",
    # --- abusable but too useful to lose: gated by flag or subcommand -----
    "pwd", "which", "whereis", "flatpak", "snap",
    "find", "sort", "xxd", "dmesg", "systemctl", "journalctl", "docker",
    "git", "ollama", "pip", "python3", "ip", "nmcli", "gsettings", "dpkg",
    "wpctl", "pactl",       # audio state (skills/no-sound.md); read-only subcommands
}

# Flags that turn a read-only tool into a writing or executing one. Each entry
# is the exact mechanism GTFOBins documents for that binary.
BANNED_ARGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprintf",
              "-fls", "-fprint", "-fprint0"},
    "sort": {"-o", "--output"},          # sort -o overwrites a file
    "xxd": {"-r", "--revert"},           # reverse mode writes binary
    "jq": {"-f", "--from-file"},
    "ping": set(), "grep": set(),
}

SUBCOMMANDS = {
    "python3": {"--version", "-V"},       # never -c / -m: arbitrary code
    "pip": {"list", "show", "freeze", "--version"},
    "dpkg": {"-l", "--list", "-s", "--status", "-L", "--listfiles", "-S"},
    "nmcli": {"device", "connection", "general", "radio", "networking"},
    "ip": {"addr", "a", "link", "l", "route", "r", "neigh", "n"},
    "gsettings": {"get", "list-keys", "list-schemas", "list-recursively",
                   "range", "describe"},
    "systemctl": {"status", "is-active", "is-enabled", "list-units",
                   "list-timers", "list-unit-files", "show", "cat"},
    "journalctl": None,                   # read-only by nature
    "docker": {"ps", "logs", "images", "stats", "inspect", "version", "df",
                "top", "port", "info"},
    "git": {"status", "log", "diff", "show", "branch", "remote", "describe",
             "shortlog", "blame", "ls-files", "rev-parse", "count-objects"},
    "ollama": {"list", "ps", "show"},
    # Read-only subcommands only: `flatpak uninstall` / `snap remove` are
    # exactly the kind of thing this tool must never reach.
    "flatpak": {"list", "info", "remotes", "history", "--version"},
    "snap": {"list", "info", "find", "changes", "version", "--version"},
    "wpctl": {"status", "inspect", "get-volume"},
    "pactl": {"info", "list", "get-default-sink", "get-default-source",
              "get-sink-volume", "get-sink-mute", "get-source-volume", "get-source-mute"},
}

SENSITIVE = re.compile(
    r"(\.ssh|\.gnupg|\.aws|\.azure|\.kube|gcloud|id_rsa|id_ed25519|"
    r"\.pem$|\.key$|credentials|shadow|\.env|secrets?|\.netrc|"
    r"\.docker/config|password)", re.I)

TERMINAL_DIR = PROJECT_ROOT / "brain/terminal"
PAGE_LINES = 40
# A page must also fit under johan_assistant.MAX_TOOL_CHARS (2400) or the
# backstop truncates it mid-page and the model reads half a line as fact.
# 40 lines of 100 chars is 4000, so lines alone were not a sufficient bound.
PAGE_CHARS = 1700


def _page_of(lines, page):
    """Slice a page bounded by BOTH line count and characters."""
    pages, start, idx = [], 0, 0
    while idx < len(lines):
        chunk, size = [], 0
        while idx < len(lines) and len(chunk) < PAGE_LINES and size < PAGE_CHARS:
            chunk.append(lines[idx][:PAGE_CHARS])
            size += len(chunk[-1]) + 1
            idx += 1
        pages.append(chunk)
    if not pages:
        pages = [[]]
    page = max(1, min(int(page or 1), len(pages)))
    return "\n".join(pages[page - 1]), page, len(pages)


# What to reach for when a command is blocked. awk and sed are absent from
# SAFE_COMMANDS deliberately — both were proven live shell escapes here
# (`awk 'BEGIN{system(...)}'`, `sed 's/x/cmd/e'`) — so the model needs a
# route, not just a refusal.
SUBSTITUTES = {
    "awk": "Use cut -d' ' -fN, or grep -o, or just read the raw output pages",
    "gawk": "Use cut -d' ' -fN, or grep -o",
    "mawk": "Use cut -d' ' -fN, or grep -o",
    "sed": "Use grep, cut or tr",
    "xargs": "No xargs. To delete/move the files, call propose_action with `gio trash PATH …` listing the exact paths",
    "sh": "Run the program directly; there is no shell here (no while/for loops either)",
    "bash": "Run the program directly; there is no shell here",
    "zsh": "Run the program directly; there is no shell here",
    "rm": "run_command is read-only. To trash files, propose_action with `gio trash PATH` (he approves it)",
    "while": "No shell loops. To act on many files, propose_action with `gio trash PATH1 PATH2 …`",
    "for": "No shell loops. To act on many files, propose_action with `gio trash PATH1 PATH2 …`",
    "mv": "run_command is read-only. To move files, use propose_action (he approves it)",
    "cp": "run_command is read-only. To copy files, use propose_action (he approves it)",
    "apt": "run_command is read-only. To install/remove, use propose_action; to look, apt-cache or `dpkg -l`",
    "apt-get": "run_command is read-only. To install/remove, use propose_action; to look, apt-cache or `dpkg -l`",
    "less": "Use cat, then run_command(page=N) for the rest",
    "more": "Use cat, then run_command(page=N) for the rest",
    "nano": "Use cat — this tool cannot edit; use propose_edit for that",
    "vim": "Use cat — this tool cannot edit; use propose_edit for that",
    "curl": "Use the fetch_page tool, or web_search",
    "wget": "Use the fetch_page tool, or web_search",
}


def _check_command(argv):
    """Return an error string, or None if the command is allowed."""
    if not argv:
        return "empty command"
    prog = Path(argv[0]).name
    if prog not in SAFE_COMMANDS:
        # Listing all 127 allowed names is ~900 chars of noise that never
        # says what to do INSTEAD, so the model just retried the same
        # blocked command verbatim three times. Name a substitute.
        hint = SUBSTITUTES.get(prog)
        return (f"'{prog}' is not available. "
                + (f"{hint}. " if hint else
                   "This tool is read-only — to install, change a setting, "
                   "restart or move anything use propose_action. Text tools: "
                   "grep, cut, tr, sort, uniq, head, tail, wc, jq, column. ")
                + "Do NOT retry this same command; change the approach.")
    if prog == "ping" and not any(a in ("-c", "-w") for a in argv):
        return "ping must be bounded: use `ping -c 3 host`"
    for bad in BANNED_ARGS.get(prog, ()):
        if bad in argv:
            return (f"'{bad}' is not allowed with {prog} — run_command only reads. "
                    + ("To delete files: list them with find here, then call "
                       "propose_action with `gio trash PATH1 PATH2 …` (exact paths, "
                       "he approves it). " if prog == "find" else "")
                    + "Do NOT retry with another flag.")
    subs = SUBCOMMANDS.get(prog, "no-gate")
    if subs not in ("no-gate", None):
        # Check BOTH the first argument and the first non-flag one: some
        # subcommands are flags (`python3 --version`), and skipping anything
        # starting with "-" rejected those outright.
        first = argv[1] if len(argv) > 1 else None
        non_flag = next((a for a in argv[1:] if not a.startswith("-")), None)
        if first not in subs and non_flag not in subs:
            return (f"only these {prog} subcommands are allowed: "
                    f"{', '.join(sorted(subs))} (got {first or non_flag!r}). "
                    f"run_command only reads — to CHANGE something, call "
                    f"propose_action with this command instead")
    for arg in argv[1:]:
        if SENSITIVE.search(arg):
            return (f"refusing to touch {arg!r}: it looks like credentials or "
                    f"keys, and command output goes into the model's context")
    return None


_STDERR_SINK = re.compile(r"^2>\s*(/dev/null|&1|&-)$")


def _strip_stderr_redirect(argv):
    """Drop `2>/dev/null`, `2>&1`, `2> /dev/null` tokens from an argv.

    Safe because there is no shell here: these tokens would otherwise reach
    the program as literal arguments (`find` would search for a file called
    "2>/dev/null"), so they are never meaningful input. Only the stderr
    forms — a bare `>` or `1>` still gets refused below, since that writes
    a file."""
    out, skip = [], False
    for i, tok in enumerate(argv):
        if skip:
            skip = False
            continue
        if _STDERR_SINK.match(tok):
            continue
        if tok == "2>" and i + 1 < len(argv) and argv[i + 1] in ("/dev/null", "&1"):
            skip = True
            continue
        out.append(tok)
    return out


def _child_env():
    """git/systemctl/journalctl hand output to a pager, and `less` is a
    documented shell escape (`!sh`). Force a dumb pager and strip the loader
    variables that turn any binary into arbitrary code."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
                        "PYTHONSTARTUP", "BASH_ENV")}
    env.update(PAGER="cat", GIT_PAGER="cat", SYSTEMD_PAGER="cat",
               GIT_TERMINAL_PROMPT="0", LESSSECURE="1")
    return env


def _exec_pipeline(stages, timeout, env):
    """Run one validated pipeline. Returns (output, exit_code).

    Raises nothing the caller doesn't expect: FileNotFoundError and
    TimeoutExpired propagate so the caller can phrase them."""
    procs, prev = [], None
    try:
        for i, stage in enumerate(stages):
            procs.append(subprocess.Popen(
                stage, env=env, stdin=(prev.stdout if prev else None),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if i == len(stages) - 1 else subprocess.DEVNULL,
                text=True, cwd=str(PROJECT_ROOT)))
            if prev:
                prev.stdout.close()      # let an upstream stage see SIGPIPE
            prev = procs[-1]
        stdout, stderr = procs[-1].communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for pr in procs:
            pr.kill()
        raise
    out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
    return out, procs[-1].returncode


def _split_segments(argv):
    """Split an argv on `&&`, `||` and `;` into [(operator, tokens), ...].

    The first segment's operator is None. Refusing these was inconsistent:
    pipes are allowed precisely BECAUSE every stage is validated against the
    same allowlist, and that argument applies identically here — a sequence
    of allowlisted read-only commands is no more dangerous than making the
    same calls one at a time. Refusing them cost real hops: asked what was
    installed, the model wrote `which flatpak && flatpak list || echo none`
    three different ways and never got an answer."""
    segments, op = [], None
    current = []
    for tok in argv:
        if tok in ("&&", "||", ";"):
            segments.append((op, current))
            op, current = tok, []
        else:
            current.append(tok)
    segments.append((op, current))
    return segments


def _blocked(problem):
    """A command the read-only checker won't run by itself. run_command_tool
    can still ask him to approve it (see johan_actions._raw_step)."""
    return {"error": problem, "blocked": True}


# Characters a shell only treats specially when UNQUOTED. Inside quotes (or
# after a backslash) they are swapped for private-use stand-ins before
# splitting and put back after globbing/operator splitting: `find -name "*.py"`
# had its pattern expanded against the working folder (scripts/ is full of
# .py), find got a list of filenames, failed, and the model looped six times.
_LITERAL = {c: chr(0xE000 + i) for i, c in enumerate("*?[|;&")}
_UNLITERAL = {v: k for k, v in _LITERAL.items()}


def _protect_quoted(command):
    out, quote, i = [], None, 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            nxt = command[i + 1]
            out.append(_LITERAL[nxt] if nxt in _LITERAL else ch + nxt)
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
            out.append(_LITERAL.get(ch, ch))
        else:
            if ch in "'\"":
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


def _restore_literal(arg):
    return "".join(_UNLITERAL.get(c, c) for c in arg)


def run_command(command, timeout=20):
    """Run a read-only shell command. Output is paged, not truncated.

    Large output is the reason this pages rather than truncates: `docker logs`
    or a big grep would otherwise either blow the context window or lose the
    part you needed. Page 1 comes back immediately; `read_output(page=N)`
    fetches the rest."""
    import shlex
    if isinstance(command, str):
        try:
            argv = shlex.split(_protect_quoted(command))
        except ValueError as e:
            return _blocked(f"could not parse command: {e}")
    else:
        argv = list(command)

    # `2>/dev/null` is stripped, not refused. It only discards the child's
    # stderr — it cannot change what runs, and stderr is captured separately
    # here anyway. Refusing it was the single most expensive rule in this
    # validator: asked to find a TF2 config, the model wrote it reflexively
    # and lost EIGHT of nine hops to the same error, never reaching an
    # answer. A rule a model cannot learn mid-conversation is a rule that
    # just burns the conversation.
    argv = _strip_stderr_redirect(argv)

    # There is no shell, so nothing expanded `~`: `find ~/johan` searched a
    # directory literally named "~" and the model, told "No such file",
    # concluded his project didn't exist. Expand it here; SENSITIVE still
    # judges the expanded path (`~/.ssh` -> /home/zeros/.ssh is refused).
    argv = [os.path.expanduser(a) if a.startswith("~") else a for a in argv]

    # Redirection and substitution stay refused: `>` writes a file and
    # backticks/$() splice in text that was never validated. Those genuinely
    # change what runs. `&&`, `||`, `;` and `|` do not — see _split_segments.
    for meta in (">", ">>", "<", "`", "$("):
        if any(meta in a for a in argv):
            return _blocked(f"{meta!r} is not allowed — it writes files or "
                            f"splices in unchecked text. Pipes (|) and "
                            f"chaining (&&, ||, ;) ARE supported.")

    segments = _split_segments(argv)
    if len(segments) > 5:
        return _blocked("at most 5 chained commands")

    # Parse and validate EVERYTHING before running ANYTHING — a chain must
    # not half-execute and then report a rejection from a later link.
    plan = []
    for op, seg_tokens in segments:
        if not seg_tokens:
            return {"error": "empty command around a &&, || or ; operator"}
        stages = [[]]
        for tok in seg_tokens:
            if tok == "|":
                stages.append([])
            else:
                stages[-1].append(tok)
        if any(not s for s in stages):
            return {"error": "empty stage in the pipeline"}
        if len(stages) > 4:
            return _blocked("at most 4 piped stages")

        # Expand globs ourselves (no shell to do it). `cat /sys/class/drm/*/modes`
        # silently failed before, because "*" reached cat as a literal character.
        import glob as _glob
        expanded = []
        for stage in stages:
            out_stage = []
            for arg in stage:
                if any(ch in arg for ch in "*?[") and not arg.startswith("-"):
                    hits = sorted(_glob.glob(arg))[:50]
                    out_stage.extend(hits or [arg])
                else:
                    out_stage.append(arg)
            expanded.append([_restore_literal(a) for a in out_stage])
        stages = expanded

        # Validate EVERY stage — `cat file | rm -rf ~` must die on stage two.
        for stage in stages:
            problem = _check_command(stage)
            if problem:
                return _blocked(problem)
        plan.append((op, stages))

    env = _child_env()
    budget = min(int(timeout or 20), 60)
    chunks, rc, ran, missing = [], 0, [], []
    started = time.monotonic()
    for op, stages in plan:
        # Honour the operator's semantics rather than running everything:
        # `a || b` must not run b when a worked, or the model reads the
        # fallback text as if it were the real answer.
        if op == "&&" and rc != 0:
            continue
        if op == "||" and rc == 0:
            continue
        left = budget - (time.monotonic() - started)
        if left <= 0:
            return {"error": f"timed out after {budget}s"}
        try:
            out, rc = _exec_pipeline(stages, left, env)
        except FileNotFoundError as e:
            # A missing binary is that link failing (shell rc 127), not the
            # chain dying. `dpkg -l && flatpak list && snap list` threw away
            # two good answers because snap isn't installed on this box.
            missing.append(e.filename)
            out, rc = f"[{e.filename}: not installed]", 127
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {budget}s"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        ran.append(" | ".join(" ".join(s) for s in stages))
        if out:
            chunks.append(out)

    # One command that simply isn't there is an error, not an empty result —
    # the model must not read "" as "nothing is installed".
    if len(plan) == 1 and missing:
        return {"error": f"not installed: {missing[0]}"}

    output = "\n".join(chunks)
    TERMINAL_DIR.mkdir(parents=True, exist_ok=True)
    (TERMINAL_DIR / "last.txt").write_text(output)

    lines = output.splitlines()
    head, page, pages = _page_of(lines, 1)
    out = {"command": " ; ".join(ran), "exit_code": rc,
           "lines": len(lines), "pages": pages, "page": page, "output": head}
    if pages > 1:
        # "find me the content of MyStuff.cfg" got page 1 of 4 and "want me to
        # continue?" — the rest is one call away, so say to make it.
        out["more"] = (f"showing page 1 of {pages} ({len(lines)} lines). If he asked to "
                       f"see or change all of it, call run_command(page=2) and on now — "
                       f"don't ask him whether to continue.")
    return out


def read_output(page=2):
    """Another page of the last run_command output."""
    path = TERMINAL_DIR / "last.txt"
    if not path.exists():
        return {"error": "no command has been run yet"}
    lines = path.read_text().splitlines()
    text, page, pages = _page_of(lines, page or 2)
    return {"page": page, "pages": pages, "lines": len(lines),
            "output": text,
            "more": (f"call run_command(page={page + 1})"
                     if page < pages else "this is the last page")}


# --------------------------------------------------------------------------
# Proposed edits — the model drafts, the human applies
# --------------------------------------------------------------------------
#
# Deliberately NOT a "run this code" tool. The model never writes to disk:
# it stages an edit, the UI renders a red/green diff, and a keypress applies
# it. Same principle as the apply queue — the pipeline's job ends at "here is
# the thing, you decide."
#
# Three hard guards, because self-editing code is the highest-blast-radius
# capability in this stack:
#   1. paths resolve inside ~/johan only (see _safe_path)
#   2. old_string must match EXACTLY ONCE — ambiguity is refused, not guessed
#   3. applying a .py edit syntax-checks the result and auto-reverts if it
#      breaks, so the assistant cannot brick its own engine mid-conversation

PENDING_EDITS = PROJECT_ROOT / "brain/pending-edits.json"
EDIT_BACKUPS = PROJECT_ROOT / "brain/edit-backups"


def _load_edits():
    try:
        return json.loads(PENDING_EDITS.read_text())
    except (OSError, ValueError):
        return []


def _save_edits(edits):
    PENDING_EDITS.parent.mkdir(parents=True, exist_ok=True)
    PENDING_EDITS.write_text(json.dumps(edits, indent=1))


def _diff(path, before, after):
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


EDIT_MAX_BYTES = 1024 * 1024


def _git_root(p):
    """Nearest ancestor of `p` holding a .git entry (dir, or file for
    worktrees/submodules), stopping at $HOME. None if not in a repo."""
    home = Path.home().resolve()
    for parent in [p] + list(p.parents):
        # Stop AT home, not past it: a dotfiles repo at ~ would otherwise
        # make every file in the home directory count as "in a repo".
        if parent == home or home not in parent.parents:
            return None
        if (parent / ".git").exists():
            return parent
    return None


def _edit_path(path):
    """Resolve a file propose_edit/apply_edit may touch. Returns
    (absolute_path, display_name) or raises ValueError.

    Widened 2026-09-23 at Riadh's request from ~/johan only to ~/johan OR any
    git repository under his home — git means every applied edit also shows
    in `git diff` and is revertible there. Still refused: anything outside
    $HOME, secrets (SENSITIVE), .git internals, symlinks that resolve out of
    the repo, binaries, files over 1 MB. Relative paths stay relative to
    ~/johan so existing callers keep working. `project_files` is NOT widened:
    only edits, which are staged and approved one by one."""
    raw = Path(os.path.expanduser(str(path or "").strip()))
    if not str(raw):
        raise ValueError("path is required")
    cand = raw if raw.is_absolute() else PROJECT_ROOT / raw
    real = cand.resolve()                     # follows symlinks: judge the target
    home = Path.home().resolve()
    if home not in real.parents:
        raise ValueError(f"refused: {path} is outside your home directory")
    if SENSITIVE.search(str(real)):
        raise ValueError(f"refused: {path} looks like a secret")
    if ".git" in real.parts:
        raise ValueError("refused: .git internals are never edited")
    proj = PROJECT_ROOT.resolve()
    if real == proj or proj in real.parents:
        display = str(real.relative_to(proj))
    else:
        repo = _git_root(real.parent)
        if repo is None:
            raise ValueError(f"refused: {path} is not inside ~/johan or a git "
                             f"repository — edits are limited to version-"
                             f"controlled code so every change can be reverted")
        display = f"{repo.name}:{real.relative_to(repo)}"
    if not real.is_file():
        raise ValueError(f"no such file: {path}")
    if real.stat().st_size > EDIT_MAX_BYTES:
        raise ValueError(f"refused: {path} is larger than 1 MB")
    with open(real, "rb") as fh:
        if b"\0" in fh.read(8192):
            raise ValueError(f"refused: {path} is a binary file")
    return real, display


def propose_edit(path, old_string, new_string, why=""):
    """Propose a change to a file in ~/johan or any git repo in his home.
    Does NOT write anything.

    `old_string` must appear exactly once in the file — that is the whole
    safety mechanism. A model that half-remembers the code produces a string
    matching zero or many places, and both are refused with a message it can
    act on, rather than silently editing the wrong line."""
    try:
        target, display = _edit_path(path)
    except ValueError as e:
        return {"error": str(e)}
    if old_string == new_string:
        return {"error": "old_string and new_string are identical"}
    if not old_string:
        return {"error": "old_string is required — this tool replaces text, "
                          "it does not append"}

    before = target.read_text()
    hits = before.count(old_string)
    if hits == 0:
        # Show the closest real text instead of just saying no.
        #
        # Asked for a multi-line change, the model INVENTS a plausible anchor
        # — it proposed a comment ("# Truncate to 900 chars ...") that exists
        # nowhere in the file, and a `def wikipedia` body with the wrong
        # first statement. "Read it first" is not something it can act on; it
        # just searched again and re-hallucinated. Handing back the actual
        # lines turns a dead end into a correctable one, the same lesson as
        # every other error message in this codebase.
        import difflib
        probe = (old_string.strip().splitlines() or [""])[0][:80]
        lines = before.splitlines()
        close = difflib.get_close_matches(probe, lines, n=3, cutoff=0.4)
        return {"error": "old_string does not appear in the file — do not "
                          "reconstruct code from memory, copy it verbatim.",
                 "closest_real_lines": close,
                 "hint": ("These are the actual lines, with exact indentation. "
                          "Pick one, copy it character for character, and keep "
                          "old_string SHORT — one line is usually enough to be "
                          "unique.")}
    if hits > 1:
        return {"error": f"old_string appears {hits} times — it must be "
                          f"unique. Include surrounding lines to disambiguate."}

    after = before.replace(old_string, new_string, 1)
    diff = _diff(display, before, after)

    edits = _load_edits()
    edit_id = (max((e["id"] for e in edits), default=0) + 1)
    edits.append({
        "id": edit_id,
        "path": display, "abs_path": str(target),
        "old_string": old_string, "new_string": new_string,
        "why": why or "", "created": datetime.now().isoformat(timespec="seconds"),
        "diff": diff, "turn": _actions.TURN,     # so rewind can find it
    })
    _save_edits(edits)
    # Ask him inline when someone can answer (see johan_approval): the turn
    # waits, and the model learns whether the edit was applied.
    import johan_approval as jap
    decision = jap.ask("edit", edits[-1])
    if decision is not None:
        if decision["choice"] == "no":
            discard_edit(edit_id)
            out = {"declined": True, "edit_id": edit_id, "path": display,
                   "note": "He said NO — the file is unchanged. Don't propose the "
                           "same edit again; follow what he said instead."}
            if decision["reason"]:
                out["he_said"] = decision["reason"]
            return out
        res = apply_edit(edit_id)
        if res.get("error"):
            return {"applied": False, "edit_id": edit_id, "path": display,
                    "error": f"he approved it but applying FAILED: {res['error']}"}
        return {"applied": True, "edit_id": edit_id, "path": display,
                "note": "He approved it and it is written (backup kept). Report that."}
    return {"proposed": True, "edit_id": edit_id, "path": display,
            "lines_added": sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")),
            "lines_removed": sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")),
            "note": "NOT applied. Riadh reviews the diff and applies it "
                    "himself — it is shown to him in Johan's chat on the PC.",
            "diff": diff[:1200]}


def list_edits():
    edits = _load_edits()
    return {"count": len(edits),
            "edits": [{k: e[k] for k in ("id", "path", "why", "created")}
                       for e in edits]}


def apply_edit(edit_id):
    """Write a staged edit to disk, with a backup and a syntax gate."""
    edits = _load_edits()
    edit = next((e for e in edits if e["id"] == int(edit_id)), None)
    if not edit:
        return {"error": f"no pending edit {edit_id}"}
    if edit.get("mode") == "write":
        return _apply_write(edit, edits)
    # Re-validate at apply time, not just at proposal: the file could have
    # been swapped for a symlink out of the repo in between.
    try:
        target, _ = _edit_path(edit.get("abs_path") or edit["path"])
    except ValueError as e:
        return {"error": f"{edit['path']}: {e}"}

    before = target.read_text()
    if before.count(edit["old_string"]) != 1:
        return {"error": "the file changed since this was proposed — the "
                          "anchor text is no longer unique. Discard and redo."}
    after = before.replace(edit["old_string"], edit["new_string"], 1)

    EDIT_BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = edit["path"].replace("/", "__").replace(":", "__")
    backup = EDIT_BACKUPS / f"{safe_name}.{stamp}.bak"
    backup.write_text(before)
    target.write_text(after)

    # A broken .py here could be johan_assistant.py itself — the thing
    # currently running the conversation. Revert rather than leave it broken.
    if target.suffix == ".py":
        try:
            compile(after, str(target), "exec")
        except SyntaxError as e:
            target.write_text(before)
            return {"error": f"edit reverted — it broke Python syntax: "
                              f"line {e.lineno}: {e.msg}",
                     "reverted": True, "backup": str(backup)}

    _save_edits([e for e in edits if e["id"] != int(edit_id)])
    try:
        _actions.note_edit_applied(edit, target, backup, after)   # for rewind
    except Exception:
        pass
    return {"applied": True, "path": edit["path"], "backup": str(backup),
            "note": "backup kept in brain/edit-backups/"}


def _apply_write(edit, edits):
    """write_file's half of apply_edit: same backup, syntax gate and rewind log."""
    import hashlib
    try:
        target = _write_path(edit["abs_path"])
    except ValueError as e:
        return {"error": f"{edit['path']}: {e}"}
    existed = target.exists()
    before = target.read_text(errors="replace") if existed else ""
    if existed != (not edit.get("new_file")) or (
            existed and hashlib.sha256(before.encode()).hexdigest() != edit.get("before_sha")):
        return {"error": "the file changed since this was proposed — read it again and redo"}
    after = edit["content"]
    if target.suffix == ".py":
        try:
            compile(after, str(target), "exec")
        except SyntaxError as e:
            return {"error": f"not written — it isn't valid Python: line {e.lineno}: {e.msg}"}
    EDIT_BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = EDIT_BACKUPS / (edit["path"].replace("/", "__").replace("~", "home") + f".{stamp}.bak")
    backup.write_text(before)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after)
    _save_edits([e for e in edits if e["id"] != edit["id"]])
    try:
        _actions.note_edit_applied(edit, target, backup, after)
    except Exception:
        pass
    return {"applied": True, "path": edit["path"], "backup": str(backup)}


def discard_edit(edit_id):
    edits = _load_edits()
    remaining = [e for e in edits if e["id"] != int(edit_id)]
    if len(remaining) == len(edits):
        return {"error": f"no pending edit {edit_id}"}
    _save_edits(remaining)
    return {"discarded": int(edit_id)}


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def add_task(text):
    conn = _conn(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("insert into tasks (text) values (%s) returning id",
                    (str(text).strip(),))
        tid = cur.fetchone()[0]
    conn.close()
    return {"added": True, "task_id": tid, "text": str(text).strip()}


def list_tasks(include_done=False):
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if include_done:
        cur.execute("select id, text, done from tasks order by done, created_at desc limit 30")
    else:
        cur.execute("select id, text, done from tasks where not done order by created_at desc limit 30")
    rows = [{"task_id": r["id"], "text": r["text"], "done": r["done"]}
            for r in cur.fetchall()]
    conn.close()
    return {"count": len(rows), "tasks": rows}


def complete_task(task_id):
    conn = _conn(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("update tasks set done = true, done_at = now() "
                    "where id = %s and not done returning text", (int(task_id),))
        row = cur.fetchone()
    conn.close()
    if not row:
        return {"error": f"no open task with id {task_id}"}
    return {"completed": True, "task_id": int(task_id), "text": row[0]}


# --------------------------------------------------------------------------
# Consolidated tool surface
#
# The implementations above stay as they are — these are thin dispatchers that
# collapse several tools into one where they were obviously ONE concept split
# across three schemas. 28 tools was ~2,200 tokens of schema and, more
# importantly, 28 things a 4B model had to choose between; it already picked
# `search_jobs` over `get_pipeline_stats` and misreported a count because of
# it. Fewer, clearly-scoped tools is the cheapest available accuracy win.
#
# Deliberately NOT merged: draft_cv vs build_apply_queue (the flagship path —
# "one specific job" and "give me the best N" should stay unambiguous), and
# wikipedia vs web_search (wikipedia still answers when SearXNG's upstream
# engines are rate-limited, which happens routinely).
# --------------------------------------------------------------------------

def tasks(action="list", text=None, task_id=None, include_done=False):
    """One tool for the whole todo list instead of three."""
    action = (action or "list").lower()
    if action in ("add", "create", "new"):
        if not text:
            return {"error": "add needs `text`"}
        return add_task(text)
    if action in ("done", "complete", "finish"):
        if task_id is None:
            return {"error": "done needs `task_id`"}
        return complete_task(task_id)
    if action in ("list", "show", "get"):
        return list_tasks(include_done=include_done)
    return {"error": f"unknown action '{action}' — use add, list or done"}


def memory(query=None, limit=10):
    """Recall by meaning, or list everything when no query is given."""
    if query:
        return recall(query, top_k=limit)
    return list_memories(limit=limit)


def latest_version(name, source="pypi"):
    """Current version of a library, from a package registry or GitHub."""
    src = (source or "pypi").lower()
    if src in ("github", "gh"):
        return github_latest_release(name)
    return package_latest(name, ecosystem=src)


def project_files(action=None, query=None, path=None, max_chars=1500):
    """Read a file, grep the project, or grep inside one file.

    Infers intent from what it was given rather than insisting on the right
    `action`. Asked to change one line in a 900-line file, the model called
    action='search' with a `path` and no `query` — it wanted to grep *within*
    that file, which is an obviously reasonable operation the tool didn't
    support. It got "search needs `query`", retried the identical call four
    times, and gave up without proposing the edit. The lesson from the
    build_apply_queue failure applies to arguments too: an error the model
    cannot act on is the same as no tool at all."""
    action = (action or "").lower()

    if path and query:
        # grep inside one file — the case that was missing
        try:
            target = _safe_path(path)
        except ValueError as e:
            return {"error": str(e)}
        if not target.is_file():
            return {"error": f"no such file: {path}"}
        hits = []
        for i, line in enumerate(target.read_text(errors="replace").splitlines(), 1):
            if str(query).lower() in line.lower():
                hits.append({"line": i, "text": line[:200],
                              "exact": len(line) <= 200})
            if len(hits) >= 20:
                break
        return {"path": path, "query": query, "count": len(hits), "matches": hits,
                "note": ("text is verbatim INCLUDING leading indentation — copy "
                          "it exactly for propose_edit. `exact: false` means the "
                          "line was cut at 200 chars; read the file instead.")
                        if hits else "no match in this file"}

    if path and action in ("", "read", "cat", "open", "search", "grep", "find"):
        return read_project_file(path, max_chars=max_chars)
    if query:
        res = search_project_files(query)
        if not res.get("count"):
            # Dead end otherwise: this tool only sees ~/johan, so asked to
            # "check my tf2 config" it searched the wrong place, found
            # nothing, and asked the user what TF2 was. Point at the tool
            # that CAN look elsewhere instead of stopping.
            res["note"] = (
                "This tool only sees the ~/johan project. For files anywhere "
                "else on the machine (game configs, dotfiles, ~/Downloads, "
                "Steam folders) use run_command with ls/find/cat — e.g. "
                "`find /home/zeros -maxdepth 6 -iname \'*.cfg\' -path \'*tf*\'`.")
        return res
    return {"error": "give `path` to read a file, `query` to search the "
                      "project, or both to search inside one file"}


# --------------------------------------------------------------------------
# files — read / search / write anywhere in his home (2026-10-14)
# --------------------------------------------------------------------------
#
# project_files saw only ~/johan, so a game config or a script in ~/code
# went through run_command + cat, with no line numbers to anchor an edit on.
# Reading and searching need no approval (SENSITIVE still applies — output
# lands in the model's context); writing a whole file is approved on a card
# with the diff, like propose_edit, and can be rewound.

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache",
             ".mypy_cache", ".pytest_cache", "dist", "build", ".tox", ".idea",
             "site-packages", ".local", ".var", ".steam", "snap"}
READ_CHARS = 1700
SEARCH_MAX_FILES = 6000
SEARCH_SECONDS = 4.0


def _home_path(path, base=None):
    """Resolve a path he could mean → (Path) inside his home, or ValueError.
    Relative paths are relative to `base` (default ~/johan)."""
    raw = Path(os.path.expanduser(str(path or "").strip()))
    if not str(path or "").strip():
        raise ValueError("path is required")
    cand = raw if raw.is_absolute() else (base or PROJECT_ROOT) / raw
    real = cand.resolve()
    home = Path.home().resolve()
    if real != home and home not in real.parents:
        raise ValueError(f"refused: {path} is outside your home directory "
                         f"(use run_command for system files)")
    if SENSITIVE.search(str(real)):
        raise ValueError(f"refused: {path} looks like keys, passwords or secrets")
    return real


def _is_binary(p):
    try:
        with open(p, "rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return False


def read_file(path, start=1):
    """A file with line numbers, from line `start`, bounded in size; or a
    folder's listing."""
    try:
        p = _home_path(path)
    except ValueError as e:
        return {"error": str(e)}
    if p.is_dir():
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        shown = [e.name + ("/" if e.is_dir() else "") for e in entries
                 if not SENSITIVE.search(e.name)][:80]
        return {"path": str(p), "folder": True, "entries": shown,
                "count": len(entries)}
    if not p.is_file():
        return {"error": f"no such file: {path} — check the name with "
                         f"files(path=<its folder>) or run_command find"}
    if _is_binary(p):
        return {"error": f"{path} is a binary file"}
    lines = p.read_text(errors="replace").splitlines()
    start = max(1, int(start or 1))
    out, size, n = [], 0, start
    width = len(str(len(lines)))
    while n <= len(lines) and size < READ_CHARS:
        row = f"{n:>{width}}│{lines[n - 1][:400]}"
        out.append(row)
        size += len(row) + 1
        n += 1
    res = {"path": str(p), "lines": len(lines), "from": start,
           "to": n - 1, "text": "\n".join(out)}
    if n <= len(lines):
        res["more"] = f"files(path=..., start={n}) reads on"
    res["note"] = ("numbers are not part of the file; text after │ is verbatim "
                   "(indentation included) — copy it exactly into propose_edit")
    return res


def search_files(query, path=None):
    """Case-insensitive text search through a folder (default ~/johan) or
    one file. Matches keep their indentation, for propose_edit."""
    q = str(query or "").strip()
    if not q:
        return {"error": "query is required"}
    try:
        root = _home_path(path) if path else PROJECT_ROOT.resolve()
    except ValueError as e:
        return {"error": str(e)}
    if not root.exists():
        return {"error": f"no such file or folder: {path}"}
    ql, hits, scanned = q.lower(), [], 0
    t0 = time.monotonic()

    def scan(f):
        nonlocal scanned
        scanned += 1
        try:
            if f.stat().st_size > EDIT_MAX_BYTES or _is_binary(f):
                return
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if ql in line.lower():
                    hits.append({"path": str(f.relative_to(root)) if f != root else f.name,
                                 "line": i, "text": line[:200]})
                    if len(hits) >= 40:
                        return
        except OSError:
            pass

    if root.is_file():
        scan(root)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                                 and not d.startswith("venv") and not SENSITIVE.search(d))
            for name in sorted(filenames):
                if SENSITIVE.search(name):
                    continue
                scan(Path(dirpath) / name)
                if len(hits) >= 40 or scanned >= SEARCH_MAX_FILES:
                    break
            if (len(hits) >= 40 or scanned >= SEARCH_MAX_FILES
                    or time.monotonic() - t0 > SEARCH_SECONDS):
                break
    out = {"searched": str(root), "query": q, "count": len(hits), "matches": hits,
           "files_scanned": scanned}
    if len(hits) >= 40 or scanned >= SEARCH_MAX_FILES or time.monotonic() - t0 > SEARCH_SECONDS:
        out["note"] = "stopped early — narrow it with `path` to a smaller folder"
    elif not hits:
        out["note"] = (f"no match under {root}. Other folder? pass `path` "
                       f"(e.g. ~/Documents) — this tool searches his whole home.")
    return out


WRITE_MAX_BYTES = 512 * 1024


def _write_path(path):
    """Where write_file may write: his home, no secrets, no .git internals,
    never Johan's brain/ or compose/."""
    real = _home_path(path)
    if ".git" in real.parts:
        raise ValueError("refused: .git internals are never written")
    for d in (PROJECT_ROOT / "brain", PROJECT_ROOT / "compose"):
        d = d.resolve()
        if real == d or d in real.parents:
            raise ValueError(f"refused: {d} is Johan's own data")
    if real.is_dir():
        raise ValueError(f"{path} is a folder — give a file path")
    if real.exists() and (_is_binary(real) or real.stat().st_size > EDIT_MAX_BYTES):
        raise ValueError(f"refused: {path} is binary or larger than 1 MB")
    return real


def write_file(path, content, why=""):
    """Create a file or replace its whole content — asked first, with the diff."""
    try:
        target = _write_path(path)
    except ValueError as e:
        return {"error": str(e)}
    content = "" if content is None else str(content)
    if len(content.encode()) > WRITE_MAX_BYTES:
        return {"error": "content is over 512 KB — too big to write from a chat"}
    existed = target.exists()
    before = target.read_text(errors="replace") if existed else ""
    if existed and before == content:
        return {"error": "the file already has exactly this content"}
    if existed and before and len(content) < len(before) // 3:
        hint = (" (that would drop most of the file — to change part of it, use "
                "propose_edit instead)")
    else:
        hint = ""
    import hashlib
    edits = _load_edits()
    edit_id = max((e["id"] for e in edits), default=0) + 1
    display = str(target).replace(str(Path.home()), "~", 1)
    edits.append({
        "id": edit_id, "mode": "write", "path": display, "abs_path": str(target),
        "new_file": not existed, "content": content,
        "before_sha": hashlib.sha256(before.encode()).hexdigest() if existed else None,
        "old_string": before, "new_string": content,
        "why": (why or "") + hint, "created": datetime.now().isoformat(timespec="seconds"),
        "diff": _diff(display, before, content), "turn": _actions.TURN})
    _save_edits(edits)
    import johan_approval as jap
    decision = jap.ask("edit", edits[-1])
    if decision is None:
        return {"proposed": True, "edit_id": edit_id, "path": display,
                "diff": edits[-1]["diff"][:1200],
                "note": "NOT written — it waits for his OK in Johan's chat on the PC."}
    if decision["choice"] == "no":
        discard_edit(edit_id)
        out = {"declined": True, "path": display,
               "note": "He said NO — nothing was written. Follow what he said instead."}
        if decision["reason"]:
            out["he_said"] = decision["reason"]
        return out
    res = apply_edit(edit_id)
    if res.get("error"):
        return {"written": False, "path": display,
                "error": f"he approved it but writing FAILED: {res['error']}"}
    return {"written": True, "created": not existed, "path": display,
            "note": "He approved it and it is written. Report that."}


def files(path=None, query=None, start=None, content=None, why=None, action=None):
    """One tool: read (path), search (query [+ path]), write (path + content)."""
    if content is not None and path:
        return write_file(path, content, why or "")
    if query:
        return search_files(query, path)
    if path:
        return read_file(path, start or 1)
    return {"error": "give `path` to read, `query` to search, or `path` + "
                     "`content` to write a file"}


# --------------------------------------------------------------------------
# Schemas + dispatch, imported by johan_assistant.py
# --------------------------------------------------------------------------

def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                        **({"required": required} if required else {})}}}


TOOLS = [
    # Descriptions are kept short on purpose: every word here is re-read on
    # the first message of every chat, at ~230 tok/s on this card (the whole
    # tool list was 3,889 tokens = ~17 s). Guidance that matters lives once,
    # in the system prompt, not duplicated in both places.
    _fn("memory", "Riadh's stored facts. `text` saves one (when he says remember, or "
                   "states a lasting fact/preference); `query` searches; neither lists "
                   "all; `forget_id` deletes a wrong one.",
        {"text": {"type": "string"}, "query": {"type": "string"},
         "forget_id": {"type": "integer"},
         "kind": {"type": "string", "enum": ["fact", "preference", "personal", "project"]}}),
    _fn("web_search", "Search the web. Then fetch_page the best URL. `site`: one domain.",
        {"query": {"type": "string"}, "site": {"type": "string"}}, ["query"]),
    _fn("fetch_page", "Read a web page as text.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("latest_version", "Live latest version of a package; source='github' for owner/repo.",
        {"name": {"type": "string"},
         "source": {"type": "string", "enum": list(PACKAGE_REGISTRIES) + ["github"]}},
        ["name"]),
    _fn("wikipedia", "Encyclopedia summary.", {"query": {"type": "string"}}, ["query"]),
    _fn("current_datetime", "Today's date and time."),
    _fn("calculate", "Exact arithmetic.", {"expression": {"type": "string"}}, ["expression"]),
    _fn("system_status", "This machine: OS, CPU, RAM, GPU/VRAM, disk, loaded models, services."),
    _fn("tasks", "His todo list: list, add (`text`), done (`task_id`).",
        {"action": {"type": "string", "enum": ["list", "add", "done"]},
         "text": {"type": "string"}, "task_id": {"type": "integer"}}),
    _fn("files", "Files anywhere in /home/zeros. `path` reads it with line numbers (`start` "
                  "to continue) or lists a folder; `query` searches text (in `path`, default "
                  "~/johan); `path`+`content` writes the whole file — he is asked to approve.",
        {"path": {"type": "string"}, "query": {"type": "string"},
         "start": {"type": "integer"}, "content": {"type": "string"},
         "why": {"type": "string"}}),
    _fn("scratchpad", "Working notes for a task spanning several messages (always visible "
                       "to you). `text` writes, `append` adds, `clear` wipes.",
        {"text": {"type": "string"}, "append": {"type": "boolean"},
         "clear": {"type": "boolean"}}),
    _fn("run_command", "Shell anywhere on his machine. Read-only commands (ls, cat, find, "
                        "grep, df, ps, docker ps, git log, nvidia-smi…) run at once; anything "
                        "else is shown to him to approve first (`why` says what it's for). "
                        "Long output is paged: `page`=N reads page N of the last output.",
        {"command": {"type": "string"}, "page": {"type": "integer"},
         "why": {"type": "string"}}),
    _fn("propose_edit", "Edit a file in ~/johan or a git repo under /home/zeros. He is asked "
                         "to approve it; the result comes back to you. `old_string` must match "
                         "exactly once, indentation included — read the file first.",
        {"path": {"type": "string"}, "old_string": {"type": "string"},
         "new_string": {"type": "string"}, "why": {"type": "string"}},
        ["path", "old_string", "new_string"]),
    _fn("search_conversations", "Search what was said in earlier chats.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("check_company_hiring", "Can a company hire in Tunisia? Reads its job board "
                                 "(token, e.g. 'supabase').",
        {"company": {"type": "string"},
         "ats": {"type": "string", "enum": ["greenhouse", "lever", "ashby"]}},
        ["company"]),
]


def memory_tool(text=None, query=None, forget_id=None, kind="fact", limit=10):
    """One tool for remember / recall / list / forget (was three schemas)."""
    if forget_id is not None:
        return forget(forget_id)
    if text:
        return remember(text, kind=kind or "fact")
    return memory(query=query, limit=limit)


def scratchpad(text=None, append=False, clear=False):
    """scratchpad_write + scratchpad_clear as one tool."""
    if clear and not text:
        return scratchpad_clear()
    if clear:
        scratchpad_clear()
    if text:
        return scratchpad_write(text, append=bool(append))
    return {"notes": scratchpad_read() or "(empty)"}


def run_command_tool(command=None, page=None, timeout=20, why=None):
    """run_command + read_output as one tool: `page` pages the last output."""
    if page and not command:
        return read_output(page=page)
    if not command:
        return {"error": "run_command needs `command` (or `page` to read more of the last output)"}
    out = run_command(command, timeout=timeout)
    if out.pop("blocked", False):
        # Outside the read-only list: like Claude Code, ask him — the card
        # shows the exact command; it runs only on his yes. Secrets are never
        # asked about (the checker's "refusing to touch" stays a refusal).
        import johan_approval as jap
        if "refusing to touch" in out["error"]:
            return out
        if not jap.can_ask():
            out["note"] = ("nobody can approve it right now (phone or one-shot) — "
                           "propose_action stages it for him to approve on the PC")
            return out
        res = _actions.propose_action(command, why=why or "")
        res.setdefault("note", "")
        res["asked_because"] = "not on the read-only list, so he was asked first"
        return res
    if out.get("pages", 1) > 1:
        # Long output: also show the lines past page 1 that match his question,
        # so the answer on page 7 doesn't cost six more hops to find.
        import johan_reader as jr
        try:
            first = len(out["output"].splitlines())
            rel = jr.relevant_lines((TERMINAL_DIR / "last.txt").read_text(), skip_first=first)
        except Exception:
            rel = ""
        if rel:
            head = out["output"][:1000]
            out["output"] = head[:head.rfind("\n")] if "\n" in head else head
            out["relevant_later_lines"] = rel
    return out


import johan_sandbox as _sandbox  # noqa: E402  (after SENSITIVE is defined)
TOOLS.append(_sandbox.TOOL)
import johan_actions as _actions  # noqa: E402  (changes to the machine, approved in the chat)
TOOLS.append(_actions.TOOL)

DISPATCH = {
    "memory": memory_tool,
    "web_search": web_search, "fetch_page": fetch_page,
    "latest_version": latest_version, "wikipedia": wikipedia,
    "current_datetime": current_datetime, "calculate": calculate,
    "system_status": system_status, "tasks": tasks,
    "files": files, "project_files": project_files,
    "search_conversations": search_conversations,
    "propose_edit": propose_edit,
    "run_command": run_command_tool, "scratchpad": scratchpad,
    "check_company_hiring": check_company_hiring,
    "run_python": _sandbox.run_python,
    "propose_action": _actions.propose_action,
}
