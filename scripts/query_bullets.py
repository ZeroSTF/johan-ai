import sys
import requests
from qdrant_client import QdrantClient

query = " ".join(sys.argv[1:]) or "Java backend engineer"
vec = requests.post(
    "http://127.0.0.1:8100/embed",
    json={"texts": [query]},
    timeout=120,
).json()["embeddings"][0]

hits = QdrantClient(host="127.0.0.1", port=6333).query_points(
    collection_name="johan_bullets",
    query=vec,
    limit=8,
    with_payload=True,
).points

print(f"query: {query}\n")
for h in hits:
    p = h.payload
    print(f"{h.score:.3f}  {p['bullet_id']}  [{p['confidence']:<8}]  {p['text'][:80]}")
