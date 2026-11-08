# Daily SEO Performance Reviewer Agent

Every morning at 06:00 UTC, this agent checks a website's SEO health, compares it with
yesterday, and posts a short prioritised review to the team's Telegram chat.

## How it works

```
GitHub Actions (cron, daily)
   └─ seo_agent.py
        1. COLLECT   robots.txt · sitemap · on-page SEO · Core Web Vitals · Search Console
        2. COMPARE   today's snapshot vs. yesterday's (stored in data/snapshots/)
        3. DETECT    rule-based checks → issues tagged critical / warning / info, and NEW vs ongoing
        4. REVIEW    Claude turns the issues into a ≤250-word brief: what's wrong, why, next step
        5. DELIVER   Telegram (or Slack) message + reports/YYYY-MM-DD.md + snapshot committed to the repo
```

**Design choice:** the rules decide *what* is wrong; the LLM only decides *how to say it* and
in what order. That keeps the review factual (no invented numbers) while still reading like
an analyst wrote it. If the Claude API is unavailable, a plain fallback report is sent.

## The data the agent works from

All raw data is saved daily as JSON in `data/snapshots/`.

| Data | Where it comes from | What it tells us |
|---|---|---|
| robots.txt | Fetched from `<site>/robots.txt` | Whether crawlers are blocked; whether the file changed overnight |
| Sitemap URLs | XML sitemap(s) listed in robots.txt (or `/sitemap.xml`) | How many pages the site asks Google to index; sudden drops |
| On-page SEO (40 pages) | Agent crawls a stable sample of sitemap URLs | Status code, redirects, response time, title, meta description, H1, canonical, noindex, word count |
| Core Web Vitals (3 key pages) | Google PageSpeed Insights API (free) | Lab performance score + real-user LCP / INP / CLS from Chrome UX Report |
| Organic traffic (optional) | Google Search Console API | Clicks, impressions, CTR, average position – week-over-week, by page and query |

Search Console needs owner access to the site, so it switches on only when credentials are
provided. Everything else works for any public website.

## Setup

1. Push this folder to a new GitHub repo.
2. *Settings → Secrets and variables → Actions*:
   - Variable `SITE_URL` (e.g. `https://www.searchbloom.com`)
   - Variable `FOCUS_URLS` – key pages checked every day, comma-separated
     (e.g. `https://www.searchbloom.com/strategy/best-ai-seo-agency-companies-services-usa`)
   - AI writer (pick one, or none for a plain rule-based report):
     - Secret `LLM_API_KEY` – free Google Gemini key from aistudio.google.com (default), or
     - Secret `ANTHROPIC_API_KEY` – Claude
   - Secrets `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (bot from @BotFather), and/or
     secret `SLACK_WEBHOOK_URL` (Slack → create app → Incoming Webhooks)
   - Optional: secret `PSI_API_KEY` (Google Cloud, avoids rate limits)
   - Optional: secret `GSC_SERVICE_ACCOUNT_JSON` + variable `GSC_PROPERTY` (e.g. `sc-domain:yoursite.com`)
3. *Actions → Daily SEO review → Run workflow* to test. After that it runs every morning.

Run locally: `pip install -r requirements.txt && SITE_URL=https://... python seo_agent.py`

## Limitations / next steps

- Checks a sample of 40 pages, not the whole site; JavaScript-rendered content isn't rendered.
- No keyword rank tracking without Search Console (would need a paid SERP API).
- No backlink data (would need Ahrefs/Semrush APIs).
- Thresholds are fixed; with more history they could adapt to each site's normal variance.
- Next: a weekly trend chart, issue tracking with owners (Jira/Linear), and chat replies so
  the team can ask the agent follow-up questions.
