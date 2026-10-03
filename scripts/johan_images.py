"""generate_image — local, uncensored image generation on the 6 GB card.

Engine: ComfyUI (~/johan/comfyui, its own venv ~/johan/venv-comfy) running
WAI-NSFW-Illustrious v14 (SDXL, anime/illustration) with the DMD2 4-step
LoRA and the fp16-fixed SDXL VAE. Riadh chose illustration over photoreal
(2026-10-03), so only that model was downloaded.

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

# Two models, same SDXL plumbing (same VAE, same DMD2 4-step LoRA). "anime"
# writes danbooru tags; "real" (RealVisXL V5.0, SG161222 on Hugging Face,
# SHA-256 checked on download, added 2026-10-06) takes plain English.
STYLES = {
    "anime": {
        "unet": UNET, "clip_l": CLIP_L, "clip_g": CLIP_G,
        "cfg": 1.0,
        "quality": "masterpiece, best quality, amazing quality, very aesthetic, absurdres",
        "negative": ("bad quality, worst quality, worst detail, sketch, censor, "
                     "lowres, bad anatomy, bad hands, extra fingers, watermark, signature"),
    },
    "real": {
        "unet": "realvis_v5_unet.safetensors",
        "clip_l": "realvis_v5_clip_l.safetensors",
        "clip_g": "realvis_v5_clip_g.safetensors",
        # CFG 1 (DMD2's default) gives the prompt the least possible pull and
        # ignores the negative entirely; the anime model copes because it was
        # trained on exact tags, this one drifted from what he typed. 1.5 adds
        # real guidance at ~2x the time per step (2026-10-11, untested A/B).
        "cfg": 1.5,
        "quality": "RAW photo, photorealistic, natural skin texture, detailed, sharp focus",
        "negative": ("worst quality, low quality, illustration, 3d, 2d, painting, cartoon, "
                     "anime, sketch, deformed, bad anatomy, bad hands, extra fingers, "
                     "watermark, text, signature"),
    },
}

SIZES = {"portrait": (832, 1216), "landscape": (1216, 832), "square": (1024, 1024)}
STEPS = 6

def refuse_reason(prompt):
    """Why this prompt is refused, or None. Code, not a model judgement."""
    return None

REF_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_STRENGTH = 0.6


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
              model=None):
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
        # DMD2's recipe: LCM sampler, sgm_uniform schedule, CFG 1 (per style).
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
        wf["7"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["12", 0], "vae": ["3", 0]}}
        wf["8"]["inputs"]["denoise"] = strength
    return wf


def generate_image(prompt, size=None, seed=None, on_progress=None,
                   reference=None, strength=None, style="anime"):
    """Generate one illustration. With `reference` (a picture path), redraw
    that picture instead: `strength` 0.2-0.95, how much to change it.
    Returns {"path", "seconds", ...} or {"error"}."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "generate_image needs a prompt"}
    reason = refuse_reason(prompt)
    if reason:
        return {"error": reason}
    if not PY.exists() or not (COMFY / "main.py").exists():
        return {"error": "image engine not installed (~/johan/comfyui)"}
    style = (style or "anime").lower()
    style = {"realistic": "real", "photo": "real", "photorealistic": "real",
             "illustration": "anime"}.get(style, style)
    if style not in STYLES:
        return {"error": f"unknown style '{style}' — use anime or real"}
    model = STYLES[style]
    missing = [f for d, f in (("diffusion_models", model["unet"]),
                              ("text_encoders", model["clip_l"]),
                              ("text_encoders", model["clip_g"]),
                              ("vae", VAE), ("loras", LORA))
               if not (COMFY / "models" / d / f).exists()]
    if missing:
        return {"error": f"image model files missing: {', '.join(missing)}"
                         + (" (the realistic model may still be downloading)"
                            if style == "real" else "")}
    ref_img = ref_path = None
    if reference:
        ref_img, ref_path = _reference(reference, prompt)
        if ref_img is None:
            return {"error": ref_path}
        size = size if size in SIZES else _size_for(ref_img)
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

    positive = f"{model['quality']}, {prompt}"

    ref_name = None
    if ref_img is not None:
        try:
            ref_name = _upload(ref_img)
        except Exception as e:
            return {"error": f"couldn't hand the reference to the engine: {e}"}
    wf = _workflow(positive, model["negative"], width, height, seed, STEPS,
                   ref=ref_name, strength=strength or 1.0, model=model)
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
            **({"reference": str(ref_path), "strength": strength} if ref_name else {}),
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
        "Draw a picture on his GPU (uncensored, ~40 s). style 'anime' (default): "
        "`prompt` is danbooru tags — subject/count (1girl, no humans…) then ONLY "
        "the details he gave; add nothing he didn't ask for. style 'real' (photo, "
        "realistic): a short plain description. A picture he points at goes in "
        "`reference` to redraw it. Tell him where it saved; ctrl+o opens it."),
    "parameters": {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "style": {"type": "string", "enum": ["anime", "real"]},
        "size": {"type": "string", "enum": ["portrait", "landscape", "square"]},
        "reference": {"type": "string"},
        "strength": {"type": "number", "description": "redraw: 0.3 small tweak … 0.9 keep only layout"},
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
