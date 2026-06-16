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
ARCHIVE_RETENTION_YEARS = 2  # keep past events this long for the searchable archive

# Hints for finding an events page from the homepage.
# Split into two tiers so /events/ wins over /news/ when both exist on the page
# (homepages often list "News" before "Events" in nav, so the old first-match
# logic silently grabbed blog posts instead of the real events page).
EVENT_LINK_HINTS_PRIMARY = re.compile(
    r"event|fair|expo|seminar|webinar|workshop|activity|กิจกรรม|งาน|สัมมนา",
    re.IGNORECASE,
)
EVENT_LINK_HINTS_FALLBACK = re.compile(r"news", re.IGNORECASE)

# ─── Per-country config ────────────────────────────────────────────────────
# Adding a market = one entry here. `home_cities` are the in-country cities we
# KEEP; they are subtracted from the shared FOREIGN_CITIES master so the same
# master correctly excludes other markets' hubs for each country.
COUNTRY_CONFIG = {
    "thailand": {
        "db_match": "%Thailand%",
        "name": "Thailand",
        "adjective": "Thai",
        "prompt_cities": "Bangkok, Chiang Mai, Phuket, or other Thai cities",
        "home_cities": {
            "bangkok", "chiang mai", "phuket", "pattaya", "hat yai",
            "khon kaen", "nonthaburi", "krabi", "ayutthaya",
        },
    },
    "vietnam": {
        "db_match": "%Vietnam%",
        "name": "Vietnam",
        "adjective": "Vietnamese",
        "prompt_cities": "Hanoi, Ho Chi Minh City, Da Nang, or other Vietnamese cities",
        "home_cities": {
            "hanoi", "ha noi", "ho chi minh", "ho chi minh city", "hcmc",
            "saigon", "da nang", "hai phong", "can tho", "nha trang",
            "hue", "bien hoa",
        },
    },
    "taiwan": {
        "db_match": "%Taiwan%",
        "name": "Taiwan",
        "adjective": "Taiwanese",
        "prompt_cities": "Taipei, Kaohsiung, Taichung, Tainan, or other Taiwanese cities",
        "home_cities": {
            "taipei", "new taipei", "kaohsiung", "taichung", "tainan",
            "hsinchu", "taoyuan", "keelung", "chiayi", "changhua",
        },
    },
    "hongkong": {
        "db_match": "%Hong Kong%",
        "name": "Hong Kong",
        "adjective": "Hong Kong",
        "prompt_cities": "Hong Kong Island, Kowloon, the New Territories, or other Hong Kong districts",
        "home_cities": {
            "hong kong", "kowloon", "new territories", "tsim sha tsui",
            "mong kok", "causeway bay", "sha tin", "kwun tong", "wan chai",
        },
    },
    "indonesia": {
        "db_match": "%Indonesia%",
        "name": "Indonesia",
        "adjective": "Indonesian",
        "prompt_cities": "Jakarta, Surabaya, Bandung, Medan, or other Indonesian cities",
        "home_cities": {
            "jakarta", "surabaya", "bandung", "medan", "semarang", "bekasi",
            "depok", "tangerang", "yogyakarta", "jogja", "makassar", "bali",
            "denpasar", "palembang", "bsd",
        },
    },
    "malaysia": {
        "db_match": "%Malaysia%",
        "name": "Malaysia",
        "adjective": "Malaysian",
        "prompt_cities": "Kuala Lumpur, Penang, Johor Bahru, Petaling Jaya, or other Malaysian cities",
        "home_cities": {
            "kuala lumpur", "petaling jaya", "johor bahru", "johor", "penang",
            "george town", "georgetown", "ipoh", "shah alam", "subang jaya",
            "kuching", "kota kinabalu", "malacca", "melaka", "putrajaya", "cyberjaya",
        },
    },
    "ghana": {
        "db_match": "%Ghana%",
        "name": "Ghana",
        "adjective": "Ghanaian",
        "prompt_cities": "Accra, Kumasi, Tamale, Takoradi, or other Ghanaian cities",
        "home_cities": {
            "accra", "kumasi", "tamale", "takoradi", "tema", "cape coast",
            "ho", "koforidua", "sunyani", "east legon",
        },
    },
    "nigeria": {
        "db_match": "%Nigeria%",
        "name": "Nigeria",
        "adjective": "Nigerian",
        "prompt_cities": "Lagos, Abuja, Ibadan, Port Harcourt, or other Nigerian cities",
        "home_cities": {
            "lagos", "abuja", "ibadan", "kano", "port harcourt", "benin city",
            "kaduna", "enugu", "ilorin", "jos", "abeokuta", "owerri", "uyo",
            "calabar", "lekki", "ikeja",
        },
    },
    "singapore": {
        "db_match": "%Singapore%",
        "name": "Singapore",
        "adjective": "Singaporean",
        "prompt_cities": "Singapore (any district — Orchard, Jurong, Tampines, etc.)",
        "home_cities": {
            "singapore", "orchard", "jurong", "tampines", "woodlands",
            "sentosa", "novena", "bugis", "clementi", "one-north",
        },
    },
    "cambodia": {
        "db_match": "%Cambodia%",
        "name": "Cambodia",
        "adjective": "Cambodian",
        "prompt_cities": "Phnom Penh, Siem Reap, Battambang, or other Cambodian cities",
        "home_cities": {
            "phnom penh", "siem reap", "sihanoukville", "battambang",
            "kampong cham", "kandal", "poipet", "kep", "kampot",
        },
    },
    "india": {
        "db_match": "%India%",
        "name": "India",
        "adjective": "Indian",
        "prompt_cities": "Mumbai, Delhi, Bangalore, Chennai, Hyderabad, or other Indian cities",
        "home_cities": {
            "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "chennai",
            "kolkata", "hyderabad", "pune", "ahmedabad", "jaipur", "chandigarh",
            "lucknow", "kochi", "cochin", "nagpur", "indore", "surat", "gurgaon",
            "gurugram", "noida", "coimbatore", "vadodara", "vijayawada",
            "thiruvananthapuram", "trivandrum", "bhopal", "patna", "kanpur",
            "nashik", "ludhiana", "amritsar", "mangalore", "mysore", "visakhapatnam",
        },
    },
    "nepal": {
        "db_match": "%Nepal%",
        "name": "Nepal",
        "adjective": "Nepali",
        "prompt_cities": "Kathmandu, Pokhara, Lalitpur, Biratnagar, or other Nepali cities",
        "home_cities": {
            "kathmandu", "pokhara", "lalitpur", "patan", "biratnagar", "butwal",
            "chitwan", "bharatpur", "birgunj", "dharan", "nepalgunj", "itahari",
        },
    },
    "japan": {
        "db_match": "%Japan%",
        "name": "Japan",
        "adjective": "Japanese",
        "prompt_cities": "Tokyo, Osaka, Kyoto, Yokohama, Nagoya, or other Japanese cities",
        "home_cities": {
            "tokyo", "osaka", "kyoto", "yokohama", "nagoya", "sapporo", "fukuoka",
            "kobe", "sendai", "hiroshima", "saitama", "chiba", "kawasaki",
        },
    },
    "korea": {
        "db_match": "%Korea%",
        "name": "South Korea",
        "adjective": "Korean",
        "prompt_cities": "Seoul, Busan, Incheon, Daegu, Daejeon, or other Korean cities",
        "home_cities": {
            "seoul", "busan", "incheon", "daegu", "daejeon", "gwangju", "suwon",
            "ulsan", "jeju", "seongnam", "goyang", "yongin",
        },
    },
    "srilanka": {
        "db_match": "%Sri Lanka%",
        "name": "Sri Lanka",
        "adjective": "Sri Lankan",
        "prompt_cities": "Colombo, Kandy, Galle, Jaffna, or other Sri Lankan cities",
        "home_cities": {
            "colombo", "kandy", "galle", "jaffna", "negombo", "kurunegala",
            "batticaloa", "anuradhapura", "ratnapura", "matara", "gampaha",
            "nugegoda", "dehiwala", "moratuwa",
        },
    },
}

EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """You extract upcoming education event listings from agent websites for international universities.

Given the visible text of a page (and the organizer's company name), identify any UPCOMING events — education fairs, expos, seminars, webinars, workshops, info sessions. Skip generic services, blog posts, or past events.

Rules:
- Only extract events that are physically taking place in {country_name} ({prompt_cities}) OR online events hosted by {adjective} agents. Discard any events located outside {country_name}.
- Return events ONLY if there is an explicit date that you can normalise to ISO 8601 (YYYY-MM-DD).
- If only a year is given, skip the event.
- Times should be local time strings like "14:00" or "2:00 PM - 4:00 PM". Leave empty if unknown.
- Location is the city/venue/country. For online events, use "Online".
- registration_url should be an absolute URL to register or get more info. Leave empty if none.
- The organizer is the agent company whose website you are reading — use the name provided.
- If no upcoming events are present on the page, return an empty list.

Be precise. Do not fabricate dates."""


def build_extraction_prompt(country: str) -> str:
    cfg = COUNTRY_CONFIG[country]
    return EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(
        country_name=cfg["name"],
        prompt_cities=cfg["prompt_cities"],
        adjective=cfg["adjective"],
    )


# Master set of foreign event locations: study-abroad destinations + every
# regional hub (including the home markets' own cities). For a given market we
# exclude this whole set MINUS that market's home_cities, so an event in
# another country's hub is dropped while the home country's cities are kept.
# Matched case-insensitively as whole-ish substrings (word-boundary on each side).
FOREIGN_CITIES = {
    # Home-market hubs (kept for their own market via home_cities subtraction,
    # excluded for every other market)
    "bangkok", "chiang mai", "phuket", "pattaya", "hat yai", "khon kaen",
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
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "chennai", "kolkata",
    "hyderabad", "pune", "ahmedabad", "jaipur", "chandigarh", "lucknow",
    "kochi", "cochin", "nagpur", "indore", "surat", "gurgaon", "gurugram",
    "noida", "coimbatore", "vadodara", "vijayawada", "thiruvananthapuram",
    "trivandrum", "bhopal", "patna", "kanpur", "nashik", "ludhiana", "amritsar",
    "mangalore", "mysore", "visakhapatnam",
    # Other Asia
    "singapore", "hong kong", "kowloon", "kuala lumpur", "penang", "johor",
    "petaling jaya", "johor bahru", "george town", "ipoh", "shah alam",
    "kuching", "kota kinabalu", "malacca", "melaka", "putrajaya", "cyberjaya",
    "jakarta", "bali", "surabaya", "bandung", "medan", "semarang", "yogyakarta",
    "makassar", "denpasar", "palembang", "manila", "cebu",
    "ho chi minh", "hanoi", "da nang", "saigon",
    "phnom penh", "siem reap", "sihanoukville", "battambang", "vientiane", "yangon",
    "tokyo", "osaka", "kyoto", "yokohama", "nagoya", "sapporo", "fukuoka", "kobe",
    "seoul", "busan", "incheon", "daegu", "daejeon", "gwangju",
    "beijing", "shanghai", "shenzhen", "guangzhou", "taipei", "kaohsiung",
    "taichung", "tainan", "hsinchu", "taoyuan",
    "colombo", "kandy", "galle", "jaffna", "negombo", "kurunegala",
    "kathmandu", "pokhara", "lalitpur", "biratnagar", "butwal", "bharatpur",
    "dhaka", "karachi", "lahore", "islamabad",
    # Middle East
    "dubai", "abu dhabi", "doha", "riyadh", "jeddah", "kuwait city", "muscat",
    # Africa
    "cape town", "johannesburg", "durban", "pretoria", "nairobi",
    "lagos", "cairo", "accra", "kumasi", "tamale", "takoradi", "tema",
    "abuja", "ibadan", "kano", "port harcourt", "benin city", "kaduna",
    "enugu", "ilorin", "abeokuta", "owerri", "calabar", "lekki", "ikeja",
    # NZ
    "auckland", "wellington", "christchurch",
    # Europe
    "dublin", "berlin", "munich", "frankfurt", "hamburg",
    "paris", "lyon", "marseille",
    "amsterdam", "rotterdam", "the hague",
    "madrid", "barcelona", "rome", "milan", "florence",
    "vienna", "zurich", "geneva", "stockholm", "copenhagen", "oslo", "helsinki",
}

# Compiled exclusion regex per country, cached. Built from FOREIGN_CITIES minus
# the country's own home_cities.
_EXCLUSION_RE: dict[str, "re.Pattern"] = {}


def _exclusion_re(country: str) -> "re.Pattern":
    rx = _EXCLUSION_RE.get(country)
    if rx is None:
        cities = FOREIGN_CITIES - COUNTRY_CONFIG[country]["home_cities"]
        rx = re.compile(
            r"\b(" + "|".join(re.escape(c) for c in sorted(cities, key=len, reverse=True)) + r")\b",
            re.IGNORECASE,
        )
        _EXCLUSION_RE[country] = rx
    return rx


def is_outside_country(location: str, country: str) -> bool:
    """Return True if the location explicitly names a city outside `country`."""
    if not location:
        return False
    return bool(_exclusion_re(country).search(location))

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
    client: anthropic.AsyncAnthropic, company_name: str, source_url: str, text: str,
    system_prompt: str = EXTRACTION_SYSTEM_PROMPT_TEMPLATE,
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
                "text": system_prompt,
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


async def process_agent(ctx, client: anthropic.AsyncAnthropic, agent: sqlite3.Row,
                        system_prompt: str) -> tuple[sqlite3.Row, list[dict], str | None]:
    """Scrape one site and extract events. Returns (agent, events, error)."""
    page = await ctx.new_page()
    try:
        final_url, text = await fetch_page_text(page, agent["website"])
        events = await extract_events(client, agent["company_name"], final_url, text, system_prompt)
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


async def scrape_async(limit: int | None, refresh: bool, companies: list[str] | None = None,
                       country: str = "thailand") -> None:
    from playwright.async_api import async_playwright

    cfg = COUNTRY_CONFIG[country]
    system_prompt = build_extraction_prompt(country)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_events_table(conn)

    if refresh:
        # Retain past events for ARCHIVE_RETENTION_YEARS so they populate the
        # searchable past-events archive; only prune what's older than that.
        today = datetime.now().date()
        try:
            cutoff = today.replace(year=today.year - ARCHIVE_RETENTION_YEARS)
        except ValueError:  # Feb 29 in a leap year → clamp to 28th
            cutoff = today.replace(year=today.year - ARCHIVE_RETENTION_YEARS, day=28)
        conn.execute("DELETE FROM events WHERE date < ?", (cutoff.isoformat(),))
        conn.commit()

    # Fresh failure log each run so the table reflects current state
    conn.execute("DELETE FROM scrape_failures")
    conn.commit()

    # Pull every in-country agent row, then collapse by canonical URL key
    # (hostname without www + path). This catches the case where the same
    # site is listed under multiple URL variants across universities
    # (e.g. www.X.com vs https://X.com/ vs http://X.com).
    company_clause, params = "", [cfg["db_match"]]
    if companies:
        ors = " OR ".join(["company_name LIKE ? OR canonical_name LIKE ?"] * len(companies))
        company_clause = f" AND ({ors})"
        for c in companies:
            params += [f"%{c}%", f"%{c}%"]
    all_rows = conn.execute(
        f"""SELECT id, company_name, website FROM agents
           WHERE country LIKE ?
             AND website IS NOT NULL AND TRIM(website) != ''
             {company_clause}
           ORDER BY id""", params
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
        f"Scanning {len(rows)} unique {cfg['name']} agent websites "
        f"(deduped from {len(all_rows)} agent rows, concurrency={CONCURRENCY}) …",
        flush=True,
    )

    # Purge events from non-canonical Hands On / IDP / etc. agent_ids that were
    # scraped under different URL variants in earlier runs. Without this, stale
    # events accumulate as orphans whenever the canonical-URL dedup picks a
    # different representative across runs. Only delete events tied to this
    # country's agents we considered for scraping — leave other markets alone.
    canonical_ids = [r["id"] for r in rows]
    all_country_ids = [r["id"] for r in all_rows]
    orphan_ids = [i for i in all_country_ids if i not in set(canonical_ids)]
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
                f"{len(orphan_ids)} non-canonical {cfg['name']} agent_ids.",
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
                *(process_agent(ctx, client, a, system_prompt) for a in batch)
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
                    and not is_outside_country(ev.get("location", ""), country)
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

    # Collapse duplicate events for the same company on the same date down to a
    # single row (keeping the earliest-scraped). Catches both multi-branch
    # listings (StudyIn Bangkok / Chiang Mai) and language variants of one event
    # scraped from a Thai + /en/ site (e.g. Hands On listing an open day twice,
    # once with a Thai subtitle, once English-only).
    # Group also by a.country so the same brand running an event on the same
    # date in two markets (e.g. an IDP info day in Bangkok and Hanoi) is not
    # collapsed across countries.
    deduped = conn.execute("""
        DELETE FROM events
        WHERE id NOT IN (
            SELECT MIN(e.id) FROM events e JOIN agents a ON e.agent_id = a.id
            GROUP BY a.canonical_name, e.date, a.country
        )""").rowcount
    conn.commit()
    if deduped:
        print(f"  deduped {deduped} cross-branch duplicate event(s)", flush=True)

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


def generate_report(country: str = "thailand") -> Path:
    cfg = COUNTRY_CONFIG[country]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_events_table(conn)

    today = datetime.now().date()
    cutoff = today + timedelta(days=30)
    rows = conn.execute(
        """SELECT e.*, a.company_name AS agent_name, a.website AS agent_website
           FROM events e JOIN agents a ON e.agent_id = a.id
           WHERE e.date BETWEEN ? AND ?
             AND a.country LIKE ?
           ORDER BY e.date, e.time""",
        (today.isoformat(), cutoff.isoformat(), cfg["db_match"]),
    ).fetchall()

    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"events_{country}_{today.isoformat()}.html"

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
    ap.add_argument("--company", nargs="+", help="Only scan agents matching these names (e.g. --company WIN StudyIn)")
    ap.add_argument("--country", default="thailand", choices=sorted(COUNTRY_CONFIG),
                    help="Market to scrape (default: thailand)")
    ap.add_argument("--all", action="store_true",
                    help="Scrape every market in COUNTRY_CONFIG (used by the weekly cron)")
    args = ap.parse_args()

    if args.report:
        path = generate_report(args.country)
        print(f"Report written to {path}")
        return 0

    countries = sorted(COUNTRY_CONFIG) if args.all else [args.country]
    for country in countries:
        if args.all:
            print(f"\n=== {COUNTRY_CONFIG[country]['name']} ===")
        asyncio.run(scrape_async(limit=args.limit, refresh=args.refresh,
                                 companies=args.company, country=country))
    return 0


if __name__ == "__main__":
    sys.exit(main())
