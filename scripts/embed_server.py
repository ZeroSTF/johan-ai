import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""

from contextlib import asynccontextmanager
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-m3"
THREADS = 4          # 4 physical cores (CPUs 8-15 are 4 cores x 2 threads)
MAX_BATCH = 64

torch.set_num_threads(THREADS)
state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["model"] = SentenceTransformer(MODEL_NAME, device="cpu")
    state["dim"] = state["model"].get_sentence_embedding_dimension()
    yield
    state.clear()


app = FastAPI(title="johan-embed", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1)


@app.get("/health")
def health():
    ready = "model" in state
    return {"status": "ok" if ready else "loading",
            "model": MODEL_NAME,
            "dim": state.get("dim"),
            "threads": THREADS}


@app.post("/embed")
def embed(req: EmbedRequest):
    if "model" not in state:
        raise HTTPException(503, "model still loading")
    if len(req.texts) > MAX_BATCH:
        raise HTTPException(413, f"max {MAX_BATCH} texts per request")
    vectors = state["model"].encode(
        req.texts,
        normalize_embeddings=True,     # cosine == dot product downstream
        batch_size=8,
        show_progress_bar=False,
    )
    return {"dim": state["dim"], "embeddings": [v.tolist() for v in vectors]}
