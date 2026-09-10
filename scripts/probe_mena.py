import re
import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) johan-jobhunter/0.1"}

SITES = [
    ("bayt-tunisia", "https://www.bayt.com/en/tunisia/jobs/",
     "https://www.bayt.com/robots.txt"),
    ("bayt-uae", "https://www.bayt.com/en/uae/jobs/software-engineer-jobs/",
     "https://www.bayt.com/robots.txt"),
    ("gulftalent", "https://www.gulftalent.com/jobs/software-engineer",
     "https://www.gulftalent.com/robots.txt"),
    ("keejob", "https://www.keejob.com/offres-emploi/",
     "https://www.keejob.com/robots.txt"),
    ("tanitjobs", "https://www.tanitjobs.com/",
     "https://www.tanitjobs.com/robots.txt"),
    ("optioncarriere", "https://www.optioncarriere.tn/emploi?s=developpeur",
     "https://www.optioncarriere.tn/robots.txt"),
]

for name, url, robots in SITES:
    print(f"\n=== {name}")
    try:
        r = requests.get(robots, headers=UA, timeout=20)
        lines = [l for l in r.text.splitlines()
                 if re.match(r"\s*(user-agent|disallow|crawl-delay)", l, re.I)]
        print("  robots.txt:", r.status_code, f"({len(lines)} directives)")
        for l in lines[:14]:
            print("   ", l.strip())
    except Exception as e:
        print("  robots.txt FAILED:", str(e)[:60])

    try:
        r = requests.get(url, headers=UA, timeout=30)
        html = r.text
        print(f"  page: HTTP {r.status_code}  {len(html)} bytes")
        # does server-rendered HTML already contain job links?
        for pat, label in [
            (r'href="[^"]*/job[s]?/[^"]{6,}', "job links"),
            (r'application/ld\+json', "JSON-LD blocks"),
            (r'"@type"\s*:\s*"JobPosting"', "JobPosting schema"),
            (r'__NEXT_DATA__|__NUXT__|window\.__', "JS app state"),
        ]:
            print(f"    {label}: {len(re.findall(pat, html, re.I))}")
    except Exception as e:
        print("  page FAILED:", str(e)[:60])
