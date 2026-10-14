"""Redraw: how a reference is read and used. No GPU, no model, no files written.

Tag merging (his tags replace the reference's, colours only, scenes drop the
old background), the real-photo guard (whole words — "stripes" is not
"strip"), keep modes in the workflow, and the fallback when a mode's model
files are missing. Every case here is a bug found while building it.

    python test_redraw.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import johan_images as ji        # noqa: E402

results = {}


def check(label, ok):
    results[label] = bool(ok)


GIRL = {"general": ["1girl", "solo", "blonde hair", "long hair", "blue eyes", "smile", "day",
                    "outdoors", "white background", "simple background", "school uniform",
                    "photo (medium)", "asian"], "character": ["lisa (game)"], "rating": "general"}
merge = ji.merge_reference_tags

m = merge("red hair", GIRL)
check("his tags come first", m.startswith("red hair, "))
check("'red hair' drops the reference's 'blonde hair'", "blonde hair" not in m)
check("…but keeps 'long hair' (length isn't colour)", "long hair" in m)
check("a known character's name is carried over", "lisa (game)" in m)
check("photo-medium tags never reach an anime drawing", "photo (medium)" not in m)
check("the tagger's ethnicity guess is dropped (it was wrong on the first photo)", "asian" not in m)
check("no new scene: the reference's background stays", "white background" in m)
m = merge("night, city street, neon lights", GIRL)
check("a new scene drops the old background", "white background" not in m
      and "simple background" not in m and "outdoors" not in m)
check("'night' drops 'day'", ", day" not in m)
check("'backpack' is not a scene ('back' ≠ background)", "white background" in merge("backpack", GIRL))
check("no tags (tagger missing): prompt unchanged", merge("red hair", None) == "red hair")
m = merge("1boy, knight", {"general": ["1girl", "solo", "maid"]})
check("his '1boy' replaces the reference's '1girl' / 'solo'", "1girl" not in m and "solo" not in m
      and "maid" in m)
check("no person tag from him: the reference's stays", "1girl" in merge("knight", GIRL))
check("the tool says places/animals are `no humans`, not a forced 1girl",
      "no humans" in ji.TOOL["function"]["description"])
check("a duplicate of his own tag isn't added twice", merge("smile", GIRL).count("smile") == 1)
check("at most REF_TAGS_MAX tags from the reference",
      len(merge("x", {"general": [f"tag{i}" for i in range(60)]}).split(", ")) == ji.REF_TAGS_MAX + 1)

# Adding someone: one prompt for everyone drew extra copies of her (her 24
# tags outweighed his 3), so each side of the canvas gets its own.
MAID = {"general": ["1girl", "solo", "maid", "blue hair", "full body", "standing",
                    "white background", "simple background", "photo (medium)"]}
old, new = ji.add_prompts("1boy, black hair, suit", MAID)
check("add: her side keeps her (1girl, blue hair, maid)", "1girl" in old and "blue hair" in old)
check("add: his 'black hair' never touches her side", "black hair" not in old)
check("add: the newcomer's side has only him — no 'maid', no '1girl'",
      "maid" not in new and "1girl" not in new and "1boy" in new)
check("add: one newcomer ('solo' — 648 px drew two men once)", "solo" in new.split(", "))
check("add: framed like her (full body)", "full body" in new)
check("add: both sides share the background (else each half drew its own room)",
      "white background" in old and "white background" in new)
check("add: photo-medium tags dropped like any redraw", "photo (medium)" not in old)
old, new = ji.add_prompts("1boy, night, city street", MAID)
check("add: a scene he names replaces her background on BOTH sides",
      "night" in old and "city street" in new and "white background" not in old + new)
old, new = ji.add_prompts("1boy in black suit standing next to 1girl blue hair maid outfit", MAID)
check("add: the model describing HER too (seen) is cut from his side",
      new.startswith("1boy in black suit standing") and "maid" not in new)
old, new = ji.add_prompts("girl with blue hair in a maid outfit, plus a boy with black hair beside her", MAID)
check("add: prose with 'plus … beside her' keeps only the boy",
      "boy with black hair" in new and "maid" not in new and "her" not in new.split(", "))
old, new = ji.add_prompts("1girl, red hair, maid", MAID)
check("add: a second maid girl is still allowed (one of her tags isn't 'her')",
      new.startswith("1girl, red hair, maid"))
old, new = ji.add_prompts("cat", MAID)
check("add: a cat gets no humans + full body, no solo/standing", new == "cat, no humans, full body, white background, simple background")
check("add: no tagger → just his tags", ji.add_prompts("1boy", None) == ("", "1boy, solo"))

from PIL import Image        # noqa: E402
canvas, pw = ji._add_canvas(Image.new("RGB", (832, 1216), (250, 250, 250)))
check("add: a landscape canvas with the picture on the left",
      canvas.size == ji.SIZES["landscape"] and pw < canvas.width // 2)
check("add: padded with the picture's own border colour (a grey pad drew a seam)",
      canvas.getpixel((canvas.width - 5, 5)) == (250, 250, 250))
canvas, pw = ji._add_canvas(Image.new("RGB", (1600, 900), "red"))
check("add: a wide picture is shrunk to leave room", pw <= canvas.width * 0.58
      and canvas.getpixel((canvas.width - 5, canvas.height - 5)) == (255, 0, 0))
regions = [("her", 0, 600), ("him", 500, 716)]
wf = ji._workflow("p", "n", 1216, 832, 1, 6, ref="c.png", keep="add", regions=regions)
areas = [n for n in wf.values() if n["class_type"] == "ConditioningSetArea"]
check("add: one area prompt per side, nothing over the whole canvas",
      len(areas) == 2 and wf["22"]["inputs"]["positive"] != ["5", 0]
      and wf["8"]["inputs"]["positive"] == ["22", 0])
check("add: her lines kept (Canny on the canvas)", wf["20"]["inputs"]["image"] == ["11", 0])
check("add: drawn fresh (empty latent)", wf["7"]["class_type"] == "EmptyLatentImage")


wf = ji._workflow("p", "n", 832, 1216, 1, 6, ref="r.png", strength=0.6, keep="lines")
check("lines: ControlNet on the reference's edges", wf.get("20", {}).get("class_type") == "Canny"
      and wf["8"]["inputs"]["positive"] == ["22", 0])
check("lines: drawn from scratch (denoise 1, empty latent)", wf["8"]["inputs"]["denoise"] == 1.0
      and wf["7"]["class_type"] == "EmptyLatentImage")
wf = ji._workflow("p", "n", 832, 1216, 1, 6, ref="r.png", strength=0.6, keep="look")
check("look: classic img2img at the strength", wf["7"]["class_type"] == "VAEEncode"
      and wf["8"]["inputs"]["denoise"] == 0.6)
wf = ji._workflow("p", "n", 832, 1216, 1, 6, ref="r.png", keep="character")
check("character: IP-Adapter patches the model", wf["8"]["inputs"]["model"] == ["32", 0]
      and wf["7"]["class_type"] == "EmptyLatentImage")
wf = ji._workflow("p", "n", 832, 1216, 1, 6)
check("no reference: plain drawing, nothing extra", set(wf) == {str(i) for i in range(1, 11)})

real = ji.COMFY
ji.COMFY = Path("/nonexistent")
check("a mode whose files are missing says so", ji.keep_available("lines")
      and ji.keep_available("character"))
ji.COMFY = real
check("look needs nothing extra", ji.keep_available("look") is None)
props = ji.TOOL["function"]["parameters"]["properties"]
check("the tool offers keep = lines/look/character", props["keep"]["enum"] == list(ji.KEEP_MODES))

# A drawing ends the turn in code: no model reload just to say "here it is"
# (measured 29 s of the 70 s turn), no fact check, a background re-warm.
import johan_assistant as ja        # noqa: E402
model_calls, warmed = [], []


def fake_model(messages, **k):
    model_calls.append(len(messages))
    if len(model_calls) == 1:
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "generate_image",
                                             "arguments": {"prompt": "red panda"}}}]}
    return {"role": "assistant", "content": "a model answer"}


real = (ja.call_model, ja.DISPATCH["generate_image"], ja.rewarm, ja.log_tool_call, ja.VERIFY_ANSWERS,
        ja._maybe_research, ja._skills.inject)
ja.call_model = fake_model
ja.VERIFY_ANSWERS = False
ja.DISPATCH["generate_image"] = lambda **kw: {"path": "/x/panda.png", "seconds": 31.0}
ja.rewarm = lambda m: warmed.append(len(m))
ja.log_tool_call = lambda *a, **k: None
ja._maybe_research = lambda *a, **k: None
ja._skills.inject = lambda *a, **k: None
try:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "draw a red panda"}]
    ans = ja.run_turn(msgs)
    check("after a drawing, no second model call", len(model_calls) == 1)
    check("…the answer names the file", "/x/panda.png" in ans and "ctrl+o" in ans)
    check("…and the model reloads in the background", warmed)
    check("…nothing to fact-check", ja.ANSWERED_BY_CODE)
    model_calls.clear()
    ja.DISPATCH["generate_image"] = lambda **kw: {"error": "engine failed"}
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "draw a red panda"}]
    ans = ja.run_turn(msgs)
    check("a FAILED drawing still goes back to the model to explain", len(model_calls) == 2
          and ans == "a model answer")
finally:
    (ja.call_model, ja.DISPATCH["generate_image"], ja.rewarm, ja.log_tool_call, ja.VERIFY_ANSWERS,
     ja._maybe_research, ja._skills.inject) = real

for k, ok in results.items():
    print(f"{'ok  ' if ok else 'FAIL'}  {k}")
bad = [k for k, ok in results.items() if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} pass")
sys.exit(1 if bad else 0)
