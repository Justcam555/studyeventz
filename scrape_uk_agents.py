#!/usr/bin/env python3
"""
scrape_uk_agents.py — Extract international recruitment agents from UK universities'
agent-directory pages and store the agent<->university relationships in agents.db.

Why LLM extraction
------------------
The agent pages found by find_agent_pages.py are wildly heterogeneous: some list
agents under country headings (De Montfort), some in tables of "Name www.site"
(Heriot-Watt), many are landing pages with no list at all, and a few are false
positives (an art exhibition called "Agents of Change"). A per-site regex parser
does not scale to 140 pages. Instead we hand each page's visible text to Claude
(claude-sonnet-4-6, per this project's convention) and ask for structured agent
records — which also naturally returns nothing for landing pages / false hits.

What it writes
--------------
For each agent found: a row in `agents` linked to the university
(university_id), with company_name, country (the agent's market), city, email,
website, source_url and a canonical_name for cross-university de-duplication.
UNIQUE(university_id, company_name, country) keeps re-runs idempotent.

Usage
-----
    python scrape_uk_agents.py --pilot --dry-run    # pilot set, print only (no DB writes)
    python scrape_uk_agents.py --pilot              # pilot set, write to DB
    python scrape_uk_agents.py --all                # every UK uni with an agent_page_url
    python scrape_uk_agents.py --hesa 10003154 ...  # specific universities by UKPRN
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "agents.db"
MODEL = "claude-sonnet-4-6"
MAX_TEXT_CHARS = 300000         # max page text retained (large directories chunked below)
CHUNK_SIZE = 24000              # per-model-call text size; pages above this are chunked
REQUEST_INTERVAL = 2.0

# Pilot set (UKPRNs) chosen to span formats: rich country-grouped list, table
# format, landing pages with no list, and a known false positive.
PILOT_HESA = [
    "10001883",  # De Montfort  — country-heading list (rich)
    "10007764",  # Heriot-Watt  — table format
    "10007759",  # Aston        — landing page
    "10007814",  # Cardiff      — country-selector landing
    "10001726",  # Coventry     — "find your region" landing
    "10007850",  # Bath         — sanity
    "10007761",  # Courtauld    — false positive (exhibition)
    "10000291",  # Anglia Ruskin— "agent toolkit" landing
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

EXTRACT_TOOL = {
    "name": "record_agents",
    "description": "Record the education recruitment agents / representatives that the university officially lists.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_agent_directory": {
                "type": "boolean",
                "description": "True only if this page actually lists named recruitment agents/representatives. False for generic info/landing pages, country selectors with no names, or unrelated pages.",
            },
            "agents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company_name": {"type": "string"},
                        "country": {"type": "string", "description": "The market/country the agent operates in, usually from the section heading. Empty if unknown."},
                        "city": {"type": "string"},
                        "email": {"type": "string"},
                        "website": {"type": "string"},
                    },
                    "required": ["company_name"],
                },
            },
        },
        "required": ["is_agent_directory", "agents"],
    },
}

SYSTEM_PROMPT = (
    "You extract the official education recruitment agents (a.k.a. representatives, "
    "counsellors, partners) that a UK university lists on its agent-directory page. "
    "Return ONLY real agency organisations that appear in the page's listing. "
    "Rules:\n"
    "- The `country` field is the market the agent serves (e.g. India, China, Nigeria), "
    "normally taken from the section heading the agent sits under.\n"
    "- Do NOT include the university itself, its own offices/staff, navigation links, "
    "social media, cookie/legal text, or generic advice paragraphs.\n"
    "- If the page is a generic 'how to apply via an agent' info page, a country selector "
    "with no named agencies, or unrelated (e.g. an event/exhibition), set "
    "is_agent_directory=false and return an empty agents list.\n"
    "- IMPORTANT: an agency listed with only a name and a website (no email) is STILL a "
    "valid agent. Many directories are tables or lists of 'Agency Name + website' grouped "
    "under country headings — extract EVERY such entry, not just ones with an email. These "
    "are agent listings, not navigation links.\n"
    "- Never invent agents or fields. Leave a field empty if it is not present."
)

_last = 0.0


def throttle():
    global _last
    w = REQUEST_INTERVAL - (time.monotonic() - _last)
    if w > 0:
        time.sleep(w)
    _last = time.monotonic()


def load_env():
    """Make ANTHROPIC_API_KEY available from .env if not already in the environment."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and "=" in line:
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                break


def clean_html(html: str) -> str:
    """Strip chrome/scripts and return capped visible text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text[:MAX_TEXT_CHARS]


def fetch_with_playwright(url: str):
    """Render a page in headless Chromium (for bot-blocked / JS pages)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "no-playwright"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page(user_agent=random.choice(USER_AGENTS))
            # domcontentloaded fires reliably; networkidle hangs on sites with
            # chat widgets / analytics that poll forever (caused the timeouts).
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(3500)
            html = page.content()
            b.close()
        return clean_html(html), "ok:playwright"
    except Exception as e:
        return None, f"pw-err:{type(e).__name__}"


def fetch_text(url: str):
    """Return (visible_text, status). Tries plain HTTP, falls back to a real
    browser when the site bot-blocks (403/406/429) or renders too little text."""
    throttle()
    try:
        r = requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)},
                         timeout=20, allow_redirects=True)
        if r.status_code == 200:
            text = clean_html(r.text)
            if len(text) >= 400:          # enough content — good enough
                return text, "ok:http"
            # too little text => probably JS-rendered; try the browser
        elif r.status_code not in (403, 406, 429, 503):
            return None, f"http:{r.status_code}"
    except requests.RequestException:
        pass  # fall through to browser
    return fetch_with_playwright(url)


def canonical(name: str) -> str:
    """Light canonical key for cross-university de-duplication."""
    n = name.lower()
    n = re.sub(r"\b(ltd|limited|pty|inc|llc|llp|co|gmbh|t/a|trading as|pvt|private)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _call_model(client, uni_name: str, url: str, text: str):
    msg = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "record_agents"},
        messages=[{
            "role": "user",
            "content": f"University: {uni_name}\nPage URL: {url}\n\n--- PAGE TEXT ---\n{text}",
        }],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "record_agents":
            data = block.input
            return bool(data.get("is_agent_directory")), data.get("agents", [])
    return False, []


def _chunk(text: str, size: int):
    """Split text into <= size pieces, preferring blank-line boundaries."""
    chunks, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            nl = text.rfind("\n\n", i, end)
            if nl > i + size // 2:
                end = nl
        chunks.append(text[i:end])
        i = end
    return chunks


def extract_agents(client, uni_name: str, url: str, text: str):
    """Call Claude to extract agent records. Returns (is_dir, [agents]).

    Large directories are chunked so a single model call's output can't truncate.
    Retries once when a (single-call) page claims to be a directory but yields
    nothing — catches under-extraction on table / name+website formats.
    """
    if len(text) <= CHUNK_SIZE:
        is_dir, agents = _call_model(client, uni_name, url, text)
        if is_dir and not agents:
            is_dir, agents = _call_model(client, uni_name, url, text)
        return is_dir, agents

    any_dir, all_agents = False, []
    for part in _chunk(text, CHUNK_SIZE):
        d, ags = _call_model(client, uni_name, url, part)
        any_dir = any_dir or d
        all_agents.extend(ags)
    return any_dir, all_agents


def select_universities(conn, args):
    cur = conn.cursor()
    cols = "SELECT id, name, hesa_id, agent_page_url FROM universities"
    if args.uni_id:  # any university by id, any country
        qs = ",".join("?" * len(args.uni_id))
        return cur.execute(cols + f" WHERE id IN ({qs}) AND agent_page_url IS NOT NULL", args.uni_id).fetchall()
    base = cols + " WHERE country='United Kingdom' AND agent_page_url IS NOT NULL"
    if args.hesa:
        qs = ",".join("?" * len(args.hesa))
        rows = cur.execute(base + f" AND hesa_id IN ({qs})", args.hesa).fetchall()
    elif args.pilot:
        qs = ",".join("?" * len(PILOT_HESA))
        rows = cur.execute(base + f" AND hesa_id IN ({qs})", PILOT_HESA).fetchall()
    else:  # --all
        rows = cur.execute(base + " ORDER BY name").fetchall()
    return rows


def main():
    ap = argparse.ArgumentParser(description="Scrape UK university agent directories into agents.db.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pilot", action="store_true", help="run the curated pilot set")
    g.add_argument("--all", action="store_true", help="run every UK uni with an agent_page_url")
    g.add_argument("--hesa", nargs="+", help="specific UK universities by UKPRN")
    g.add_argument("--uni-id", nargs="+", help="any universities by DB id (any country)")
    ap.add_argument("--replace", action="store_true", help="delete a university's existing agents before re-inserting")
    ap.add_argument("--dry-run", action="store_true", help="print extracted agents, do NOT write to the DB")
    args = ap.parse_args()

    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (env or .env).")
    import anthropic
    client = anthropic.Anthropic()

    conn = sqlite3.connect(DB_PATH)
    unis = select_universities(conn, args)
    mode = "DRY-RUN (no DB writes)" if args.dry_run else "WRITING to DB"
    print(f"{mode} | model={MODEL} | {len(unis)} universities\n")

    grand = 0
    for uid, name, hesa, url in unis:
        text, status = fetch_text(url)
        if not text:
            print(f"■ {name[:34]:34} fetch {status} — skipped\n")
            continue
        try:
            is_dir, agents = extract_agents(client, name, url, text)
        except Exception as e:
            print(f"■ {name[:34]:34} extract ERR: {repr(e)[:80]}\n")
            continue

        # Clean + dedupe within this university
        seen, clean = set(), []
        for a in agents:
            cn = (a.get("company_name") or "").strip()
            if not cn:
                continue
            key = (canonical(cn), (a.get("country") or "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            clean.append(a)

        flag = "" if is_dir else "  (model: NOT an agent directory)"
        print(f"● {name[:34]:34} {len(clean):3} agents{flag}")
        for a in clean[:6]:
            print(f"     - {a.get('company_name','')[:34]:34} {a.get('country','')[:14]:14} {a.get('email','') or a.get('website','')}")
        if len(clean) > 6:
            print(f"     ... +{len(clean)-6} more")
        print()
        grand += len(clean)

        if not args.dry_run:
            cur = conn.cursor()
            if args.replace:
                cur.execute("DELETE FROM agents WHERE university_id=?", (uid,))
            n_ins = 0
            for a in clean:
                cur.execute("""INSERT OR IGNORE INTO agents
                    (university_id, company_name, country, city, email, website,
                     source_url, canonical_name, scraped_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (uid, a.get("company_name"), a.get("country") or None,
                     a.get("city") or None, a.get("email") or None, a.get("website") or None,
                     url, canonical(a.get("company_name", "")), datetime.utcnow().isoformat()))
                n_ins += cur.rowcount
            cur.execute("UPDATE universities SET scrape_status=?, last_scraped=? WHERE id=?",
                        (f"ok:uk_llm ({n_ins})", datetime.utcnow().isoformat(), uid))
            cur.execute("""INSERT INTO scrape_log (university_id, status, agents_found, method, notes)
                           VALUES (?,?,?,?,?)""",
                        (uid, "ok" if is_dir else "no_directory", n_ins, "uk_llm", url))
            conn.commit()

    print(f"{'Would extract' if args.dry_run else 'Extracted'} {grand} agent listings across {len(unis)} universities.")
    conn.close()


if __name__ == "__main__":
    main()
