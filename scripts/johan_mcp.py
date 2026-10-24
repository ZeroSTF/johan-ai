"""Johan's tools for OpenCode (or any MCP client), over stdio.

The daily assistant is OpenCode on the 35B (2026-10-24); Johan keeps what
OpenCode doesn't have — drawing and redrawing, picture search, search on his
own SearXNG, deep research, the job pipeline, memory about him, and the
browser/code checks for things it builds. Each tool keeps
Johan's own tuned description and runs Johan's real code, guards included.

One tool behaves differently from inside Johan:
  generate_image   the image engine needs the whole card, so drawing stops
                   the 35B; OpenCode's next message would hit a dead server.
                   This restarts the model before returning, and opens the
                   picture in his image viewer (OpenCode can't show pictures).

Machine changes are NOT exposed: OpenCode's own shell and approval rules handle
them (decided 2026-10-24 — one approval system, not two).

    venv-embed/bin/python3 scripts/johan_mcp.py      # OpenCode starts it
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("JOHAN_MODEL", "qwen36")

import anyio                                 # noqa: E402
import mcp.types as mt                       # noqa: E402
from mcp.server.lowlevel import Server       # noqa: E402
from mcp.server.stdio import stdio_server    # noqa: E402

import johan_assistant as ja   # noqa: E402
import johan_llm as llm        # noqa: E402

# Only what OpenCode can't do itself (2026-10-24): it runs code, tests, `date`,
# python maths and nvidia-smi in its own shell, fetches pages and keeps todos.
EXPOSED = ["generate_image", "find_images", "web_search", "deep_research", "memory",
           "build_apply_queue", "search_jobs", "get_job", "get_pipeline_stats", "draft_cv",
           "applied", "try_page"]

INSTRUCTIONS = ("Johan's tools on Riadh's PC: drawing and picture search, web search on his "
                "own search engine, his job applications, and memories about him.")

def _open(path):
    try:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _run(name, kwargs):
    args = {k: v for k, v in kwargs.items() if v is not None}
    fn = ja.DISPATCH[name]
    if name == "generate_image":
        try:
            res = fn(**args)
        finally:
            try:
                llm.ensure(ja.MODEL)        # the drawing unloaded it
            except Exception:
                pass
        if isinstance(res, dict) and res.get("path"):
            _open(res["path"])
            res["shown"] = "opened in his image viewer"
        return res
    return fn(**args)


# Johan's own schemas, enums and descriptions — they were tuned on real
# failures (the image tool's "no concrete example", the job tools' warnings).
SPECS = {t["function"]["name"]: t["function"] for t in ja.TOOLS if t["function"]["name"] in EXPOSED}


async def list_tools(ctx, params):
    return mt.ListToolsResult(tools=[
        mt.Tool(name=n, description=f.get("description", ""),
                input_schema=f.get("parameters") or {"type": "object", "properties": {}})
        for n, f in SPECS.items()])


async def call_tool(ctx, params):
    name, args = params.name, dict(params.arguments or {})
    if name not in SPECS:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=f"unknown tool {name}")],
                                 is_error=True)
    try:
        # Tools block (a drawing takes ~a minute): run them off the event loop.
        res = await anyio.to_thread.run_sync(lambda: _run(name, args))
    except TypeError as e:
        res = {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:      # noqa: BLE001 - the model sees the error, like in Johan
        res = {"error": f"{name} failed: {type(e).__name__}: {e}"}
    text = json.dumps(res, default=str, ensure_ascii=False)
    err = isinstance(res, dict) and bool(res.get("error"))
    return mt.CallToolResult(content=[mt.TextContent(type="text", text=text[:20000])], is_error=err)


server = Server("johan", instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
