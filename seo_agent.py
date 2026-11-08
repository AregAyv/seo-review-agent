"""
Daily SEO Performance Reviewer Agent
------------------------------------
1. COLLECT  – robots.txt, sitemap, on-page SEO for a stable sample of pages,
              Core Web Vitals (PageSpeed Insights), optional Google Search Console.
2. COMPARE  – load yesterday's snapshot and diff against today.
3. DETECT   – deterministic rules turn data + diffs into issues (critical / warning / info).
4. REVIEW   – Claude turns the issues into a short, prioritised brief for the team.
5. DELIVER  – post to Slack, save a markdown report and the raw snapshot.
"""
import os, re, html, json, gzip, time, hashlib, datetime as dt
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

SITE = os.environ.get("SITE_URL", "https://www.example.com").rstrip("/")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))
# Key pages always audited first and always included in the PageSpeed checks (comma-separated)
FOCUS_URLS = [u.strip() for u in os.environ.get("FOCUS_URLS", "").split(",") if u.strip()]
PSI_PAGES = int(os.environ.get("PSI_PAGES", "3"))
DATA_DIR, REPORT_DIR = Path("data/snapshots"), Path("reports")
UA = {"User-Agent": "Mozilla/5.0 (compatible; SEO-Review-Agent/1.0)"}
TODAY = dt.date.today().isoformat()
ASSET_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".pdf", ".mp4", ".zip", ".xml")


def fetch(url, **kw):
    return requests.get(url, headers=UA, timeout=20, **kw)


# ---------------------------------------------------------------- 1. COLLECT
def blocks_everything(robots_txt):
    """True if the group for User-agent: * contains 'Disallow: /'."""
    agents, in_rules, blocked = [], False, False
    for raw in robots_txt.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        key, val = [x.strip() for x in line.split(":", 1)]
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(val)
        elif key in ("allow", "disallow"):
            in_rules = True
            if key == "disallow" and val == "/" and "*" in agents:
                blocked = True
    return blocked


def get_robots():
    try:
        r = fetch(f"{SITE}/robots.txt")
        text = r.text if r.ok else ""
        status = r.status_code
    except Exception:
        text, status = "", 0
    return {
        "status": status,
        "hash": hashlib.md5(text.encode()).hexdigest(),
        "blocks_all": blocks_everything(text),
        "sitemaps": [l.split(":", 1)[1].strip() for l in text.splitlines()
                     if l.lower().startswith("sitemap:")],
    }


def sitemap_urls(sitemaps, limit=5000):
    queue, seen, urls = list(sitemaps) or [f"{SITE}/sitemap.xml"], set(), []
    while queue and len(urls) < limit:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            content = fetch(sm).content
            if sm.endswith(".gz"):
                content = gzip.decompress(content)
            root = ET.fromstring(content)
        except Exception:
            continue
        # Only the <loc> directly inside each <url>/<sitemap>; nested ones like <image:loc> are images, not pages
        locs = [c.text.strip() for entry in root for c in entry
                if c.tag.split("}")[-1] == "loc" and c.text]
        if root.tag.endswith("sitemapindex"):
            queue += locs
        else:
            urls += locs
    return urls[:limit]


def homepage_links():
    """Fallback when there is no sitemap: internal links on the homepage."""
    try:
        soup = BeautifulSoup(fetch(SITE).text, "html.parser")
    except Exception:
        return []
    host = urlparse(SITE).netloc
    links = {urljoin(SITE, a["href"]).split("#")[0] for a in soup.find_all("a", href=True)}
    return [l for l in links if urlparse(l).netloc == host]


def pick_sample(urls):
    """Focus pages + homepage + a STABLE sample (same pages every day, so diffs are meaningful)."""
    urls = [u for u in urls if not urlparse(u).path.lower().endswith(ASSET_EXT)]
    head = list(dict.fromkeys(FOCUS_URLS + [SITE + "/"]))
    taken = {u.rstrip("/") for u in head}
    rest = sorted({u for u in urls if u.rstrip("/") not in taken},
                  key=lambda u: hashlib.md5(u.encode()).hexdigest())
    return head + rest[: max(0, MAX_PAGES - len(head))]


def audit_page(url):
    start = time.time()
    try:
        r = fetch(url, allow_redirects=True)
        for attempt in range(2):  # 429 = the server asks US to slow down: wait and retry
            if r.status_code != 429:
                break
            wait = r.headers.get("Retry-After", "")
            time.sleep(min(int(wait), 30) if wait.isdigit() else 10 * (attempt + 1))
            r = fetch(url, allow_redirects=True)
    except Exception as e:
        return {"url": url, "status": 0, "error": str(e)[:120]}
    page = {"url": url, "status": r.status_code, "final_url": r.url,
            "redirected": bool(r.history), "response_s": round(time.time() - start, 2)}
    if "text/html" not in r.headers.get("Content-Type", ""):
        return page
    s = BeautifulSoup(r.text, "html.parser")
    robots = s.find("meta", attrs={"name": lambda x: x and x.lower() == "robots"})
    desc = s.find("meta", attrs={"name": lambda x: x and x.lower() == "description"})
    canon = s.find("link", rel=lambda x: x and "canonical" in x)
    title = s.title.get_text(strip=True) if s.title else ""
    robots_directives = (robots.get("content", "") if robots else "") + r.headers.get("X-Robots-Tag", "")
    page.update(
        title=title,
        title_len=len(title),
        meta_desc_len=len(desc.get("content", "").strip()) if desc else 0,
        h1_count=len(s.find_all("h1")),
        canonical=urljoin(r.url, canon["href"]) if canon and canon.get("href") else "",
        noindex="noindex" in robots_directives.lower(),
        word_count=len(s.get_text(" ", strip=True).split()),
    )
    return page


def pagespeed(url):
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if os.environ.get("PSI_API_KEY"):
        params["key"] = os.environ["PSI_API_KEY"]
    try:
        j = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                         params=params, timeout=120).json()
    except Exception as e:
        return {"url": url, "error": str(e)[:120]}
    if "lighthouseResult" not in j:
        return {"url": url, "error": j.get("error", {}).get("message", "no result")[:120]}
    lh, field = j["lighthouseResult"], j.get("loadingExperience", {}).get("metrics", {})
    audits, score = lh.get("audits", {}), lh.get("categories", {}).get("performance", {}).get("score")
    cls = field.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile")
    return {
        "url": url,
        "perf_score": round(score * 100) if score is not None else None,
        "lab_lcp_s": round(audits.get("largest-contentful-paint", {}).get("numericValue", 0) / 1000, 2),
        "lab_cls": round(audits.get("cumulative-layout-shift", {}).get("numericValue", 0), 3),
        # Field data = real Chrome users (CrUX, 28-day rolling). Only exists for sites with enough traffic.
        "field_lcp_ms": field.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile"),
        "field_inp_ms": field.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile"),
        "field_cls": cls / 100 if cls is not None else None,
    }


def search_console():
    """Optional: only runs if you have access to the site's Search Console property."""
    key, prop = os.environ.get("GSC_SERVICE_ACCOUNT_JSON"), os.environ.get("GSC_PROPERTY")
    if not (key and prop):
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(key), scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
    end = dt.date.today() - dt.timedelta(days=3)          # GSC data lags ~2-3 days
    cur = (end - dt.timedelta(days=6), end)                # last 7 days
    prev = (end - dt.timedelta(days=13), end - dt.timedelta(days=7))  # 7 days before that

    def query(rng, dim=None):
        body = {"startDate": rng[0].isoformat(), "endDate": rng[1].isoformat(), "rowLimit": 500}
        if dim:
            body["dimensions"] = [dim]
        rows = svc.searchanalytics().query(siteUrl=prop, body=body).execute().get("rows", [])
        return {(r["keys"][0] if dim else "total"):
                {k: r[k] for k in ("clicks", "impressions", "ctr", "position")} for r in rows}

    out = {"window": f"{cur[0]}..{cur[1]} vs {prev[0]}..{prev[1]}",
           "totals": {"current": query(cur).get("total", {}), "previous": query(prev).get("total", {})}}
    for dim in ("page", "query"):
        c, p = query(cur, dim), query(prev, dim)
        moves = [{"key": k,
                  "clicks_now": c.get(k, {}).get("clicks", 0), "clicks_before": p.get(k, {}).get("clicks", 0),
                  "pos_now": round(c.get(k, {}).get("position", 0), 1),
                  "pos_before": round(p.get(k, {}).get("position", 0), 1)}
                 for k in set(c) | set(p)]
        moves.sort(key=lambda m: m["clicks_now"] - m["clicks_before"])
        out[f"{dim}_losers"], out[f"{dim}_winners"] = moves[:10], moves[-5:][::-1]
    return out


# ------------------------------------------------------- 2+3. COMPARE & DETECT
def find_issues(today, prev):
    issues, grouped = [], defaultdict(list)

    def add(sev, cat, msg, key=None):
        issues.append({"id": key or msg, "severity": sev, "category": cat, "message": msg})

    old_pages = {p["url"]: p for p in (prev or {}).get("pages", [])}
    old_psi = {p["url"]: p for p in (prev or {}).get("pagespeed", [])}

    # Site-level
    rb = today["robots"]
    if rb["status"] >= 500 or rb["status"] == 0:
        add("critical", "crawling", f"robots.txt unreachable (status {rb['status']})")
    if rb["blocks_all"]:
        add("critical", "crawling", "robots.txt blocks ALL crawlers (User-agent: * / Disallow: /)")
    if prev and prev["robots"]["hash"] != rb["hash"]:
        add("warning", "crawling", "robots.txt changed since yesterday – confirm it was intentional")
    n, n_old = today["sitemap_url_count"], (prev or {}).get("sitemap_url_count")
    if n == 0:
        add("warning", "crawling", "No URLs found in sitemap (or sitemap missing)")
    elif n_old and n < n_old * 0.9:
        add("warning", "crawling", f"Sitemap shrank from {n_old} to {n} URLs (-{100 - round(100 * n / n_old)}%)")

    # Page-level
    titles = defaultdict(list)
    for p in today["pages"]:
        u, o = p["url"], old_pages.get(p["url"], {})
        if p["status"] == 429:
            grouped[("info", "data health",
                     "Not checked today: the server rate-limited the agent (HTTP 429), "
                     "not a problem for normal visitors")].append(u)
            continue
        if p["status"] == 0 or p["status"] >= 400:
            was_ok = o.get("status") == 200
            add("critical" if was_ok else "warning", "availability",
                f"{u} returns {p['status'] or p.get('error')}" + (" (was 200 yesterday)" if was_ok else ""))
            continue
        if p.get("noindex"):
            newly = o and not o.get("noindex")
            add("critical" if newly else "warning", "indexing",
                f"{u} is set to noindex" + (" – NEW since yesterday" if newly else ""))
        if p.get("redirected") and o and not o.get("redirected"):
            add("warning", "availability", f"{u} now redirects to {p['final_url']}")
        if p.get("response_s", 0) > 3:
            grouped[("warning", "speed", "Slow server response (>3s)")].append(u)
        if "title" not in p:
            continue
        titles[p["title"]].append(u)
        if not p["title"]:
            grouped[("warning", "on-page", "Missing <title>")].append(u)
        elif not 30 <= p["title_len"] <= 65:
            grouped[("info", "on-page", "Title length outside 30–65 chars")].append(u)
        if o.get("title") and o["title"] != p["title"]:
            add("info", "on-page", f"Title changed on {u}: '{o['title']}' → '{p['title']}'")
        if p["meta_desc_len"] == 0:
            grouped[("info", "on-page", "Missing meta description")].append(u)
        if p["h1_count"] != 1:
            grouped[("info", "on-page", "Not exactly one <h1>")].append(u)
        if p["canonical"] and p["canonical"].rstrip("/") != p["final_url"].rstrip("/"):
            grouped[("info", "indexing", "Canonical points to a different URL")].append(u)
        if o.get("word_count", 0) > 200 and p["word_count"] < 0.5 * o["word_count"]:
            add("warning", "content", f"Content on {u} dropped from {o['word_count']} to {p['word_count']} words")
    for t, urls in titles.items():
        if t and len(urls) > 1:
            grouped[("warning", "on-page", f"Duplicate title '{t[:60]}'")].extend(urls)

    # Core Web Vitals (Google 'good' thresholds: LCP ≤2.5s, INP ≤200ms, CLS ≤0.1)
    for s in today["pagespeed"]:
        if s.get("perf_score") is None:
            continue
        u, o = s["url"], old_psi.get(s["url"], {})
        if o.get("perf_score") and s["perf_score"] <= o["perf_score"] - 10:
            add("warning", "speed", f"Mobile performance score on {u} fell {o['perf_score']} → {s['perf_score']}")
        if s["perf_score"] < 50:
            add("warning", "speed", f"Mobile performance score is poor on {u}: {s['perf_score']}/100",
                key=f"poor-psi-{u}")
        if (s.get("field_lcp_ms") or 0) > 2500:
            add("warning", "speed", f"Real-user LCP on {u} is {s['field_lcp_ms']}ms (good ≤ 2500)", key=f"lcp-{u}")
        if (s.get("field_inp_ms") or 0) > 200:
            add("warning", "speed", f"Real-user INP on {u} is {s['field_inp_ms']}ms (good ≤ 200)", key=f"inp-{u}")
        if (s.get("field_cls") or 0) > 0.1:
            add("warning", "speed", f"Real-user CLS on {u} is {s['field_cls']} (good ≤ 0.1)", key=f"cls-{u}")

    # Search Console (organic traffic)
    g = today.get("gsc")
    if g and g["totals"]["previous"].get("clicks"):
        now, before = g["totals"]["current"].get("clicks", 0), g["totals"]["previous"]["clicks"]
        pct = round(100 * (now - before) / before)
        if pct <= -15:
            add("critical" if pct <= -30 else "warning", "traffic",
                f"Organic clicks {pct}% week-over-week ({before} → {now})", key="gsc-total")
        for m in g["page_losers"]:
            drop = m["clicks_before"] - m["clicks_now"]
            if drop >= 10 and drop >= 0.3 * m["clicks_before"]:
                add("warning", "traffic", f"{m['key']} lost {drop} clicks WoW "
                    f"(position {m['pos_before']} → {m['pos_now']})", key=f"gsc-page-{m['key']}")
        for m in g["query_losers"]:
            if m["pos_before"] and m["pos_now"] - m["pos_before"] >= 3:
                add("info", "rankings", f"Query '{m['key']}' slipped from position {m['pos_before']} "
                    f"to {m['pos_now']}", key=f"gsc-q-{m['key']}")

    for (sev, cat, label), urls in grouped.items():
        urls = sorted(set(urls))
        add(sev, cat, f"{label}: {len(urls)} page(s), e.g. {', '.join(urls[:3])}", key=label)

    old_ids = {i["id"] for i in (prev or {}).get("issues", [])}
    rank = {"critical": 0, "warning": 1, "info": 2}
    for i in issues:
        i["new"] = bool(prev) and i["id"] not in old_ids
    return sorted(issues, key=lambda i: (rank[i["severity"]], not i["new"]))


# ------------------------------------------------------------------ 4. REVIEW
SYSTEM = """You are an SEO analyst writing the daily SEO review for a website team.
Use ONLY the data provided – never invent numbers or causes you can't see.
You can't see whether Google has indexed a page, only whether it's blocked (noindex/robots), so never say a page is 'indexed'.
Format for a chat message: *single asterisks* for bold, • for bullets, plain URLs (no markdown links,
no # headers, no tables). Keep it under 250 words:
1. First line: overall status emoji (🔴 critical issues / 🟡 warnings / 🟢 healthy) + one-sentence summary.
2. *Needs attention today* – max 5 items, most important first. Prioritise critical, then NEW issues,
   then anything affecting traffic. For each: what's wrong, why it matters, one concrete next step.
3. *Trends* – one or two lines on traffic / Core Web Vitals if data exists.
4. *Ongoing (not new)* – one line summarising known issues still open.
Issues in the "data health" category mean the agent could not measure something; say so in one line,
never present them as problems users experience.
If "focus_pages" are listed, always include a one-line status for each of them, even if healthy.
If nothing is wrong, say so briefly."""


def fallback_review(snap):
    lines = [f"*SEO review – {SITE} – {TODAY}*"]
    for sev in ("critical", "warning", "info"):
        items = [i for i in snap["issues"] if i["severity"] == sev]
        if items:
            lines.append(f"\n*{sev.upper()} ({len(items)})*")
            lines += [f"• {'🆕 ' if i['new'] else ''}{i['message']}" for i in items[:10]]
    return "\n".join(lines) if snap["issues"] else lines[0] + "\n🟢 No issues found."


def write_review(snap):
    """Uses Claude if ANTHROPIC_API_KEY is set, otherwise any OpenAI-compatible API
    (default: Google Gemini free tier) if LLM_API_KEY is set, otherwise a plain rule-based report."""
    payload = json.dumps({
        "site": SITE, "date": TODAY, "focus_pages": FOCUS_URLS,
        "pages_checked": len(snap["pages"]), "sitemap_urls": snap["sitemap_url_count"],
        "issues": snap["issues"][:40],
        "core_web_vitals": snap["pagespeed"],
        "search_console": snap["gsc"] and {k: snap["gsc"][k] for k in ("window", "totals", "page_winners")},
    }, indent=1)
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            msg = anthropic.Anthropic().messages.create(
                model=os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5",
                max_tokens=1200, system=SYSTEM,
                messages=[{"role": "user", "content": payload}])
            return "".join(b.text for b in msg.content if b.type == "text")
        if os.environ.get("LLM_API_KEY"):
            url = os.environ.get("LLM_API_URL") or \
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            body = {"model": os.environ.get("LLM_MODEL") or "models/gemini-flash-lite-latest",
                    "messages": [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": payload}]}
            for attempt in range(1, 4):  # free tiers are sometimes slow/overloaded: retry
                try:
                    r = requests.post(url, timeout=180, json=body,
                                      headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"})
                    if r.ok:
                        return r.json()["choices"][0]["message"]["content"]
                    if r.status_code not in (429, 500, 502, 503, 504):
                        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
                    err = f"{r.status_code}"
                except requests.exceptions.Timeout:
                    err = "timeout"
                print(f"AI attempt {attempt} failed ({err}), retrying...")
                time.sleep(15 * attempt)
            raise RuntimeError("AI service did not respond after 3 attempts")
    except Exception as e:
        print("AI call failed, using plain report instead:", e)
    return fallback_review(snap)


# ----------------------------------------------------------------- 5. DELIVER
def to_telegram_html(text):
    """Turn the review's chat formatting (*bold*, `code`) into Telegram-safe HTML."""
    t = html.escape(text, quote=False)
    t = re.sub(r"&lt;(https?://[^|&]+)(?:\|[^&]*)?&gt;", r"\1", t)        # Slack-style <url|text> links
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)                           # **bold**
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<b>\1</b>", t)  # *bold*
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    return t


def chunks(text, size=3500):
    """Telegram messages max out at 4096 chars: split on line breaks."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line
    return out + [cur] if cur else out


def tg_post(api, payload):
    """POST to Telegram with a longer timeout and up to 3 tries (networks can be flaky)."""
    for attempt in range(1, 4):
        try:
            return requests.post(api, json=payload, timeout=60)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == 3:
                raise
            print(f"Telegram attempt {attempt} failed ({type(e).__name__}), retrying...")
            time.sleep(5 * attempt)


def send_telegram(text):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for part in chunks(text):
        r = tg_post(api, {"chat_id": chat, "text": to_telegram_html(part),
                          "parse_mode": "HTML", "disable_web_page_preview": True})
        if not r.ok:  # formatting rejected? send as plain text rather than not at all
            r = tg_post(api, {"chat_id": chat, "text": part, "disable_web_page_preview": True})
        if not r.ok:
            raise RuntimeError(f"Telegram {r.status_code}: {r.text[:200]}")


def deliver(text):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{TODAY}.md").write_text(text, encoding="utf-8")
    print(text)
    errors = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        try:
            send_telegram(text)
            print("\n✅ Sent to Telegram")
        except Exception as e:
            errors.append(f"Telegram: {e}")
    if os.environ.get("SLACK_WEBHOOK_URL"):
        try:
            requests.post(os.environ["SLACK_WEBHOOK_URL"], json={"text": text}, timeout=20).raise_for_status()
            print("\n✅ Sent to Slack")
        except Exception as e:
            errors.append(f"Slack: {e}")
    if errors:  # report is already saved; fail loudly so a missed delivery is noticed
        raise RuntimeError("Delivery failed – " + " | ".join(errors))


def load_previous():
    files = sorted(f for f in DATA_DIR.glob("*.json") if f.stem < TODAY)
    return json.loads(files[-1].read_text()) if files else None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prev = load_previous()
    robots = get_robots()
    urls = sitemap_urls(robots["sitemaps"])
    sample = pick_sample(urls or homepage_links())
    pages = []
    for u in sample:
        pages.append(audit_page(u))
        time.sleep(1.5)  # be polite to the server (too fast = HTTP 429)
    snap = {
        "date": TODAY, "site": SITE, "robots": robots,
        "sitemap_url_count": len(urls), "pages": pages,
        "pagespeed": [pagespeed(u) for u in sample[:PSI_PAGES]],
        "gsc": search_console(),
    }
    snap["issues"] = find_issues(snap, prev)
    (DATA_DIR / f"{TODAY}.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
    deliver(write_review(snap))


if __name__ == "__main__":
    main()
