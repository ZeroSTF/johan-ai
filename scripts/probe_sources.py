import json
import requests

UA = {"User-Agent": "johan-jobhunter/0.1 (personal job search)"}

TESTS = [
    ("arbeitnow-de", "https://www.arbeitnow.com/api/job-board-api?visa_sponsorship=true"),
    ("arbeitnow-uk", "https://www.arbeitnow.co.uk/api/job-board-api"),
    ("remotejobs.org", "https://remotejobs.org/api/v1/jobs?category=programming&limit=5"),
    ("remotive", "https://remotive.com/api/remote-jobs?category=software-dev&limit=5"),
    ("remoteok", "https://remoteok.com/api"),
]

for name, url in TESTS:
    try:
        r = requests.get(url, headers=UA, timeout=30)
        print(f"\n=== {name}  HTTP {r.status_code}  {len(r.content)} bytes")
        if r.status_code != 200:
            continue
        d = r.json()
        rows = d.get("data") if isinstance(d, dict) else d
        print(f"    rows: {len(rows) if rows else 0}")
        if rows:
            first = rows[0] if not isinstance(rows[0], dict) or "legal" not in rows[0] else rows[1]
            print("    keys:", sorted(first.keys())[:18])
            print("    sample:", json.dumps(
                {k: str(v)[:70] for k, v in list(first.items())[:6]}, indent=6))
    except Exception as e:
        print(f"\n=== {name}  FAILED: {e}")
