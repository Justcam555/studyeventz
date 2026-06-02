#!/usr/bin/env python3
"""
Social media & online presence researcher for agents in Thailand and Nepal.

Strategy per agent:
  1. Website — HTTP HEAD check (active / inactive / no website)
  2. Social links — scrape homepage for fb/ig/linkedin hrefs
  3. Google Places API — search by name+city for rating & reviews
  4. Presence score — weighted 0-10

Usage:
    python3 research_social.py                    # Thailand + Nepal
    python3 research_social.py --country Thailand # one country
    python3 research_social.py --refresh          # re-research all
"""

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DB_PATH     = Path(__file__).parent / "data" / "agents.db"
PLACES_KEY  = os.environ.get("GOOGLE_PLACES_API_KEY", "")
COUNTRIES   = ["Thailand", "Nepal", "Cambodia", "Vietnam", "Indonesia", "Sri Lanka"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_social (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id            INTEGER NOT NULL,
            country             TEXT,
            canonical_name      TEXT,

            website_url         TEXT,
            website_active      INTEGER DEFAULT 0,

            linkedin_url        TEXT,
            linkedin_followers  INTEGER,

            facebook_url        TEXT,
            facebook_followers  INTEGER,

            instagram_handle    TEXT,
            instagram_url       TEXT,
            instagram_followers INTEGER,

            google_place_id     TEXT,
            google_rating       REAL,
            google_reviews      INTEGER,
            google_maps_url     TEXT,

            presence_score      REAL DEFAULT 0,
            notes               TEXT,
            researched_at       DATETIME DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(agent_id, country)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_social_agent ON agent_social(agent_id);
        CREATE INDEX IF NOT EXISTS idx_agent_social_country ON agent_social(country);
    """)
    conn.commit()


def get_agents(conn, country, refresh=False):
    """Return one representative row per canonical_name for this country."""
    extra = "" if refresh else "AND (s.id IS NULL OR s.researched_at IS NULL)"
    return conn.execute(f"""
        SELECT a.id, a.canonical_name, a.city, a.email, a.website, a.phone, a.country
        FROM agents a
        LEFT JOIN agent_social s ON s.agent_id = a.id AND s.country = a.country
        WHERE LOWER(a.country) = LOWER(?)
          AND a.canonical_name IS NOT NULL
          AND a.canonical_name NOT IN ('Email','Phone','Unknown','')
          {extra}
        GROUP BY a.canonical_name
        ORDER BY a.canonical_name
    """, (country,)).fetchall()


def upsert(conn, data):
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    updates = ", ".join(f"{k}=excluded.{k}" for k in data if k not in ("agent_id", "country"))
    conn.execute(
        f"""INSERT INTO agent_social ({cols}) VALUES ({placeholders})
            ON CONFLICT(agent_id, country) DO UPDATE SET {updates}""",
        list(data.values())
    )
    conn.commit()


# ── Website check ─────────────────────────────────────────────────────────────

def normalise_url(raw):
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("http"):
        raw = "https://" + raw
    return raw


def check_website(url):
    """Returns (is_active, final_url, html_or_None)."""
    if not url:
        return False, None, None
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        html = r.text if r.status_code == 200 else None
        return r.status_code < 400, r.url, html
    except Exception:
        return False, url, None


# ── Social link extraction from homepage ─────────────────────────────────────

SOCIAL_PATTERNS = {
    "facebook":  re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/(?!sharer|share|dialog|tr\b|plugins)([A-Za-z0-9._\-]+)", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]+)", re.I),
    "linkedin":  re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/company/([A-Za-z0-9._\-]+)", re.I),
}

JUNK_FB   = {"100044192680673", "100063783724699", "plugins", "sharer", "share"}
JUNK_IG   = {"p", "explore", "accounts", "reel", "stories"}
JUNK_LI   = {"company", "in", "school"}


def extract_socials(html, base_url):
    """Scrape social links from page HTML."""
    result = {}
    if not html:
        return result

    soup = BeautifulSoup(html, "lxml")
    # Collect all hrefs + any text containing social URLs
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    page_text = " ".join(hrefs) + " " + soup.get_text(" ")

    for platform, pat in SOCIAL_PATTERNS.items():
        for m in pat.finditer(page_text):
            handle = m.group(1).strip("/").split("?")[0]
            if platform == "facebook" and handle in JUNK_FB:
                continue
            if platform == "instagram" and handle in JUNK_IG:
                continue
            if platform == "linkedin" and handle in JUNK_LI:
                continue
            full_url = m.group(0)
            if not full_url.startswith("http"):
                full_url = "https://" + full_url.lstrip("/")
            result[platform] = {"handle": handle, "url": full_url}
            break  # take first match per platform

    return result


# ── Google Places ─────────────────────────────────────────────────────────────

def google_places_lookup(name, city, country):
    """Query Google Places Text Search. Returns (rating, reviews, place_id, maps_url)."""
    query = f"{name} {city or ''} {country} education"
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": query, "key": PLACES_KEY},
            timeout=10,
        )
        data = r.json()
        results = data.get("results", [])
        if not results:
            return None, None, None, None
        top = results[0]
        rating   = top.get("rating")
        reviews  = top.get("user_ratings_total")
        place_id = top.get("place_id")
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else None
        return rating, reviews, place_id, maps_url
    except Exception:
        return None, None, None, None


# ── Presence score ────────────────────────────────────────────────────────────

def calc_score(data):
    """Score out of 10."""
    score = 0.0
    if data.get("website_active"):         score += 2.0
    if data.get("google_rating"):
        score += 1.5
        if data["google_rating"] >= 4.0:   score += 0.5
    if data.get("google_reviews", 0) >= 10: score += 0.5
    if data.get("facebook_url"):           score += 2.0
    if data.get("instagram_url"):          score += 1.5
    if data.get("linkedin_url"):           score += 1.0
    if data.get("instagram_followers", 0) >= 500:   score += 0.5
    if data.get("instagram_followers", 0) >= 2000:  score += 0.5
    return min(round(score, 1), 10.0)


# ── Main loop ─────────────────────────────────────────────────────────────────

def research_country(conn, country, session, refresh=False):
    agents = get_agents(conn, country, refresh)
    print(f"\n{'='*60}\n  {country} — {len(agents)} agents to research\n{'='*60}")

    for i, row in enumerate(agents, 1):
        agent_id, name, city, email, website_raw, phone, ctry = row
        print(f"\n[{i}/{len(agents)}] {name[:55]}")

        data = {
            "agent_id":     agent_id,
            "country":      ctry,
            "canonical_name": name,
            "researched_at": datetime.now().isoformat(),
        }

        # 1. Website
        url = normalise_url(website_raw)
        data["website_url"] = url
        active, final_url, html = check_website(url)
        data["website_active"] = 1 if active else 0
        if active:
            data["website_url"] = final_url
            print(f"  🌐 Website: active ({final_url[:50]})")
        else:
            html = None
            print(f"  🌐 Website: {'none' if not url else 'inactive'}")

        # 2. Social links from homepage
        socials = extract_socials(html, final_url if active else url)
        if socials.get("facebook"):
            data["facebook_url"] = socials["facebook"]["url"]
            print(f"  📘 Facebook: {socials['facebook']['url'][:60]}")
        if socials.get("instagram"):
            data["instagram_handle"] = socials["instagram"]["handle"]
            data["instagram_url"]    = socials["instagram"]["url"]
            print(f"  📸 Instagram: @{socials['instagram']['handle']}")
        if socials.get("linkedin"):
            data["linkedin_url"] = socials["linkedin"]["url"]
            print(f"  💼 LinkedIn: {socials['linkedin']['url'][:60]}")

        # 3. Google Places
        rating, reviews, place_id, maps_url = google_places_lookup(name, city, country)
        if rating:
            data["google_rating"]   = rating
            data["google_reviews"]  = reviews
            data["google_place_id"] = place_id
            data["google_maps_url"] = maps_url
            print(f"  ⭐ Google: {rating}★ ({reviews} reviews)")
        else:
            print(f"  ⭐ Google: not found")

        # 4. Presence score
        data["presence_score"] = calc_score(data)
        print(f"  📊 Score: {data['presence_score']}/10")

        upsert(conn, data)
        time.sleep(0.8)  # polite delay

    # Summary
    rows = conn.execute("""
        SELECT canonical_name, presence_score, google_rating, google_reviews,
               website_active, facebook_url, instagram_handle, linkedin_url
        FROM agent_social WHERE country=?
        ORDER BY presence_score DESC
    """, (country,)).fetchall()

    print(f"\n{'─'*70}")
    print(f"  {'Agent':<40} {'Score':>5}  {'G★':>4}  FB  IG  LI  Web")
    print(f"  {'─'*68}")
    for r in rows:
        fb = "✓" if r[5] else "·"
        ig = "✓" if r[6] else "·"
        li = "✓" if r[7] else "·"
        wb = "✓" if r[4] else "·"
        gr = f"{r[2]:.1f}" if r[2] else "  ·"
        print(f"  {r[0][:39]:<40} {r[1]:>4.1f}  {gr:>4}  {fb:>2}  {ig:>2}  {li:>2}  {wb:>2}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", help="Single country to research")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    session = requests.Session()
    session.headers.update(HEADERS)

    countries = [args.country] if args.country else COUNTRIES
    for country in countries:
        research_country(conn, country, session, refresh=args.refresh)

    conn.close()
    print("\n✅  Research complete.")


if __name__ == "__main__":
    main()
