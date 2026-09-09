import sys
import requests

CANDIDATES = [
    "gitlab", "elastic", "canonical", "hashicorp", "datadog", "mongodb",
    "confluent", "cloudflare", "twilio", "sonarsource", "contentful",
    "docker", "grafanalabs", "vercel", "supabase", "n26", "personio",
    "getir", "deel", "remotecom", "toptal", "automattic", "doist",
    "stripe", "airbnb", "dropbox", "asana", "figma", "notion",
]

GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
LV = "https://api.lever.co/v0/postings/{}?mode=json"
UA = {"User-Agent": "johan-jobhunter/0.1 (personal job search)"}

found = []
for token in CANDIDATES:
    for name, url, counter in (
        ("greenhouse", GH.format(token), lambda d: len(d.get("jobs", []))),
        ("lever", LV.format(token), lambda d: len(d)),
    ):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            n = counter(r.json())
            if n:
                print(f"OK   {name:11} {token:14} {n:4} jobs")
                found.append((name, token, n))
        except Exception:
            pass

print(f"\n{len(found)} boards found")
print("\nBOARDS = [")
for name, token, _ in found:
    print(f'    ("{name}", "{token}"),')
print("]")
