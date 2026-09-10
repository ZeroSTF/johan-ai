import re
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/128.0",
      "Accept-Language": "fr,en;q=0.8"}
BASE = "https://www.keejob.com"
LIST = f"{BASE}/offres-emploi/"

r = requests.get(LIST, headers=UA, timeout=30)
print(f"listing: HTTP {r.status_code}  {len(r.content)} bytes")
soup = BeautifulSoup(r.text, "html.parser")

# 1. what URL shapes exist on the page?
shapes = Counter()
samples = {}
for a in soup.find_all("a", href=True):
    path = urlparse(urljoin(BASE, a["href"])).path
    shape = re.sub(r"\d+", "N", path).rstrip("/")
    shapes[shape] += 1
    samples.setdefault(shape, (a["href"], a.get_text(" ", strip=True)[:60]))

print("\n--- URL shapes (top 20)")
for shape, n in shapes.most_common(20):
    href, text = samples[shape]
    print(f"{n:4}  {shape[:52]:52} | {text}")

# 2. pagination
print("\n--- pagination candidates")
for a in soup.find_all("a", href=True):
    if re.search(r"page|suivant|next", a["href"] + a.get_text(), re.I):
        print("   ", a["href"][:70], "|", a.get_text(" ", strip=True)[:30])

# 3. what container holds each posting?
print("\n--- repeated block classes")
cls = Counter()
for tag in soup.find_all(class_=True):
    for c in tag.get("class"):
        cls[f"{tag.name}.{c}"] += 1
print("   ", [f"{k}({v})" for k, v in cls.most_common(18)])

# 4. follow one detail page
detail = None
for shape, n in shapes.most_common():
    if re.search(r"offre|emploi|job", shape) and n > 3 and shape.count("/") >= 2:
        detail = urljoin(BASE, samples[shape][0])
        break

if detail:
    print(f"\n--- detail page: {detail}")
    d = requests.get(detail, headers=UA, timeout=30)
    ds = BeautifulSoup(d.text, "html.parser")
    print(f"    HTTP {d.status_code}  {len(d.content)} bytes")
    print(f"    title tag: {(ds.title.get_text(strip=True) if ds.title else '')[:80]}")
    print(f"    JobPosting JSON-LD: {len(re.findall(r'JobPosting', d.text))}")
    body = ds.find("body")
    text = body.get_text("\n", strip=True) if body else ""
    print(f"    total text chars: {len(text)}")
    dcls = Counter()
    for tag in ds.find_all(class_=True):
        t = tag.get_text(" ", strip=True)
        if len(t) > 400:
            for c in tag.get("class"):
                dcls[f"{tag.name}.{c}"] = max(dcls.get(f"{tag.name}.{c}", 0), len(t))
    print("    long-text containers:",
          sorted(dcls.items(), key=lambda x: -x[1])[:6])
else:
    print("\n--- no detail-page shape identified")
