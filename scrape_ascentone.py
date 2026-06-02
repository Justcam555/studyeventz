#!/usr/bin/env python3
"""
AscentOne EAP Agent Scraper
Discovers and scrapes agent data from universities using the AscentOne
Easy Agent Publisher platform at eap.ascentone.com/[shortcode]

Usage:
    python3 scrape_ascentone.py              # Scrape all known shortcodes
    python3 scrape_ascentone.py --list       # List what was found/scraped
"""

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "data" / "agents.db"
BASE_URL = "https://eap.ascentone.com"
HANDLER  = f"{BASE_URL}/PageHandlers/AgentPublisherV5.ashx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

# Confirmed shortcodes + names (name used to match/insert universities table)
CONFIRMED = {
    "mq":            "Macquarie University",
    "canberra":      "University of Canberra",
    "westernsydney": "Western Sydney University",
    "cdu":           "Charles Darwin University",
    "murdoch":       "Murdoch University",
    "usq":           "University of Southern Queensland / UniSQ",
    "utas":          "University of Tasmania",
    "curtin":        "Curtin University",
    "rmit":          "RMIT University",
    "une":           "University of New England",
    "torrens":       "Torrens University Australia",
    "ecu":           "Edith Cowan University",
    "flinders":      "Flinders University",
}

# Guesses — will be tried; skipped if page doesn't load or has no UniqueId
GUESSES = [
    "uts", "unsw", "uow", "newcastle", "sydney", "bond", "cqu",
    "griffith", "jcu", "qut", "uq", "adelaide", "deakin",
    "latrobe", "monash", "swinburne", "unimelb", "vu", "uwa",
    "acu", "csu", "scu", "jcu",
]

# Name hints for guesses (best-effort match to universities table)
GUESS_NAMES = {
    "uts":       "University of Technology Sydney",
    "unsw":      "UNSW Sydney",
    "uow":       "University of Wollongong",
    "newcastle":  "University of Newcastle",
    "sydney":    "University of Sydney",
    "bond":      "Bond University",
    "cqu":       "CQUniversity Australia",
    "griffith":  "Griffith University",
    "jcu":       "James Cook University",
    "qut":       "Queensland University of Technology",
    "uq":        "University of Queensland",
    "adelaide":  "Adelaide University",
    "deakin":    "Deakin University",
    "latrobe":   "La Trobe University",
    "monash":    "Monash University",
    "swinburne": "Swinburne University of Technology",
    "unimelb":   "University of Melbourne",
    "vu":        "Victoria University",
    "uwa":       "University of Western Australia",
    "acu":       "Australian Catholic University",
    "csu":       "Charles Sturt University",
    "scu":       "Southern Cross University",
}


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_or_create_university(conn, name, shortcode):
    """Return university id, creating a row if needed."""
    # Try partial name match first
    row = conn.execute(
        "SELECT id FROM universities WHERE LOWER(name) LIKE LOWER(?)",
        (f"%{name.split('/')[0].strip()}%",)
    ).fetchone()
    if row:
        return row["id"]

    # Insert new
    conn.execute(
        """INSERT INTO universities (name, agent_page_url, scrape_status)
           VALUES (?, ?, 'pending')""",
        (name, f"{BASE_URL}/{shortcode}")
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def save_agents(conn, uni_id, agents, source_url):
    saved = 0
    for a in agents:
        addr_parts = [
            a.get("AgentStreet1", ""),
            a.get("AgentStreet2", ""),
            a.get("AgentCity", ""),
            a.get("AgentState", ""),
            a.get("post_code", ""),
        ]
        address = ", ".join(p for p in addr_parts if p and p.strip())

        company = (a.get("legal_name") or a.get("loc_display_as") or "").strip()
        country = (a.get("AgentCountry") or "").strip()
        if not company and not country:
            continue

        try:
            conn.execute("""
                INSERT INTO agents
                    (university_id, company_name, country, region, city,
                     email, phone, website, address, raw_text, source_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(university_id, company_name, country) DO UPDATE SET
                    region=excluded.region,
                    city=excluded.city,
                    email=excluded.email,
                    phone=excluded.phone,
                    website=excluded.website,
                    address=excluded.address,
                    raw_text=excluded.raw_text,
                    scraped_at=CURRENT_TIMESTAMP
            """, (
                uni_id,
                company or "Unknown",
                country,
                a.get("AgentState", ""),
                a.get("AgentCity", ""),
                a.get("email", ""),
                a.get("phone", ""),
                a.get("website", ""),
                address[:500],
                json.dumps({k: a[k] for k in ("legal_name","AgentCountry","email","phone","website","AddressLine1") if k in a}),
                source_url,
            ))
            saved += 1
        except Exception as e:
            pass
    conn.commit()
    return saved


# ─── AscentOne API ────────────────────────────────────────────────────────────

def get_unique_id(session, shortcode):
    """Fetch the EAP page and extract the UniqueId."""
    try:
        r = session.get(f"{BASE_URL}/{shortcode}", timeout=15)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        m = re.search(r"UniqueId\s*=\s*'([0-9a-f\-]{36})'", r.text, re.I)
        if not m:
            return None, "UniqueId not found in page"
        return m.group(1), None
    except Exception as e:
        return None, str(e)


def fetch_agents(session, unique_id, shortcode):
    """POST to the AscentOne handler and return the ClientDetails list."""
    client_filter = json.dumps({
        "eKey": unique_id,
        "hasMap": 0,
        "hasChinaMap": 0,
        "selectedDistance": "",
        "AgentName": "",
        "Country": "",
        "State": "",
        "City": "",
        "lattitude": 0,
        "longitude": 0,
    })
    try:
        r = session.post(
            f"{HANDLER}?rdnm=0.1&mapload=0&operate=GetAgentPublishersGridData",
            data={"ClientFilter": client_filter},
            headers={**HEADERS, "Referer": f"{BASE_URL}/{shortcode}"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("ClientDetails", []), None
    except Exception as e:
        return [], str(e)


# ─── Main loop ────────────────────────────────────────────────────────────────

def scrape_all(confirmed_only=False):
    conn = get_conn()
    session = requests.Session()
    session.headers.update(HEADERS)

    shortcodes = dict(CONFIRMED)
    if not confirmed_only:
        for code in GUESSES:
            if code not in shortcodes:
                shortcodes[code] = GUESS_NAMES.get(code, code.upper())

    results = []

    for shortcode, uni_name in shortcodes.items():
        print(f"\n{'─'*60}")
        print(f"  {shortcode:20s}  →  {uni_name}")

        unique_id, err = get_unique_id(session, shortcode)
        if err:
            print(f"  ✗  Page error: {err}")
            results.append((shortcode, uni_name, "page_error", 0, err))
            time.sleep(1)
            continue

        print(f"  UniqueId: {unique_id}")

        agents, err = fetch_agents(session, unique_id, shortcode)
        if err:
            print(f"  ✗  API error: {err}")
            results.append((shortcode, uni_name, "api_error", 0, err))
            time.sleep(1)
            continue

        if not agents:
            print(f"  ⚠  No agents returned")
            results.append((shortcode, uni_name, "no_agents", 0, None))
            time.sleep(1)
            continue

        uni_id = get_or_create_university(conn, uni_name, shortcode)
        saved  = save_agents(conn, uni_id, agents, f"{BASE_URL}/{shortcode}")

        # Update scrape status
        conn.execute(
            "UPDATE universities SET last_scraped=?, scrape_status=?, agent_page_url=? WHERE id=?",
            (datetime.now(), f"ok:ascentone ({saved})", f"{BASE_URL}/{shortcode}", uni_id)
        )
        conn.execute(
            "INSERT INTO scrape_log (university_id, status, agents_found, method, notes) VALUES (?,?,?,?,?)",
            (uni_id, "success", saved, "ascentone_api", f"shortcode={shortcode}")
        )
        conn.commit()

        print(f"  ✓  {len(agents)} agents fetched → {saved} saved to DB")
        results.append((shortcode, uni_name, "ok", saved, None))
        time.sleep(1.5)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"{'Shortcode':<20} {'University':<35} {'Status':<15} {'Agents':>7}")
    print("─" * 80)
    total = 0
    for code, name, status, count, _ in sorted(results, key=lambda x: -x[3]):
        print(f"{code:<20} {name[:34]:<35} {status:<15} {count:>7}")
        total += count
    print(f"\n  ✅  Total agents saved across all AscentOne universities: {total}")
    return results


def cmd_list():
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.name, u.scrape_status, COUNT(a.id) as agents
        FROM universities u
        LEFT JOIN agents a ON a.university_id = u.id
        WHERE u.scrape_status LIKE 'ok:ascentone%'
        GROUP BY u.id
        ORDER BY agents DESC
    """).fetchall()
    print(f"\n{'University':<45} {'Agents':>7}  Status")
    print("─" * 70)
    for r in rows:
        print(f"{r['name']:<45} {r['agents']:>7}  {r['scrape_status']}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AscentOne EAP Scraper")
    parser.add_argument("--confirmed-only", action="store_true",
                        help="Only scrape confirmed shortcodes, skip guesses")
    parser.add_argument("--list", action="store_true",
                        help="List previously scraped AscentOne universities")
    args = parser.parse_args()

    if args.list:
        cmd_list()
    else:
        scrape_all(confirmed_only=args.confirmed_only)
