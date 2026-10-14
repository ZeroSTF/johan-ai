"""Read a picture into danbooru tags (WD ViT tagger v3, CPU).

Runs in ~/johan/venv-comfy as its own process (onnxruntime lives there, not
in Johan's venv — same reason the browser has its own venv). Prints JSON:
{"general": [...], "character": [...], "rating": "general|sensitive|..."}

    venv-comfy/bin/python tag_image.py <picture> [threshold]
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL_DIR = Path.home() / "johan/comfyui/models/tagger"
MODEL = MODEL_DIR / "wd-vit-tagger-v3.onnx"
TAGS = MODEL_DIR / "wd-vit-tagger-v3.csv"
GENERAL = 0.35          # the model card's suggested general threshold
CHARACTER = 0.85        # character tags only when sure — a wrong name redraws someone else
KEEP_UNDERSCORE = {"0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>", "=_=",
                   ">_<", "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u", "x_x", "|_|",
                   "||_||"}


def _prepare(path, size):
    im = Image.open(path)
    im.load()
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    im = Image.alpha_composite(bg, im).convert("RGB")
    side = max(im.size)
    sq = Image.new("RGB", (side, side), (255, 255, 255))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    sq = sq.resize((size, size), Image.BICUBIC)
    arr = np.asarray(sq, dtype=np.float32)[:, :, ::-1]          # RGB -> BGR
    return np.ascontiguousarray(arr[None])


def tag(path, threshold=GENERAL):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    sess = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    probs = sess.run(None, {inp.name: _prepare(path, inp.shape[1])})[0][0]
    with open(TAGS, newline="") as f:
        rows = list(csv.DictReader(f))
    general, character, ratings = [], [], {}
    for row, p in zip(rows, probs):
        name = row["name"]
        name = name if name in KEEP_UNDERSCORE else name.replace("_", " ")
        cat = row["category"]
        if cat == "9":
            ratings[name] = float(p)
        elif cat == "4" and p >= CHARACTER:
            character.append((name, float(p)))
        elif cat == "0" and p >= threshold:
            general.append((name, float(p)))
    general.sort(key=lambda t: -t[1])
    character.sort(key=lambda t: -t[1])
    return {"general": [n for n, _ in general], "character": [n for n, _ in character],
            "rating": max(ratings, key=ratings.get) if ratings else None}


if __name__ == "__main__":
    th = float(sys.argv[2]) if len(sys.argv) > 2 else GENERAL
    print(json.dumps(tag(sys.argv[1], th)))
