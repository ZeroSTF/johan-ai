from pathlib import Path
import requests
import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

FACTS = Path.home() / "johan/job-hunter/facts/bullets.yaml"
EMBED = "http://127.0.0.1:8100/embed"
COLLECTION = "johan_bullets"
BATCH = 32

doc = yaml.safe_load(FACTS.read_text())
roles = {r["id"]: r for r in doc["roles"]}
roles.update({e["id"]: e for e in doc.get("education", [])})

texts = []
for b in doc["bullets"]:
    r = roles.get(b["role"], {})
    context = f"{r.get('title', '')} at {r.get('org', '')}".strip(" at ")
    skills = ", ".join(b.get("skills", []))
    texts.append(f"{b['text']} Skills: {skills}. Context: {context}.")

vectors, dim = [], None
for i in range(0, len(texts), BATCH):
    resp = requests.post(EMBED, json={"texts": texts[i:i + BATCH]}, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    dim = data["dim"]
    vectors.extend(data["embeddings"])
    print(f"embedded {len(vectors)}/{len(texts)}")

client = QdrantClient(host="127.0.0.1", port=6333)
if client.collection_exists(COLLECTION):
    client.delete_collection(COLLECTION)
client.create_collection(
    collection_name=COLLECTION,
    vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
)

points = []
for i, (b, vec) in enumerate(zip(doc["bullets"], vectors)):
    r = roles.get(b["role"], {})
    points.append(PointStruct(id=i, vector=vec, payload={
        "bullet_id": b["id"],
        "text": b["text"],
        "skills": b.get("skills", []),
        "domains": b.get("domains", []),
        "confidence": b.get("confidence"),
        "role_id": b["role"],
        "org": r.get("org"),
        "title": r.get("title"),
        "seniority": r.get("seniority"),
    }))

client.upsert(collection_name=COLLECTION, points=points, wait=True)
print(f"indexed {len(points)} bullets at dim {dim}")
