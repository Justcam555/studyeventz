#!/usr/bin/env python3
"""
scrape_events.py — Discover upcoming events on Thailand agent websites.

Visits each Thailand agent's website with Playwright (async, 8 concurrent),
finds the events/news page, and uses Claude (Sonnet 4.6) to extract structured
event details. Keeps only events within the next 30 days and stores them in
agents.db.

Usage:
    python scrape_events.py                   # scrape all Thailand agents
    python scrape_events.py --limit 10        # test on first 10
    python scrape_events.py --report          # generate HTML report
    python scrape_events.py --refresh         # re-scrape (clear stale rows)

Scheduled weekly on Nesta via cron.
"""

import argparse
import asyncio
import json
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anthropic

DB_PATH = Path(__file__).parent / "data" / "agents.db"
REPORTS_DIR = Path(__file__).parent / "reports"
MODEL = "claude-sonnet-4-6"
CONCURRENCY = 8
BATCH_DELAY_RANGE = (1.0, 2.0)  # seconds between batches

# Hints for finding an events page from the homepage.
# Split into two tiers so /events/ wins over /news/ when both exist on the page
# (homepages often list "News" before "Events" in nav, so the old first-match
# logic silently grabbed blog posts instead of the real events page).
EVENT_LINK_HINTS_PRIMARY = re.compile(
    r"event|fair|expo|seminar|webinar|workshop|activity|กิจกรรม|งาน|สัมมนา",
    re.IGNORECASE,
)
EVENT_LINK_HINTS_FALLBACK = re.compile(r"news", re.IGNORECASE)

EXTRACTION_SYSTEM_PROMPT = """You extract upcoming education event listings from agent websites for Australian universities.

Given the visible text of a page (and the organizer's company name), identify any UPCOMING events — education fairs, expos, seminars, webinars, workshops, info sessions. Skip generic services, blog posts, or past events.

Rules:
- Only extract events that are physically taking place in Thailand (Bangkok, Chiang Mai, Phuket, or other Thai cities) OR online events hosted by Thai agents. Discard any events located outside Thailand.
- Return events ONLY if there is an explicit date that you can normalise to ISO 8601 (YYYY-MM-DD).
- If only a year is given, skip the event.
- Times should be local time strings like "14:00" or "2:00 PM - 4:00 PM". Leave empty if unknown.
- Location is the city/venue/country. For online events, use "Online".
- registration_url should be an absolute URL to register or get more info. Leave empty if none.
- The organizer is the agent company whose website you are reading — use the name provided.
- If no upcoming events are present on the page, return an empty list.

Be precise. Do not fabricate dates."""

# Known non-Thai cities — events with any of these in the location field are dropped.
# Matched case-insensitively as whole-ish substrings (word-boundary on each side).
NON_THAI_CITIES = {
    # Australia
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
    "gold coast", "darwin", "hobart", "newcastle", "wollongong",
    # United Kingdom
    "london", "manchester", "edinburgh", "oxford", "cambridge", "birmingham",
    "glasgow", "liverpool", "bristol", "leeds", "sheffield", "nottingham",
    # United States
    "new york", "boston", "san francisco", "chicago", "los angeles",
    "seattle", "washington", "philadelphia", "houston", "atlanta",
    # Canada
    "toronto", "vancouver", "montreal", "ottawa", "calgary",
    # India
    "mumbai", "delhi", "bangalore", "bengaluru", "chennai", "kolkata",
    "hyderabad", "pune", "ahmedabad", "jaipur", "chandigarh", "lucknow",
    "kochi", "nagpur", "indore", "surat",
    # Other Asia
    "singapore", "hong kong", "kuala lumpur", "penang", "johor",
    "jakarta", "bali", "surabaya", "manila", "cebu",
    "ho chi minh", "hanoi", "da nang", "saigon",
    "phnom penh", "siem reap", "vientiane", "yangon",
    "tokyo", "osaka", "kyoto", "seoul", "busan",
    "beijing", "shanghai", "shenzhen", "guangzhou", "taipei", "kaohsiung",
    "colombo", "kathmandu", "dhaka", "karachi", "lahore", "islamabad",
    # Middle East
    "dubai", "abu dhabi", "doha", "riyadh", "jeddah", "kuwait city", "muscat",
    # Africa
    "cape town", "johannesburg", "durban", "pretoria", "nairobi",
    "lagos", "cairo", "accra",
    # NZ
    "auckland", "wellington", "christchurch",
    # Europe
    "dublin", "berlin", "munich", "frankfurt", "hamburg",
    "paris", "lyon", "marseille",
    "amsterdam", "rotterdam", "the hague",
    "madrid", "barcelona", "rome", "milan", "florence",
    "vienna", "zurich", "geneva", "stockholm", "copenhagen", "oslo", "helsinki",
}

_NON_THAI_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in NON_THAI_CITIES) + r")\b",
    re.IGNORECASE,
)


def is_outside_thailand(location: str) -> bool:
    """Return True if the location explicitly names a non-Thai city."""
    if not location:
        return False
    return bool(_NON_THAI_RE.search(location))

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "date": {"type": "string", "description": "ISO 8601 date (YYYY-MM-DD)"},
                    "time": {"type": "string"},
                    "location": {"type": "string"},
                    "organizer": {"type": "string"},
                    "registration_url": {"type": "string"},
                },
                "required": ["name", "date", "time", "location", "organizer", "registration_url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


def init_events_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id          INTEGER REFERENCES agents(id),
            name              TEXT NOT NULL,
            date              TEXT NOT NULL,
            time              TEXT,
            location          TEXT,
            organizer         TEXT,
            registration_url  TEXT,
            scraped_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(agent_id, name, date)
        );
        CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
        CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);

        CREATE TABLE IF NOT EXISTS scrape_failures (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    INTEGER REFERENCES agents(id),
            company_name TEXT,
            website     TEXT,
            error_kind  TEXT,
            error       TEXT,
            scraped_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_failures_kind ON scrape_failures(error_kind);
    """)
    conn.commit()


def classify_error(msg: str) -> str:
    m = msg.lower()
    if "anthropic" in m:
        return "anthropic"
    if "err_cert" in m or "ssl" in m or "certificate" in m:
        return "ssl"
    if "err_name_not_resolved" in m or "dns" in m:
        return "dns"
    if "timeout" in m or "timed out" in m:
        return "timeout"
    if "err_connection" in m or "refused" in m or "reset" in m:
        return "connection"
    if "navigation failed" in m:
        return "navigation"
    return "other"


def record_failure(conn: sqlite3.Connection, agent: sqlite3.Row, error: str) -> None:
    try:
        conn.execute(
            """INSERT INTO scrape_failures
               (agent_id, company_name, website, error_kind, error)
               VALUES (?,?,?,?,?)""",
            (agent["id"], agent["company_name"], agent["website"], classify_error(error), error),
        )
    except sqlite3.Error as e:
        print(f"    failure-log DB error: {e}", file=sys.stderr)


async def fetch_page_text(page, url: str) -> tuple[str, str]:
    """Load url, follow an events-page link if one is obvious, return (final_url, text)."""
    from playwright.async_api import TimeoutError as PWTimeout

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except PWTimeout:
        pass
    except Exception as e:
        raise RuntimeError(f"navigation failed: {e}")

    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except PWTimeout:
        pass

    # Try to find an events-page link on the homepage.
    # Two-pass scan: prefer "events/fair/seminar/..." over "news" so we don't
    # land on a blog when the site also has a dedicated events page.
    events_url = None
    try:
        anchors = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => ({href: a.href, text: (a.innerText || '').trim()}))",
        )
        current_host = urlparse(page.url).netloc
        primary_match = None
        fallback_match = None
        for a in anchors:
            text = a.get("text", "") or ""
            href = a.get("href", "") or ""
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                # Skip non-navigational links (in-page anchors like "#" dropdown
                # triggers, which otherwise match the regex via their menu text
                # and short-circuit the search before we reach the real events link).
                continue
            if urlparse(href).netloc and urlparse(href).netloc != current_host:
                continue
            haystack = f"{text} {href}"
            if primary_match is None and EVENT_LINK_HINTS_PRIMARY.search(haystack):
                primary_match = href
            elif fallback_match is None and EVENT_LINK_HINTS_FALLBACK.search(haystack):
                fallback_match = href
            if primary_match:
                break  # stop on first primary hit
        chosen = primary_match or fallback_match
        if chosen:
            events_url = urljoin(page.url, chosen)
    except Exception:
        pass

    if events_url and events_url != page.url:
        try:
            await page.goto(events_url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_load_state("networkidle", timeout=6_000)
        except Exception:
            pass

    try:
        text = await page.evaluate("() => document.body.innerText")
    except Exception:
        text = ""

    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return page.url, text


async def extract_events(
    client: anthropic.AsyncAnthropic, company_name: str, source_url: str, text: str
) -> list[dict]:
    """Call Claude to extract structured event records from the page text."""
    if not text or len(text) < 80:
        return []

    text = text[:12000]
    user_content = (
        f"Organizer: {company_name}\n"
        f"Source URL: {source_url}\n"
        f"Today's date: {datetime.now().date().isoformat()}\n\n"
        f"Page content:\n---\n{text}\n---"
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": EXTRACTION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": EVENT_SCHEMA}},
        messages=[{"role": "user", "content": user_content}],
    )

    text_block = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text_block)
    except json.JSONDecodeError:
        return []
    return data.get("events", [])


# Locale-code top-level paths that mirror the root content (different language,
# same events). These collapse to the site root so /en/ and /th/ aren't
# scraped as separate canonical URLs.
LANG_PREFIXES = {"/en", "/th", "/cn", "/jp", "/ja", "/zh", "/de", "/fr", "/es", "/vi", "/ko"}


def canonical_url_key(url: str) -> tuple[str, str]:
    """Return (hostname_without_www, path) for URL deduplication.
    Treats www.X / https://X/ / http://X / X (no scheme) as the same site,
    treats /en, /th, etc. as equivalent to root, but keeps real branch paths
    (/branches/bangkok vs /branches/chiang-mai) separate."""
    u = (url or "").strip()
    if not u:
        return ("", "")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        parsed = urlparse(u)
    except Exception:
        return (u.lower(), "")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/").lower() or "/"
    # Strip a leading language-code segment so /en, /en/something, /th/... fold to root
    for lp in LANG_PREFIXES:
        if path == lp or path.startswith(lp + "/"):
            path = path[len(lp):] or "/"
            break
    return (host, path)


def within_next_30_days(date_str: str) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    today = datetime.now().date()
    return today <= d <= today + timedelta(days=30)


def upsert_event(conn: sqlite3.Connection, agent_id: int, ev: dict) -> bool:
    try:
        conn.execute(
            """INSERT INTO events
               (agent_id, name, date, time, location, organizer, registration_url)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(agent_id, name, date) DO UPDATE SET
                 time=excluded.time,
                 location=excluded.location,
                 organizer=excluded.organizer,
                 registration_url=excluded.registration_url,
                 scraped_at=CURRENT_TIMESTAMP""",
            (
                agent_id,
                ev.get("name", "").strip(),
                ev.get("date", "").strip(),
                (ev.get("time") or "").strip(),
                (ev.get("location") or "").strip(),
                (ev.get("organizer") or "").strip(),
                (ev.get("registration_url") or "").strip(),
            ),
        )
        return True
    except sqlite3.Error as e:
        print(f"    DB error: {e}", file=sys.stderr)
        return False


async def process_agent(ctx, client: anthropic.AsyncAnthropic, agent: sqlite3.Row) -> tuple[sqlite3.Row, list[dict], str | None]:
    """Scrape one site and extract events. Returns (agent, events, error)."""
    page = await ctx.new_page()
    try:
        final_url, text = await fetch_page_text(page, agent["website"])
        events = await extract_events(client, agent["company_name"], final_url, text)
        return agent, events, None
    except anthropic.APIStatusError as e:
        return agent, [], f"Anthropic {e.status_code}: {e.message}"
    except Exception as e:
        return agent, [], str(e)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def scrape_async(limit: int | None, refresh: bool) -> None:
    from playwright.async_api import async_playwright

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_events_table(conn)

    if refresh:
        cutoff = (datetime.now() - timedelta(days=30)).date().isoformat()
        conn.execute("DELETE FROM events WHERE date < ?", (cutoff,))
        conn.commit()

    # Fresh failure log each run so the table reflects current state
    conn.execute("DELETE FROM scrape_failures")
    conn.commit()

    # Pull every Thailand agent row, then collapse by canonical URL key
    # (hostname without www + path). This catches the case where the same
    # site is listed under multiple URL variants across universities
    # (e.g. www.X.com vs https://X.com/ vs http://X.com).
    all_rows = conn.execute(
        """SELECT id, company_name, website FROM agents
           WHERE country LIKE '%Thailand%'
             AND website IS NOT NULL AND TRIM(website) != ''
           ORDER BY id"""
    ).fetchall()
    canonical: dict[tuple[str, str], sqlite3.Row] = {}
    for r in all_rows:
        key = canonical_url_key(r["website"])
        if not key[0]:
            continue  # skip malformed URLs
        if key not in canonical:
            canonical[key] = r  # keep the lowest-id row as the representative
    rows = sorted(canonical.values(), key=lambda r: r["id"])
    if limit:
        rows = rows[:limit]
    print(
        f"Scanning {len(rows)} unique Thailand agent websites "
        f"(deduped from {len(all_rows)} agent rows, concurrency={CONCURRENCY}) …",
        flush=True,
    )

    # Purge events from non-canonical Hands On / IDP / etc. agent_ids that were
    # scraped under different URL variants in earlier runs. Without this, stale
    # events accumulate as orphans whenever the canonical-URL dedup picks a
    # different representative across runs. Only delete events tied to TH agents
    # we considered for scraping — leave non-Thailand data alone.
    canonical_ids = [r["id"] for r in rows]
    all_th_ids = [r["id"] for r in all_rows]
    orphan_ids = [i for i in all_th_ids if i not in set(canonical_ids)]
    if orphan_ids:
        placeholders = ",".join("?" for _ in orphan_ids)
        cur = conn.execute(
            f"DELETE FROM events WHERE agent_id IN ({placeholders})",
            orphan_ids,
        )
        conn.commit()
        if cur.rowcount:
            print(
                f"Purged {cur.rowcount} stale event(s) from "
                f"{len(orphan_ids)} non-canonical Thailand agent_ids.",
                flush=True,
            )

    # max_retries=5 (default is 2) so transient 529 "overloaded" responses
    # don't drop sites during Anthropic load spikes. SDK uses exponential backoff.
    client = anthropic.AsyncAnthropic(max_retries=5)
    total_found = total_kept = sites_with_events = total_failed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        # Process in batches of CONCURRENCY, with a small random delay between batches
        for batch_start in range(0, len(rows), CONCURRENCY):
            batch = rows[batch_start : batch_start + CONCURRENCY]
            batch_num = batch_start // CONCURRENCY + 1
            total_batches = (len(rows) + CONCURRENCY - 1) // CONCURRENCY
            print(
                f"\n— Batch {batch_num}/{total_batches} ({len(batch)} sites) —",
                flush=True,
            )

            results = await asyncio.gather(
                *(process_agent(ctx, client, a) for a in batch)
            )

            for agent, events, error in results:
                label = f"  {str(agent['company_name'])[:50]} — {agent['website']}"
                if error:
                    record_failure(conn, agent, error)
                    total_failed += 1
                    print(f"{label}\n    skip [{classify_error(error)}]: {error}", file=sys.stderr, flush=True)
                    continue
                total_found += len(events)
                # Filter Claude's output to events we'd actually keep
                keep = [
                    ev for ev in events
                    if within_next_30_days(ev.get("date", ""))
                    and not is_outside_thailand(ev.get("location", ""))
                ]
                # If this site produced any events, replace its stored events
                # atomically so stale name-variants from previous runs are dropped.
                if keep:
                    conn.execute("DELETE FROM events WHERE agent_id = ?", (agent["id"],))
                kept = 0
                for ev in keep:
                    if upsert_event(conn, agent["id"], ev):
                        kept += 1
                if kept:
                    sites_with_events += 1
                    print(f"{label}\n    {kept} upcoming event(s) saved", flush=True)
                else:
                    print(label, flush=True)
                total_kept += kept
            conn.commit()

            # Pacing delay between batches (skip after last batch)
            if batch_start + CONCURRENCY < len(rows):
                await asyncio.sleep(random.uniform(*BATCH_DELAY_RANGE))

        await browser.close()

    print(
        f"\nDone. Sites with events: {sites_with_events}. "
        f"Events found: {total_found}, kept (≤30 days): {total_kept}. "
        f"Failed sites: {total_failed} (see scrape_failures table).",
        flush=True,
    )

    if total_failed:
        breakdown = conn.execute(
            "SELECT error_kind, COUNT(*) AS n FROM scrape_failures GROUP BY error_kind ORDER BY n DESC"
        ).fetchall()
        print("  Failure breakdown:", ", ".join(f"{r['error_kind']}={r['n']}" for r in breakdown), flush=True)


def generate_report() -> Path:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_events_table(conn)

    today = datetime.now().date()
    cutoff = today + timedelta(days=30)
    rows = conn.execute(
        """SELECT e.*, a.company_name AS agent_name, a.website AS agent_website
           FROM events e JOIN agents a ON e.agent_id = a.id
           WHERE e.date BETWEEN ? AND ?
           ORDER BY e.date, e.time""",
        (today.isoformat(), cutoff.isoformat()),
    ).fetchall()

    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"events_thailand_{today.isoformat()}.html"

    def esc(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    items = []
    for r in rows:
        reg = r["registration_url"] or ""
        reg_html = (
            f'<a href="{esc(reg)}" target="_blank" rel="noopener">Register</a>'
            if reg
            else "—"
        )
        items.append(
            f"""<tr>
              <td class="date">{esc(r['date'])}</td>
              <td>{esc(r['time'] or '')}</td>
              <td><strong>{esc(r['name'])}</strong></td>
              <td>{esc(r['location'] or '')}</td>
              <td>{esc(r['organizer'] or r['agent_name'])}<br>
                  <small><a href="{esc(r['agent_website'] or '')}" target="_blank" rel="noopener">{esc(r['agent_website'] or '')}</a></small></td>
              <td>{reg_html}</td>
            </tr>"""
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Thailand Agent Events — Next 30 Days</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
          max-width: 1200px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ margin-bottom: 0.25rem; }}
  .meta {{ color: #666; margin-bottom: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 0.6rem 0.8rem; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }}
  th {{ background: #f7f7f9; font-weight: 600; }}
  td.date {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  tr:hover td {{ background: #fafbff; }}
  small {{ color: #777; }}
  .empty {{ padding: 2rem; text-align: center; color: #777; }}
</style>
</head>
<body>
  <h1>Thailand Agent Events</h1>
  <p class="meta">{len(rows)} upcoming event(s) between {today.isoformat()} and {cutoff.isoformat()}.</p>
  {"<table><thead><tr><th>Date</th><th>Time</th><th>Event</th><th>Location</th><th>Organizer</th><th>Register</th></tr></thead><tbody>" + "".join(items) + "</tbody></table>" if items else '<div class="empty">No upcoming events found.</div>'}
</body>
</html>"""

    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="Limit number of agents to scan")
    ap.add_argument("--refresh", action="store_true", help="Clear stale (past) events first")
    ap.add_argument("--report", action="store_true", help="Generate HTML report and exit")
    args = ap.parse_args()

    if args.report:
        path = generate_report()
        print(f"Report written to {path}")
        return 0

    asyncio.run(scrape_async(limit=args.limit, refresh=args.refresh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
