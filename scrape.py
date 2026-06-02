#!/usr/bin/env python3
"""
Australian University Agent Scraper
Scrapes agent pages from Australian universities and stores data in SQLite.

Usage:
    python scrape.py                    # Scrape all universities
    python scrape.py --uni "Monash"     # Scrape one university (partial name match)
    python scrape.py --refresh          # Re-scrape all (ignore existing data)
    python scrape.py --list             # List all universities and scrape status
"""

import argparse
import sqlite3
import json
import time
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "agents.db"
XLSX_PATH = Path(__file__).parent / "data" / "australian_university_agent_pages.xlsx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ─── Database setup ──────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS universities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            website     TEXT,
            agent_page_url TEXT,
            status_note TEXT,
            last_scraped DATETIME,
            scrape_status TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS agents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            university_id   INTEGER REFERENCES universities(id),
            company_name    TEXT,
            contact_name    TEXT,
            country         TEXT,
            region          TEXT,
            city            TEXT,
            email           TEXT,
            phone           TEXT,
            website         TEXT,
            address         TEXT,
            raw_text        TEXT,
            source_url      TEXT,
            scraped_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(university_id, company_name, country)
        );

        CREATE TABLE IF NOT EXISTS scrape_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            university_id   INTEGER REFERENCES universities(id),
            scraped_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            status          TEXT,
            agents_found    INTEGER DEFAULT 0,
            method          TEXT,
            notes           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_agents_university ON agents(university_id);
        CREATE INDEX IF NOT EXISTS idx_agents_country ON agents(country);
        CREATE INDEX IF NOT EXISTS idx_agents_email ON agents(email);
    """)

    conn.commit()
    return conn


def load_universities(conn):
    """Load university list from Excel into DB."""
    df = pd.read_excel(XLSX_PATH, sheet_name="Agent Pages")
    c = conn.cursor()
    for _, row in df.iterrows():
        c.execute("""
            INSERT INTO universities (name, website, agent_page_url, status_note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                website=excluded.website,
                agent_page_url=excluded.agent_page_url,
                status_note=excluded.status_note
        """, (
            row["University"],
            row.get("Website"),
            row.get("Agent page URL") if pd.notna(row.get("Agent page URL")) else None,
            row.get("Status / note") if pd.notna(row.get("Status / note")) else None,
        ))
    conn.commit()
    print(f"✓ Loaded {len(df)} universities into database")


# ─── Scraping logic ───────────────────────────────────────────────────────────

def fetch_page(url, timeout=20, session=None):
    req = session or requests
    try:
        r = req.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text, r.url, None
    except Exception as e:
        return None, url, str(e)


def parse_agents(html, base_url, university_name):
    """
    Multi-strategy parser to extract agent data from a university agent page.
    Returns list of dicts with agent fields.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    agents = []
    method = "none"

    # ── Strategy 1: HTML Tables ───────────────────────────────────────────
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        raw_headers = [th.get_text(" ", strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        kw = ["agent", "company", "name", "country", "region", "city", "email", "contact", "representative"]
        if not any(k in " ".join(raw_headers) for k in kw):
            continue

        col_map = _map_columns(raw_headers)
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if not any(cells):
                continue
            agent = _build_agent_from_cells(cells, raw_headers, col_map)
            links = row.find_all("a", href=True)
            for lnk in links:
                href = lnk["href"]
                if href.startswith("mailto:"):
                    agent["email"] = href[7:]
                elif href.startswith("http") and urlparse(base_url).netloc not in href:
                    agent.setdefault("website", href)
            if _is_valid_agent(agent):
                agents.append(agent)
        if agents:
            method = "table"
            break

    if agents:
        return agents, method

    # ── Strategy 2: Repeated card / list elements ─────────────────────────
    card_patterns = [
        r"agent", r"partner", r"representative", r"contact.?card",
        r"country.?item", r"office.?item", r"agent.?item", r"list.?item"
    ]
    for pat in card_patterns:
        containers = soup.find_all(["div", "li", "article", "section"],
                                   class_=re.compile(pat, re.I))
        if len(containers) >= 3:
            for container in containers[:300]:
                agent = _extract_from_container(container, base_url)
                if _is_valid_agent(agent):
                    agents.append(agent)
            if agents:
                method = "cards"
                break

    if agents:
        return agents, method

    # ── Strategy 3: Definition lists / structured text ────────────────────
    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        defs = dl.find_all("dd")
        if len(terms) >= 3:
            agent = {}
            for t, d in zip(terms, defs):
                key = t.get_text(strip=True).lower().rstrip(":")
                val = d.get_text(" ", strip=True)
                _assign_field(agent, key, val)
            if _is_valid_agent(agent):
                agents.append(agent)
                method = "dl"

    if agents:
        return agents, method

    # ── Strategy 4: Email-anchored text blocks ────────────────────────────
    full_text = soup.get_text("\n", strip=True)
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", line)
        if m:
            ctx_lines = lines[max(0, i - 4): i + 5]
            block = " | ".join(ctx_lines)
            agent = {"raw_text": block, "email": m.group()}
            # Try to grab a company/org name from preceding lines
            for prev in reversed(lines[max(0, i - 4): i]):
                if len(prev) > 5 and not re.search(r"@|http|phone|fax|tel", prev, re.I):
                    agent["company_name"] = prev
                    break
            agents.append(agent)
    if agents:
        method = "email_blocks"

    if agents:
        return agents, method

    # ── Strategy 5: Country/region headings + bullet entries ─────────────
    # Some pages list agents under country headings
    country_agents = _parse_by_country_headings(soup, base_url)
    if country_agents:
        return country_agents, "country_headings"

    # ── Fallback: Raw page snapshot ───────────────────────────────────────
    main = soup.find("main") or soup.find("body")
    preview = main.get_text(" ", strip=True)[:2000] if main else ""
    return [{"raw_text": preview}], "raw_text_fallback"


def _map_columns(headers):
    """Map raw table headers to canonical field names."""
    field_map = {
        "company_name": ["company", "agent", "organisation", "organization", "name", "agency", "representative"],
        "contact_name": ["contact", "person", "representative", "manager"],
        "country":      ["country", "nation", "location"],
        "region":       ["region", "state", "province", "territory", "area"],
        "city":         ["city", "suburb", "town", "office"],
        "email":        ["email", "e-mail", "mail"],
        "phone":        ["phone", "tel", "telephone", "mobile", "fax"],
        "website":      ["website", "web", "url", "site"],
        "address":      ["address", "street", "addr"],
    }
    result = {}
    for i, h in enumerate(headers):
        for field, synonyms in field_map.items():
            if any(s in h for s in synonyms) and field not in result.values():
                result[i] = field
                break
    return result


def _build_agent_from_cells(cells, headers, col_map):
    agent = {}
    for i, cell in enumerate(cells):
        field = col_map.get(i)
        if field:
            agent[field] = cell
        elif i < len(headers) and headers[i]:
            agent[headers[i]] = cell
    return agent


def _extract_from_container(container, base_url):
    agent = {"raw_text": container.get_text(" ", strip=True)}
    # Email
    m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", agent["raw_text"])
    if m:
        agent["email"] = m.group()
    # Phone
    m = re.search(r"(?:Ph|Tel|Phone|Fax)?[\s:]*(\+?[\d\s\-().]{7,20})", agent["raw_text"])
    if m:
        agent["phone"] = m.group(1).strip()
    # Links
    for lnk in container.find_all("a", href=True):
        href = lnk["href"]
        if href.startswith("mailto:"):
            agent["email"] = href[7:]
        elif href.startswith("http") and urlparse(base_url).netloc not in href:
            agent.setdefault("website", href)
    # Headings as company name
    h = container.find(["h2", "h3", "h4", "strong", "b"])
    if h:
        agent["company_name"] = h.get_text(strip=True)
    return agent


def _parse_by_country_headings(soup, base_url):
    agents = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        heading_text = heading.get_text(strip=True)
        # Likely a country name if it's short and title-case
        if len(heading_text) < 40 and re.match(r"^[A-Z][a-zA-Z\s]+$", heading_text):
            country = heading_text
            # Grab next sibling ul/ol/div
            sib = heading.find_next_sibling(["ul", "ol", "div", "table"])
            if sib:
                items = sib.find_all(["li", "tr", "p"])
                for item in items:
                    text = item.get_text(" ", strip=True)
                    if len(text) > 8:
                        agent = {"country": country, "raw_text": text}
                        m = re.search(r"[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}", text)
                        if m:
                            agent["email"] = m.group()
                        for lnk in item.find_all("a", href=True):
                            href = lnk["href"]
                            if href.startswith("mailto:"):
                                agent["email"] = href[7:]
                            elif href.startswith("http") and urlparse(base_url).netloc not in href:
                                agent.setdefault("website", href)
                        agents.append(agent)
    return agents


def _assign_field(agent, key, val):
    for field, synonyms in {
        "company_name": ["company", "agent", "organisation"],
        "country":      ["country"],
        "email":        ["email"],
        "phone":        ["phone", "tel"],
        "website":      ["website", "web"],
        "address":      ["address"],
    }.items():
        if any(s in key for s in synonyms):
            agent[field] = val
            return
    agent[key] = val


def _is_valid_agent(agent):
    meaningful = {"company_name", "email", "phone", "country", "website", "contact_name"}
    return any(k in agent and agent[k] for k in meaningful)


# ─── Main scrape loop ─────────────────────────────────────────────────────────

def scrape_university(conn, uni_row, session, verbose=True):
    uni_id = uni_row["id"]
    uni_name = uni_row["name"]
    url = uni_row["agent_page_url"]

    if not url:
        _log(conn, uni_id, "no_url", 0, None, "No agent page URL available")
        if verbose:
            print(f"  ⚠  No URL for {uni_name}")
        return 0

    if verbose:
        print(f"\n{'─'*60}")
        print(f"  🌐  {uni_name}")
        print(f"  URL: {url}")

    html, final_url, err = fetch_page(url, session=session)

    if err:
        _log(conn, uni_id, "fetch_error", 0, None, err)
        conn.execute("UPDATE universities SET last_scraped=?, scrape_status=? WHERE id=?",
                     (datetime.now(), "error", uni_id))
        conn.commit()
        if verbose:
            print(f"  ✗  Fetch error: {err[:120]}")
        return 0

    agents, method = parse_agents(html, final_url, uni_name)

    saved = 0
    for agent in agents:
        if method == "raw_text_fallback":
            continue  # Don't save raw fallback as agent records
        try:
            conn.execute("""
                INSERT INTO agents
                    (university_id, company_name, contact_name, country, region, city,
                     email, phone, website, address, raw_text, source_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(university_id, company_name, country) DO UPDATE SET
                    contact_name=excluded.contact_name,
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
                agent.get("company_name", agent.get("name", agent.get("agent", "Unknown"))),
                agent.get("contact_name"),
                agent.get("country"),
                agent.get("region"),
                agent.get("city"),
                agent.get("email"),
                agent.get("phone"),
                agent.get("website"),
                agent.get("address"),
                agent.get("raw_text", json.dumps(agent))[:2000],
                final_url,
            ))
            saved += 1
        except Exception as e:
            pass

    conn.execute("UPDATE universities SET last_scraped=?, scrape_status=? WHERE id=?",
                 (datetime.now(), f"ok:{method}", uni_id))
    _log(conn, uni_id, "success", saved, method, None)
    conn.commit()

    if verbose:
        print(f"  ✓  Method: {method} | Agents saved: {saved}")
    return saved


def _log(conn, uni_id, status, count, method, notes):
    conn.execute(
        "INSERT INTO scrape_log (university_id, status, agents_found, method, notes) VALUES (?,?,?,?,?)",
        (uni_id, status, count, method, notes)
    )


def run_scrape(conn, filter_name=None, refresh=False):
    session = requests.Session()
    session.headers.update(HEADERS)

    query = "SELECT * FROM universities"
    params = []
    if filter_name:
        query += " WHERE name LIKE ?"
        params.append(f"%{filter_name}%")
    elif not refresh:
        query += " WHERE scrape_status='pending' OR scrape_status IS NULL OR scrape_status LIKE 'error%'"

    unis = conn.execute(query, params).fetchall()

    if not unis:
        print("No universities to scrape (all already done). Use --refresh to re-scrape.")
        return

    total_agents = 0
    for uni in unis:
        count = scrape_university(conn, uni, session)
        total_agents += count
        time.sleep(2)  # polite delay

    print(f"\n{'='*60}")
    print(f"✅  Scrape complete. Total agents saved: {total_agents}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def cmd_list(conn):
    rows = conn.execute("""
        SELECT u.name, u.scrape_status, u.last_scraped,
               COUNT(a.id) as agent_count
        FROM universities u
        LEFT JOIN agents a ON a.university_id = u.id
        GROUP BY u.id
        ORDER BY u.name
    """).fetchall()
    print(f"\n{'University':<45} {'Status':<20} {'Agents':>7} {'Last Scraped'}")
    print("─" * 95)
    for r in rows:
        scraped = r["last_scraped"][:16] if r["last_scraped"] else "never"
        print(f"{r['name']:<45} {(r['scrape_status'] or 'pending'):<20} {r['agent_count']:>7}  {scraped}")
    total = sum(r["agent_count"] for r in rows)
    print(f"\nTotal agents in database: {total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AU University Agent Scraper")
    parser.add_argument("--uni", help="Filter by university name (partial match)")
    parser.add_argument("--refresh", action="store_true", help="Re-scrape all universities")
    parser.add_argument("--list", action="store_true", help="List universities and status")
    parser.add_argument("--load-only", action="store_true", help="Only load university list, don't scrape")
    args = parser.parse_args()

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = init_db()
    load_universities(conn)

    if args.list:
        cmd_list(conn)
    elif args.load_only:
        print("University list loaded.")
    else:
        run_scrape(conn, filter_name=args.uni, refresh=args.refresh)
        cmd_list(conn)

    conn.close()
