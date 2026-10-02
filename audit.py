"""
Evidence Audit for looksmaxxing.guide
Crawls the sitemap, classifies each article's outbound sources, and flags
health articles whose claims aren't backed by medical/primary sources.
It does NOT judge whether claims are true — it shows where sourcing is thin.
Output: index.html (dashboard) + audit.json (raw data).
"""
import json, re, html, datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

SITE = "https://looksmaxxing.guide"
UA = {"User-Agent": "evidence-audit/0.1 (portfolio task)"}

# --- Source tiers (editorial judgment, kept explicit so editors can change them) ---
MEDICAL = ["pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nih.gov", "doi.org", "cochranelibrary.com",
           "who.int", "cdc.gov", "nhs.uk", "fda.gov", "ema.europa.eu", "medlineplus.gov", "mayoclinic.org",
           "clevelandclinic.org", "aad.org", "jamanetwork.com", "thelancet.com", "nejm.org", "bmj.com",
           "nature.com", "sciencedirect.com", "springer.com", "wiley.com", "frontiersin.org", "plos.org",
           "academic.oup.com", "journals.lww.com", "acpjournals.org", "examine.com", "nice.org.uk", "mhra.gov.uk"]
PRESS = ["reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "nytimes.com", "theguardian.com",
         "washingtonpost.com", "wsj.com", "ft.com", "npr.org", "cnn.com", "gq.com", "gqmiddleeast.com",
         "menshealth.com", "bloomberg.com", "hollywoodreporter.com", "abcnews.com", "abcnews.go.com", "slate.com", "variety.com", "nbcnews.com", "cbsnews.com", "theatlantic.com", "vox.com", "wired.com", "time.com", "forbes.com"]
TABLOID = ["tmz.com", "dailymail.co.uk", "thesun.co.uk", "nypost.com", "unilad.com", "uniladtech.com",
           "news-usa.today", "yahoo.com", "mirror.co.uk", "dexerto.com", "dailystar.co.uk", "buzzfeed.com", "thetab.com", "ladbible.com", "sportskeeda.com", "win.gg", "socialschmuck.com"]
IGNORE = ["fonts.googleapis.com", "fonts.gstatic.com", "twitter.com", "x.com", "facebook.com",
          "linkedin.com", "reddit.com/submit", "wa.me", "pinterest.com"]
HEALTH_PILLARS = {"looks", "fitness"}
HEALTH_WORDS = re.compile(r"inject|dose|dosing|supplement|steroid|testosterone|trt|surgery|surgical|minoxidil|"
                          r"finasteride|tretinoin|retinol|glp-1|ozempic|overdose|peptide|bonesmash|mewing|"
                          r"skin|acne|hair loss|sleep|diet|fat loss|weight loss|medical|drug", re.I)


def tier(domain):
    d = domain.lower().removeprefix("www.")
    for name, lst in (("medical", MEDICAL), ("press", PRESS), ("tabloid", TABLOID)):
        if any(d == x or d.endswith("." + x) for x in lst):
            return name
    return "other"


def article_urls():
    xml = requests.get(f"{SITE}/sitemap-0.xml", headers=UA, timeout=20).text
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    # articles live at /en/<pillar>/<slug>/  (skip pillar index pages, legal, influencer profiles)
    return [u for u in urls if re.match(rf"{SITE}/en/(?!influencers|legal)[^/]+/[^/]+/$", u)]


def audit(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="utf-8")
    except Exception as e:
        return {"url": url, "error": str(e)}
    body = soup.find("article") or soup
    title = (soup.find("h1").get_text(strip=True) if soup.find("h1") else url)
    pillar = url.split("/")[4]

    ld = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(item, dict) and item.get("@type") in ("Article", "MedicalWebPage", "BlogPosting"):
                ld = item

    counts, domains = {"medical": 0, "press": 0, "tabloid": 0, "other": 0}, {}
    for a in body.find_all("a", href=True):
        host = urlparse(a["href"]).netloc
        if not host or "looksmaxxing.guide" in host or any(i in a["href"] for i in IGNORE):
            continue
        t = tier(host)
        counts[t] += 1
        domains.setdefault(host.removeprefix("www."), t)

    text = body.get_text(" ", strip=True)
    words = len(text.split())
    # health = health keyword in the title, or a health pillar whose body actually discusses health topics
    is_health = bool(HEALTH_WORDS.search(title)) or (pillar in HEALTH_PILLARS and len(HEALTH_WORDS.findall(text)) >= 5)
    author = ld.get("author", {})
    author_type = author.get("@type") if isinstance(author, dict) else "Unknown"

    flags = []
    if is_health and counts["medical"] == 0:
        flags.append("Health topic, no medical sources")
    if counts["tabloid"] > 0 and counts["tabloid"] >= counts["medical"]:
        flags.append("Tabloids outnumber medical sources")
    if is_health and not ld.get("reviewedBy") and author_type != "Person":
        flags.append("No named author or medical reviewer")
    if not ld.get("dateModified"):
        flags.append("No last-updated date")
    if sum(counts.values()) == 0:
        flags.append("No external sources at all")

    # Priority: unsourced health content first, weighted by how much text makes claims
    score = (3 if "Health topic, no medical sources" in flags else 0) + \
            (2 if "Tabloids outnumber medical sources" in flags else 0) + \
            len(flags) * 0.5 + (1 if is_health and words > 1500 else 0)

    return {"url": url, "title": title, "pillar": pillar, "health": is_health, "words": words,
            "counts": counts, "domains": domains, "published": ld.get("datePublished"),
            "modified": ld.get("dateModified"), "author_type": author_type,
            "flags": flags, "score": round(score, 1)}


def render(rows):
    ok = [r for r in rows if "error" not in r]
    health = [r for r in ok if r["health"]]
    no_med = [r for r in health if r["counts"]["medical"] == 0]
    total_links = {k: sum(r["counts"][k] for r in ok) for k in ("medical", "press", "tabloid", "other")}
    ok.sort(key=lambda r: -r["score"])
    esc = html.escape

    def chip(d, t):
        return f'<span class="chip {t}">{esc(d)}</span>'

    trs = "\n".join(
        f'<tr><td><a href="{esc(r["url"])}" target="_blank">{esc(r["title"])}</a>'
        f'<div class="meta">{esc(r["pillar"])} · {r["words"]} words{" · health" if r["health"] else ""}</div></td>'
        f'<td class="n">{r["counts"]["medical"]}</td><td class="n">{r["counts"]["press"]}</td>'
        f'<td class="n">{r["counts"]["tabloid"]}</td><td class="n">{r["counts"]["other"]}</td>'
        f'<td>{"".join(f"<div class=flag>{esc(f)}</div>" for f in r["flags"]) or "<span class=ok>OK</span>"}</td>'
        f'<td class="doms">{"".join(chip(d, t) for d, t in sorted(r["domains"].items(), key=lambda x: x[1]))}</td></tr>'
        for r in ok)

    pct = lambda a, b: f"{(100 * a / b):.0f}%" if b else "–"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evidence Audit — looksmaxxing.guide</title>
<style>
:root{{--bg:#0e0f11;--card:#17191c;--fg:#e8e8e8;--mute:#9aa0a6;--med:#2e9d6a;--press:#3b7dd8;--tab:#d9534f;--oth:#666}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
.wrap{{max-width:1200px;margin:auto;padding:32px 20px}}h1{{margin:0 0 4px}}.sub{{color:var(--mute);margin:0 0 24px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}}
.stat{{background:var(--card);padding:16px;border-radius:10px}}.stat b{{font-size:28px;display:block}}.stat span{{color:var(--mute);font-size:13px}}
.box{{background:var(--card);padding:16px 20px;border-radius:10px;margin-bottom:24px}}
.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:900px}}
th,td{{padding:10px;border-bottom:1px solid #2a2d31;vertical-align:top;text-align:left}}th{{color:var(--mute);font-weight:500;font-size:13px}}
td a{{color:var(--fg)}}.meta{{color:var(--mute);font-size:12px}}.n{{text-align:center;font-variant-numeric:tabular-nums}}
.flag{{color:#f0b429;font-size:13px}}.ok{{color:var(--med)}}.chip{{display:inline-block;font-size:11px;padding:2px 6px;border-radius:4px;margin:2px;color:#fff}}
.medical{{background:var(--med)}}.press{{background:var(--press)}}.tabloid{{background:var(--tab)}}.other{{background:var(--oth)}}
</style></head><body><div class="wrap">
<h1>Evidence Audit — looksmaxxing.guide</h1>
<p class="sub">Which articles back their health claims with medical sources? Crawled {len(ok)} articles from the live sitemap · {datetime.date.today()}</p>
<div class="stats">
<div class="stat"><b>{len(health)}</b><span>health-related articles</span></div>
<div class="stat"><b>{len(no_med)} ({pct(len(no_med), len(health))})</b><span>health articles with zero medical sources</span></div>
<div class="stat"><b>{total_links['medical']}</b><span>medical/primary links site-wide</span></div>
<div class="stat"><b>{total_links['tabloid']}</b><span>tabloid links site-wide</span></div>
</div>
<div class="box"><b>How to read this.</b> This counts and classifies sources; it does not judge whether a claim is true.
News links are fine for event facts (what an influencer said or did). The flag fires when a health article has no
medical source behind it. Source tiers: <span class="chip medical">medical / primary</span>
<span class="chip press">reputable press</span> <span class="chip tabloid">tabloid / aggregator</span> <span class="chip other">other</span>.
Sorted by priority: unsourced health content first.</div>
<div class="scroll"><table><thead><tr><th>Article</th><th>Med</th><th>Press</th><th>Tabloid</th><th>Other</th><th>Flags</th><th>Sources</th></tr></thead>
<tbody>{trs}</tbody></table></div>
<p class="sub" style="margin-top:24px">Raw data: <a style="color:var(--fg)" href="audit.json">audit.json</a></p>
</div></body></html>"""


if __name__ == "__main__":
    urls = article_urls()
    print(f"Auditing {len(urls)} articles…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(audit, urls))
    json.dump(rows, open("audit.json", "w"), indent=2)
    open("index.html", "w", encoding="utf-8").write(render(rows))
    bad = [r for r in rows if "error" not in r and r["health"] and r["counts"]["medical"] == 0]
    print(f"Done. {len(bad)} health articles with no medical sources. Wrote index.html + audit.json")
