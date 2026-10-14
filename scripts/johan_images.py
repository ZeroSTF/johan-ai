"""generate_image — local, uncensored image generation on the 6 GB card.

Engine: ComfyUI (~/johan/comfyui, its own venv ~/johan/venv-comfy) running
WAI-NSFW-Illustrious v14 (SDXL, anime/illustration) with the DMD2 4-step
LoRA and the fp16-fixed SDXL VAE.

The GPU cannot hold the chat model (~4.7 GB) and an SDXL UNet (~5.1 GB fp16)
at once, so every generation is a handoff:
  1. unload the chat model from Ollama (keep_alive 0)
  2. start ComfyUI if it isn't running (pinned to cores 8-15, like every
     background job here, so a game keeps cores 0-3)
  3. generate
  4. tell ComfyUI to free VRAM, so the next chat turn can reload the model
The server stops itself after IDLE_MINUTES without a request, and
johan-pause / GameMode kills it outright — nothing holds VRAM during a game.
"""

import json
import os
import re
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path.home() / "johan"
COMFY = ROOT / "comfyui"
PY = ROOT / "venv-comfy/bin/python"
HOST = "127.0.0.1"
PORT = 8188
URL = f"http://{HOST}:{PORT}"
OUT_DIR = ROOT / "brain/images"
UPLOADS = ROOT / "brain/uploads"      # pictures sent to Johan (Telegram), never his drawings
STATE = ROOT / "brain/images/.server.json"
LOG = ROOT / "logs/comfyui.log"
IDLE_MINUTES = 10

# Measured on this card 2026-10-03: ComfyUI's automatic choice (fp16 weights,
# fp32 compute — it detects the GTX 16 series) gives 28 s warm / 39 s cold.
# --force-fp16 produced BLACK images AND ran 4x slower (10.8 s/step vs 2.6).
# Do not add it. JOHAN_COMFY_ARGS exists only to re-measure.
SERVER_ARGS = ["--listen", HOST, "--port", str(PORT), "--disable-auto-launch",
               "--preview-method", "none"] + os.environ.get("JOHAN_COMFY_ARGS", "").split()

UNET = "wai_illustrious_v14_unet.safetensors"
CLIP_L = "wai_illustrious_v14_clip_l.safetensors"
CLIP_G = "wai_illustrious_v14_clip_g.safetensors"
VAE = "sdxl_vae_fp16_fix.safetensors"
LORA = "dmd2_sdxl_4step_lora.safetensors"

# One model. `STYLES` stays a table so another can be added later without
# touching the workflow code.
STYLES = {
    "anime": {
        "unet": UNET, "clip_l": CLIP_L, "clip_g": CLIP_G,
        "cfg": 1.0,
        "quality": "masterpiece, best quality, amazing quality, very aesthetic, absurdres",
        "negative": ("bad quality, worst quality, worst detail, sketch, censor, "
                     "lowres, bad anatomy, bad hands, extra fingers, watermark, signature"),
    },
}

SIZES = {"portrait": (832, 1216), "landscape": (1216, 832), "square": (1024, 1024)}
STEPS = 6

def refuse_reason(prompt):
    """Why this prompt is refused, or None. Code, not a model judgement."""
    return None

REF_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_STRENGTH = 0.6
# How a redraw uses its reference (measured 2026-10-14 on an anime figure and
# a real photo, outputs compared side by side):
#   lines     — edges via ControlNet, everything else redrawn from scratch.
#               "night, neon city" on a white-background figure gave a full
#               rainy street with her pose and outfit kept; a photo came out
#               clean anime. Default.
#   look      — classic img2img at `strength`. Keeps colours and layout; at
#               0.6 it ignored "night, city" entirely. Small touch-ups only.
#   character — IP-Adapter: who/what style it is, not where the lines are.
KEEP_MODES = ("lines", "look", "character")
CONTROLNET = "control-lora-canny-rank128.safetensors"
CONTROL_STRENGTH = 0.8
IPADAPTER = "ip-adapter-plus_sdxl_vit-h.safetensors"
CLIP_VISION = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
IPADAPTER_WEIGHT = 0.4          # 0.6+ washed out and duplicated her; 0.4 to 70% of the steps kept her

# ---- reading the reference into tags (WD tagger, CPU, its own process)
TAGGER = ROOT / "scripts/tag_image.py"
TAGGER_MODEL = COMFY / "models/tagger/wd-vit-tagger-v3.onnx"
REF_TAGS_MAX = 24
# Tags that describe the picture as a file, not its content.
_REF_SKIP = {"general", "sensitive", "questionable", "explicit", "monochrome", "greyscale", "comic", "translated",
             "english text", "text focus", "watermark", "signature", "artist name",
             "web address", "twitter username", "patreon username", "jpeg artifacts",
             "realistic", "photorealistic", "photo (medium)", "photo background", "3d",
             # a 448px tagger's guess at ethnicity was wrong on the first photo
             # tried, and the redraw followed it
             "asian", "dark-skinned male", "dark-skinned female", "very dark skin"}

_COLOURS = {"red", "blue", "green", "yellow", "orange", "purple", "pink", "black", "white",
            "grey", "gray", "brown", "blonde", "silver", "aqua", "light", "dark", "multicolored",
            "two-tone", "gradient", "streaked", "lavender", "gold", "golden"}


_COUNT = re.compile(r"\d\+?(girls?|boys?|others?)|no humans|multiple (girls|boys)|solo")
# His tags naming a place or time: then the reference's own background goes
# ("night, city street" on a white-background figure). Whole words only.
_SCENE = re.compile(r"\b(background|scenery|street|city|room|bedroom|classroom|sky|night|"
                    r"sunset|sunrise|indoors|outdoors|forest|beach|ocean|sea|mountains?|"
                    r"field|park|space|cafe|shop|castle|ruins|desert|snow|rain|underwater|"
                    r"landscape|town|village|garden|school|office|stage|train|cityscape)\b", re.I)


def describe_reference(path, timeout=90):
    """Danbooru tags for a picture: {"general", "character", "rating"} or
    None if the tagger isn't installed or fails (redraw still works)."""
    if not (TAGGER_MODEL.exists() and PY.exists()):
        return None
    try:
        r = subprocess.run(["taskset", "-c", "8-15", str(PY), str(TAGGER), str(path)],
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def _words(tag):
    return tag.lower().split()


def merge_reference_tags(prompt, tags):
    """His tags first (what he wants changed), then what the reference shows,
    minus anything his tags replace: "red hair" drops the reference's "blonde
    hair", "night" drops "day". Whole-tag matching, never substrings."""
    if not tags:
        return prompt
    his = [t.strip() for t in prompt.split(",") if t.strip()]
    his_l = {t.lower() for t in his}
    # "red hair" replaces the reference's hair COLOUR, not "long hair".
    his_kind = {_words(t)[-1] for t in his
                if len(_words(t)) > 1 and _words(t)[0] in _COLOURS}
    opposite = {"day": "night", "night": "day", "indoors": "outdoors", "outdoors": "indoors",
                "smile": "frown", "open mouth": "closed mouth", "closed mouth": "open mouth"}
    new_scene = bool(_SCENE.search(prompt))
    his_count = any(_COUNT.fullmatch(t.lower()) for t in his)
    keep = []
    for t in tags.get("character", []) + tags.get("general", []):
        tl = t.lower()
        w = _words(tl)
        if tl in _REF_SKIP or tl in his_l:
            continue
        if his_count and _COUNT.fullmatch(tl):        # he said who: "1boy" replaces "1girl"
            continue
        if new_scene and (w[-1] == "background" or tl in ("indoors", "outdoors", "scenery")):
            continue
        if len(w) > 1 and w[0] in _COLOURS and w[-1] in his_kind:
            continue
        if opposite.get(tl) in his_l:
            continue
        keep.append(t)
        if len(keep) >= REF_TAGS_MAX:
            break
    return ", ".join(his + keep)

def _reference(path, prompt):
    """(PIL image, resolved path) or (None, error)."""
    try:
        from PIL import Image
    except ImportError:
        return None, "PIL is not installed"
    p = Path(str(path).strip().strip("'\"")).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None, f"can't read {path}"
    if not p.is_file():
        return None, f"no picture at {p}"
    if p.suffix.lower() not in REF_EXT:
        return None, f"{p.name} is not a picture (png, jpg, webp, bmp)"
    try:
        import johan_tools as jt
        if jt.SENSITIVE.search(str(p)):
            return None, "refused: that path looks like a secret"
    except Exception:
        pass
    if p.stat().st_size > 40 * 1024 * 1024:
        return None, "that picture is over 40 MB"
    own = OUT_DIR.resolve() in p.parents
    
    try:
        with Image.open(p) as im:
            im.load()
            return im.convert("RGB"), p
    except Exception as e:
        return None, f"can't open {p.name}: {e}"


def _size_for(img):
    """The preset whose shape is closest to the reference's."""
    ratio = img.width / img.height
    return min(SIZES, key=lambda k: abs(SIZES[k][0] / SIZES[k][1] - ratio))


def _upload(img):
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    name = f"johan-ref-{uuid.uuid4().hex[:10]}.png"
    r = requests.post(f"{URL}/upload/image", timeout=60, data={"overwrite": "true"},
                      files={"image": (name, buf.getvalue(), "image/png")})
    r.raise_for_status()
    return r.json()["name"]


# ------------------------------------------------------------------ server

def _up():
    try:
        return requests.get(f"{URL}/system_stats", timeout=2).ok
    except requests.RequestException:
        return False


def _touch(pid=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        pass
    if pid is not None:
        state["pid"] = pid
    state["last_used"] = time.time()
    STATE.write_text(json.dumps(state))


def _release_chat_model():
    """Free the chat model's VRAM before SDXL loads."""
    try:
        import johan_assistant as ja
        ja.release_model()
    except Exception:
        pass
    time.sleep(1.0)            # Ollama returns before CUDA memory is actually freed


def start_server(timeout=120):
    if _up():
        _touch()
        return True
    LOG.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["taskset", "-c", "8-15", "nice", "-n", "5", str(PY), "main.py", *SERVER_ARGS],
        cwd=str(COMFY), stdout=open(LOG, "a"), stderr=subprocess.STDOUT,
        start_new_session=True)
    _touch(proc.pid)
    _spawn_watchdog(proc.pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _up():
            return True
        time.sleep(1)
    return False


def free_vram():
    try:
        requests.post(f"{URL}/free", json={"unload_models": True, "free_memory": True},
                      timeout=10)
    except requests.RequestException:
        pass


def stop_server():
    try:
        pid = json.loads(STATE.read_text()).get("pid")
    except (OSError, ValueError):
        pid = None
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        STATE.unlink()
    except OSError:
        pass


# The idle shutdown runs in its OWN process. The first version used a timer
# thread in the caller — so when the caller exited (a one-shot
# `johan "draw …"`, a test) the engine kept running indefinitely, measured
# holding 1.6 GB of RAM with nobody left to stop it.
_WATCHDOG = r"""
import json, os, signal, sys, time
state, pid, idle = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
while True:
    time.sleep(30)
    try:
        os.kill(pid, 0)
    except OSError:
        break                                   # engine already gone
    try:
        last = json.load(open(state)).get("last_used", 0)
    except Exception:
        last = 0
    if time.time() - last > idle:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
        try:
            os.unlink(state)
        except OSError:
            pass
        break
"""


def _spawn_watchdog(pid):
    subprocess.Popen(["/usr/bin/python3", "-c", _WATCHDOG, str(STATE), str(pid),
                      str(IDLE_MINUTES * 60)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------ workflow

def _workflow(positive, negative, width, height, seed, steps, ref=None, strength=1.0,
              model=None, keep="look"):
    model = model or STYLES["anime"]
    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": model["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": model["clip_l"], "clip_name2": model["clip_g"],
                         "type": "sdxl"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": ["1", 0], "lora_name": LORA, "strength_model": 1.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": positive}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        # DMD2's recipe: LCM sampler, sgm_uniform schedule, CFG 1.
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "seed": seed, "steps": steps,
                         "cfg": model.get("cfg", 1.0), "sampler_name": "lcm", "scheduler": "sgm_uniform",
                         "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0],
                                                     "filename_prefix": "johan"}},
    }
    if ref:
        # Image-to-image: start from the reference (scaled and centre-cropped
        # to the output size) instead of noise; `strength` is how much of the
        # schedule runs — 0.3 keeps nearly everything, 0.9 keeps the layout.
        wf["11"] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        wf["12"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["11", 0], "upscale_method": "lanczos",
                               "width": width, "height": height, "crop": "center"}}
        if keep == "look":
            wf["7"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["12", 0], "vae": ["3", 0]}}
            wf["8"]["inputs"]["denoise"] = strength
        elif keep == "lines":
            # The reference's edges steer the first 80% of the steps; the
            # last steps are free so lines don't look traced.
            wf["20"] = {"class_type": "Canny", "inputs": {"image": ["12", 0], "low_threshold": 0.3,
                                                           "high_threshold": 0.7}}
            wf["21"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": CONTROLNET}}
            wf["22"] = {"class_type": "ControlNetApplyAdvanced",
                        "inputs": {"positive": ["5", 0], "negative": ["6", 0], "control_net": ["21", 0],
                                   "image": ["20", 0], "strength": CONTROL_STRENGTH,
                                   "start_percent": 0.0, "end_percent": 0.8, "vae": ["3", 0]}}
            wf["8"]["inputs"]["positive"] = ["22", 0]
            wf["8"]["inputs"]["negative"] = ["22", 1]
        elif keep == "character":
            wf["30"] = {"class_type": "IPAdapterModelLoader", "inputs": {"ipadapter_file": IPADAPTER}}
            wf["31"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": CLIP_VISION}}
            wf["32"] = {"class_type": "IPAdapterAdvanced",
                        "inputs": {"model": ["4", 0], "ipadapter": ["30", 0], "image": ["11", 0],
                                   "weight": IPADAPTER_WEIGHT, "weight_type": "linear",
                                   "combine_embeds": "concat", "start_at": 0.0, "end_at": 0.7,
                                   "embeds_scaling": "V only", "clip_vision": ["31", 0]}}
            wf["8"]["inputs"]["model"] = ["32", 0]
            del wf["12"]
    return wf


def keep_available(keep):
    """None if this redraw mode's model files are installed, else why not."""
    need = {"lines": [("controlnet", CONTROLNET)],
            "character": [("ipadapter", IPADAPTER), ("clip_vision", CLIP_VISION)]}.get(keep, [])
    if keep == "character" and not (COMFY / "custom_nodes/ComfyUI_IPAdapter_plus").is_dir():
        return "the IP-Adapter node isn't installed"
    gone = [f for d, f in need if not (COMFY / "models" / d / f).exists()]
    return f"missing {', '.join(gone)}" if gone else None


def generate_image(prompt, size=None, seed=None, on_progress=None,
                   reference=None, strength=None, style="anime", keep=None):
    """Generate one illustration. With `reference` (a picture path), redraw
    that picture: `keep` = lines (default: its outline/pose, everything else
    new), look (`strength` 0.2-0.95, how much to change) or character.
    Returns {"path", "seconds", ...} or {"error"}."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "generate_image needs a prompt"}
    reason = refuse_reason(prompt)
    if reason:
        return {"error": reason}
    if not PY.exists() or not (COMFY / "main.py").exists():
        return {"error": "image engine not installed (~/johan/comfyui)"}
    # `style` is still accepted (older chats and image sidecars pass it), but
    # there is one model now.
    style = "anime"
    model = STYLES[style]
    missing = [f for d, f in (("diffusion_models", model["unet"]),
                              ("text_encoders", model["clip_l"]),
                              ("text_encoders", model["clip_g"]),
                              ("vae", VAE), ("loras", LORA))
               if not (COMFY / "models" / d / f).exists()]
    if missing:
        return {"error": f"image model files missing: {', '.join(missing)}"}
    ref_img = ref_path = ref_tags = None
    if reference:
        ref_img, ref_path = _reference(reference, prompt)
        if ref_img is None:
            return {"error": ref_path}
        size = size if size in SIZES else _size_for(ref_img)
        keep = keep if keep in KEEP_MODES else ("look" if strength not in (None, "") else "lines")
        if keep_available(keep):
            keep = "look"                     # always installed
        ref_tags = describe_reference(ref_path)
        if reason:
            return {"error": reason}
        try:
            strength = min(0.95, max(0.2, float(strength if strength not in (None, "")
                                                else DEFAULT_STRENGTH)))
        except (TypeError, ValueError):
            strength = DEFAULT_STRENGTH
    size = size if size in SIZES else "portrait"
    width, height = SIZES[size]
    seed = int(seed) if seed not in (None, "") else int.from_bytes(os.urandom(4), "big")
    progress = on_progress or (lambda m: None)

    started = time.monotonic()
    progress("freeing the GPU")
    _release_chat_model()
    progress("starting the image engine")
    if not start_server():
        return {"error": f"image engine failed to start — see {LOG}"}
    _touch()

    # A redraw is told what the reference shows, or at higher strength the
    # content drifts toward whatever his few tags describe.
    drawn = merge_reference_tags(prompt, ref_tags) if ref_tags else prompt
    positive = f"{model['quality']}, {drawn}"

    ref_name = None
    if ref_img is not None:
        try:
            ref_name = _upload(ref_img)
        except Exception as e:
            return {"error": f"couldn't hand the reference to the engine: {e}"}
    wf = _workflow(positive, model["negative"], width, height, seed, STEPS,
                   ref=ref_name, strength=strength or 1.0, model=model, keep=keep or "look")
    try:
        r = requests.post(f"{URL}/prompt", json={"prompt": wf, "client_id": uuid.uuid4().hex},
                          timeout=30)
        r.raise_for_status()
        pid = r.json()["prompt_id"]
    except Exception as e:
        return {"error": f"image engine rejected the job: {e}"}

    progress("drawing")
    deadline = time.monotonic() + 600
    outputs = None
    while time.monotonic() < deadline:
        try:
            h = requests.get(f"{URL}/history/{pid}", timeout=10).json()
        except Exception:
            h = {}
        _touch()                       # a long job must not look idle
        if pid in h:
            status = h[pid].get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                err = msgs[0][1].get("exception_message", "unknown") if msgs else "unknown"
                free_vram()
                return {"error": f"generation failed: {str(err)[:300]}"}
            outputs = h[pid].get("outputs", {})
            if outputs:
                break
        time.sleep(1)
    free_vram()
    _touch()
    if not outputs:
        return {"error": "generation timed out"}

    img = next((im for o in outputs.values() for im in o.get("images", [])), None)
    if not img:
        return {"error": "the engine finished but produced no image"}
    data = requests.get(f"{URL}/view", params={"filename": img["filename"],
                        "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
                        timeout=30).content
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"{stamp}-{seed}.png"
    path.write_bytes(data)
    try:
        import sys as _sys
        session = getattr(_sys.modules.get("johan_assistant"), "SESSION_ID", None)
    except Exception:
        session = None
    meta = {"prompt": prompt, "session": session, "size": size, "width": width, "height": height,
            "seed": seed, "steps": STEPS, "cfg": model.get("cfg", 1.0), "model": model["unet"], "style": style,
            **({"reference": str(ref_path), "keep": keep,
                "strength": strength if keep == "look" else None,
                "reference_tags": drawn[len(prompt):].lstrip(", ")} if ref_name else {}),
            "seconds": round(time.monotonic() - started, 1),
            "created": datetime.now().isoformat(timespec="seconds")}
    path.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    # ComfyUI keeps its own copy in comfyui/output; ours is the one that
    # matters, so drop theirs instead of storing every image twice.
    for leftover in (COMFY / "output" / img.get("subfolder", "") / img["filename"],
                     COMFY / "input" / ref_name if ref_name else None):
        try:
            leftover and leftover.unlink()
        except OSError:
            pass
    return {"path": str(path), **meta}


def list_images(limit=60):
    out = []
    for p in sorted(OUT_DIR.glob("*.png"), reverse=True)[:limit]:
        try:
            meta = json.loads(p.with_suffix(".json").read_text())
        except (OSError, ValueError):
            meta = {}
        out.append({"path": str(p), **meta})
    return out


TOOL = {"type": "function", "function": {
    "name": "generate_image",
    # No concrete example prompt here: the first version had one ("1girl,
    # silver hair, red eyes, ... cinematic lighting") and the model pasted
    # "silver hair, red eyes" into a request for a knight in black armor.
    # A small model copies examples literally — describe the format instead.
    "description": (
        "Draw a picture on his GPU (uncensored, ~40 s, anime/illustration). "
        "`prompt` is danbooru tags. New picture: who is in it first — `no humans` for "
        "places, animals and things; 1girl/1boy for people (their real sex); a known "
        "character as `name, series` — then ONLY details he gave or you looked up. "
        "Redraw: `reference` = the picture's path, and ONLY the change he asked for (the "
        "picture is read automatically). Tell him where it saved; ctrl+o opens it."),
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "size": {"type": "string", "enum": ["portrait", "landscape", "square"]},
        "reference": {"type": "string"},
        "keep": {"type": "string", "enum": list(KEEP_MODES),
                 "description": "redraw: lines = same pose/outline, new everything else "
                                "(default); look = small touch-up; character = same "
                                "character, new scene"},
        "strength": {"type": "number", "description": "look only: 0.3 small tweak … 0.9"},
    }, "required": ["prompt"]}}}


def delete_image(path):
    for p in (Path(path), Path(path).with_suffix(".json")):
        try:
            p.unlink()
        except OSError:
            pass


def delete_for_session(session_id):
    """Delete every image drawn in one conversation. Returns how many."""
    n = 0
    for img in list_images(10_000):
        if session_id and img.get("session") == session_id:
            delete_image(img["path"])
            n += 1
    return n
