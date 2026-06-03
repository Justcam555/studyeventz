#!/usr/bin/env python3
"""
build_events_page.py — Build the events.html static page for the GitHub Pages site.

Reads events from data/agents.db, exports data/events.json, and writes
events.html at the repo root. events.html fetches events.json at runtime
and renders the filtered, week-grouped layout client-side.

Usage:
    python build_events_page.py
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "agents.db"
JSON_OUT = ROOT / "data" / "events.json"
HTML_OUT = ROOT / "events.html"
SITEMAP_OUT = ROOT / "sitemap.xml"
ROBOTS_OUT = ROOT / "robots.txt"
CHARACTERS_DIR = ROOT / "assets" / "characters"
LOGOS_DIR = ROOT / "assets" / "logos"

# ─── Site metadata (edit these to change brand-level details) ──────────────
SITE_URL       = "https://www.studyeventz.com"
EVENTS_PAGE    = f"{SITE_URL}/events.html"
PAGE_TITLE     = "Study Abroad Events in Thailand 2026 | studyeventz"
META_DESC_EN   = ("Find upcoming university fairs, info days and study abroad events "
                  "in Bangkok and Thailand. Updated weekly by studyeventz.")
META_DESC_TH   = "รวมงาน Study Abroad ในไทย อัปเดตทุกสัปดาห์"
LINE_HANDLE    = "@studyeventz"  # change here to update the LINE banner everywhere

# Tokens to skip when extracting initials from agent names
STOPWORDS_FOR_INITIALS = {"co", "ltd", "the", "and", "pty", "inc", "llc", "corp", "limited"}


def extract_initials(name: str) -> str:
    """Return up to 2 uppercase initials for use in the fallback avatar."""
    if not name:
        return "?"
    words = re.findall(r"[A-Za-z]+", name)
    chars: list[str] = []
    for w in words:
        if w.lower() in STOPWORDS_FOR_INITIALS:
            continue
        chars.append(w[0].upper())
        if len(chars) >= 2:
            break
    return "".join(chars) or name[:1].upper()


def find_logo(agent_name: str) -> str | None:
    """Look for assets/logos/{agent_name}.png (literal + slug variants). Returns URL or None."""
    if not LOGOS_DIR.exists() or not agent_name:
        return None
    slug = re.sub(r"[^A-Za-z0-9]+", "_", agent_name).strip("_")
    candidates = [f"{agent_name}.png", f"{slug}.png", f"{slug.lower()}.png"]
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if (LOGOS_DIR / cand).exists():
            return f"assets/logos/{quote(cand)}"
    return None


# Explicit agent → logo mapping. Substring matched against agent_name (lowercase).
# First match wins. Tuple: (substring, logo_path_relative_to_LOGOS_DIR_or_None, initials_override_or_None).
AGENT_LOGO_MAP: list[tuple[str, str | None, str | None]] = [
    ("studyin",            "StudyeventZ logos/studyin-logo.svg",                  None),
    ("aecc",               "StudyeventZ logos/aecc_logo.svg",                     None),
    ("idp",                "StudyeventZ logos/idp-logo.svg",                      None),
    ("one education",      "StudyeventZ logos/One Education Logo - Green.png",    None),
    ("british education",  "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("brit education",     "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("brit-ed",            "StudyeventZ logos/logo-brit-ed.svg",                  None),
    ("iec abroad",         "StudyeventZ logos/IEC_Logo-removebg-preview.png",     None),
    ("gouni",              "StudyeventZ logos/GoUni logopng.png",                 None),
    ("go uni",             "StudyeventZ logos/GoUni logopng.png",                 None),
    ("hands on",           "StudyeventZ logos/HandsOn Logo.png",                  None),
    ("hands-on",           "StudyeventZ logos/HandsOn Logo.png",                  None),
    ("mango",              "StudyeventZ logos/Mango.png",                         None),
]

# Logo files that need a coloured background circle behind them.
# Value is the CSS colour to use; None means default (teal, defined in CSS).
LOGOS_NEEDING_BG: dict[str, str | None] = {
    # No bg overrides currently — the new One Education PNG has green baked in.
}


def find_logo_for_agent(agent_name: str) -> tuple[str | None, str | None, bool, str]:
    """Resolve (logo_url, initials_override, needs_bg, bg_color_override) for an agent name.

    bg_color_override is an empty string when the default teal background should
    apply (or when needs_bg is False); otherwise a CSS colour string.

    Order:
    1. AGENT_LOGO_MAP substring match (explicit overrides)
    2. find_logo() literal/slug filename match in LOGOS_DIR root
    """
    if not agent_name:
        return None, None, False, ""
    name_lower = agent_name.lower()
    for substr, logo_rel, initials in AGENT_LOGO_MAP:
        if substr in name_lower:
            if logo_rel is None:
                return None, initials, False, ""
            full = LOGOS_DIR / logo_rel
            if full.exists():
                needs_bg = logo_rel in LOGOS_NEEDING_BG
                bg_color = LOGOS_NEEDING_BG.get(logo_rel) or "" if needs_bg else ""
                return f"assets/logos/{quote(logo_rel)}", initials, needs_bg, bg_color
            print(
                f"  WARN: mapped logo missing for '{agent_name}': {full}",
                file=sys.stderr,
            )
            return None, initials, False, ""
    return find_logo(agent_name), None, False, ""


def _natural_key(name: str):
    """Sort 'studyeventz 1' before 'studyeventz 10'."""
    parts = re.split(r"(\d+)", name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def discover_character_images() -> list[str]:
    """Return URL-safe relative paths to each character PNG, in natural order."""
    if not CHARACTERS_DIR.exists():
        return []
    pngs = sorted(CHARACTERS_DIR.glob("*.png"), key=lambda p: _natural_key(p.name))
    # Encode spaces / special chars for use in HTML src attributes
    return [f"assets/characters/{quote(p.name)}" for p in pngs]


def build_event_json_ld(events: list[dict]) -> str:
    """Return a JSON array string of schema.org/Event objects, one per event,
    ready to drop into a <script type="application/ld+json"> block."""
    docs = []
    for ev in events:
        is_online = "online" in (ev.get("location") or "").lower()
        location_doc: dict
        if is_online:
            location_doc = {
                "@type": "VirtualLocation",
                "url": EVENTS_PAGE,
            }
        else:
            location_doc = {
                "@type": "Place",
                "name": ev.get("location") or "Thailand",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": ev.get("location") or "Bangkok",
                    "addressCountry": "TH",
                },
            }
        organizer_doc = {"@type": "Organization", "name": ev.get("organizer") or ev.get("agent_name") or "studyeventz"}
        if ev.get("agent_website"):
            organizer_doc["url"] = ev["agent_website"]

        doc = {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": ev.get("name") or "",
            "startDate": ev.get("date") or "",
            "endDate": ev.get("date") or "",
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": (
                "https://schema.org/OnlineEventAttendanceMode" if is_online
                else "https://schema.org/OfflineEventAttendanceMode"
            ),
            "location": location_doc,
            "organizer": organizer_doc,
            "url": f"{EVENTS_PAGE}#event-{ev.get('id', '')}",
        }
        if ev.get("registration_url"):
            doc["offers"] = {
                "@type": "Offer",
                "url": ev["registration_url"],
                "availability": "https://schema.org/InStock",
            }
        docs.append(doc)
    return json.dumps(docs, ensure_ascii=False, indent=2)


def write_seo_files() -> None:
    """Write sitemap.xml and robots.txt at the repo root."""
    today = datetime.now().date().isoformat()
    SITEMAP_OUT.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{EVENTS_PAGE}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>
""",
        encoding="utf-8",
    )
    ROBOTS_OUT.write_text(
        f"""User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
""",
        encoding="utf-8",
    )


def _normalize_event_name(name: str) -> str:
    """Lowercase, strip punctuation and whitespace for dedup key matching."""
    return re.sub(r"[^a-z0-9ก-๙]+", " ", (name or "").lower()).strip()


def deduplicate_rows(rows: list) -> list:
    """Collapse duplicates:
       - Online events: dedup across agents on (name, date) — multiple agents
         promoting the same webinar should appear once.
       - Physical events: dedup only within the same agent on (name, date) —
         cross-agent listings of in-person fairs/seminars stay separate so
         each agent keeps its own attribution.
       In both cases, keep the row with the most complete location string."""
    groups: dict[tuple, list] = {}
    for r in rows:
        is_online = "online" in (r["location"] or "").lower()
        if is_online:
            key = ("online", _normalize_event_name(r["name"]), r["date"])
        else:
            # Physical events: collapse rows that resolve to the same logo
            # (treats "IDP Education Services Co., Ltd." and "IDP Thailand" as the
            # same agent because they both map to idp-logo.svg). Agents without a
            # logo mapping fall back to exact agent_name.
            logo_url, _, _, _ = find_logo_for_agent(r["agent_name"])
            agent_key = logo_url or r["agent_name"]
            key = ("physical", _normalize_event_name(r["name"]), r["date"], agent_key)
        groups.setdefault(key, []).append(r)

    kept: list = []
    dropped = 0
    for key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Sort: longest location first, then lowest id for stability
        best = sorted(
            group,
            key=lambda r: (-len((r["location"] or "").strip()), r["id"]),
        )[0]
        kept.append(best)
        dropped += len(group) - 1
    if dropped:
        print(f"Deduplicated {dropped} event(s) — collapsed duplicates by (name, date).")
    # Preserve SQL ordering (date, time)
    kept.sort(key=lambda r: (r["date"], r["time"] or ""))
    return kept


def export_events_json() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today = datetime.now().date()
    cutoff = today + timedelta(days=30)

    rows = conn.execute(
        """SELECT e.id, e.name, e.date, e.time, e.location, e.organizer,
                  e.registration_url, a.company_name AS agent_name,
                  a.website AS agent_website, a.country AS agent_country
           FROM events e JOIN agents a ON e.agent_id = a.id
           WHERE e.date BETWEEN ? AND ?
           ORDER BY e.date, e.time""",
        (today.isoformat(), cutoff.isoformat()),
    ).fetchall()

    rows = deduplicate_rows(rows)

    events_out = []
    for r in rows:
        logo_url, initials_override, needs_bg, bg_color = find_logo_for_agent(r["agent_name"])
        events_out.append({
            "id": r["id"],
            "name": r["name"],
            "date": r["date"],
            "time": r["time"] or "",
            "location": r["location"] or "",
            "organizer": r["organizer"] or r["agent_name"],
            "agent_name": r["agent_name"],
            "agent_country": r["agent_country"] or "",
            "agent_website": r["agent_website"] or "",
            "registration_url": r["registration_url"] or "",
            "logo_url": logo_url or "",
            "logo_needs_bg": needs_bg,
            "logo_bg_color": bg_color,
            "initials": initials_override or extract_initials(r["agent_name"]),
        })

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"from": today.isoformat(), "to": cutoff.isoformat()},
        "events": events_out,
    }

    JSON_OUT.parent.mkdir(exist_ok=True)
    JSON_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


# Legacy fallback SVG silhouettes — only used if no PNGs exist in assets/characters/.
# Kept so the page still renders if the asset directory is missing.
CHARACTER_SVGS = [
    # 0: Student with backpack, standing
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- backpack -->
      <rect x="22" y="68" width="22" height="42" rx="6" fill="#3a4a5c"/>
      <rect x="26" y="78" width="14" height="3" fill="#1a2a3a"/>
      <!-- body -->
      <path d="M40 70 Q55 60 70 70 L74 130 Q55 134 36 130 Z" fill="#dfe6ee"/>
      <!-- arms -->
      <path d="M40 72 L34 110 L38 112 L44 76 Z" fill="#dfe6ee"/>
      <path d="M70 72 L76 110 L72 112 L66 76 Z" fill="#dfe6ee"/>
      <!-- legs -->
      <rect x="44" y="128" width="9" height="38" fill="#5a6b7d"/>
      <rect x="57" y="128" width="9" height="38" fill="#5a6b7d"/>
      <!-- shoes -->
      <ellipse cx="48" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="62" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- hair -->
      <path d="M37 44 Q40 28 55 26 Q70 28 73 44 Q72 36 65 34 L62 40 Q55 33 48 40 L45 34 Q38 36 37 44 Z" fill="#1a1a1a"/>
      <!-- eyes -->
      <ellipse cx="49" cy="50" rx="1.7" ry="2.4" fill="#1a1a1a"/>
      <ellipse cx="61" cy="50" rx="1.7" ry="2.4" fill="#1a1a1a"/>
      <!-- mouth -->
      <path d="M52 58 Q55 60 58 58" stroke="#1a1a1a" stroke-width="1.2" fill="none" stroke-linecap="round"/>
      <!-- backpack strap front -->
      <path d="M44 72 L42 100" stroke="#1a2a3a" stroke-width="2" fill="none"/>
    </svg>""",
    # 1: Student with laptop, seated/casual
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="30" ry="4" fill="#000" opacity=".35"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- hair (longer) -->
      <path d="M36 46 Q34 28 55 24 Q76 28 74 46 Q74 38 68 36 Q60 30 50 32 Q40 36 36 46 Z" fill="#2a2a2a"/>
      <path d="M37 48 Q33 60 36 70 L39 56 Z" fill="#2a2a2a"/>
      <!-- eyes (focused down) -->
      <ellipse cx="49" cy="52" rx="1.7" ry="1.5" fill="#1a1a1a"/>
      <ellipse cx="61" cy="52" rx="1.7" ry="1.5" fill="#1a1a1a"/>
      <!-- mouth -->
      <path d="M52 60 L58 60" stroke="#1a1a1a" stroke-width="1.2" stroke-linecap="round"/>
      <!-- body -->
      <path d="M38 70 Q55 64 72 70 L74 124 Q55 128 36 124 Z" fill="#94a3b8"/>
      <!-- arms forward holding laptop -->
      <path d="M38 80 L30 116 L42 118 L46 86 Z" fill="#94a3b8"/>
      <path d="M72 80 L80 116 L68 118 L64 86 Z" fill="#94a3b8"/>
      <!-- laptop -->
      <rect x="28" y="116" width="54" height="14" rx="2" fill="#1a2a3a"/>
      <rect x="30" y="118" width="50" height="10" fill="#5fb8b8"/>
      <!-- laptop base -->
      <path d="M26 130 L84 130 L80 134 L30 134 Z" fill="#3a4a5c"/>
      <!-- legs (seated, short visible) -->
      <rect x="42" y="134" width="11" height="32" fill="#5a6b7d"/>
      <rect x="57" y="134" width="11" height="32" fill="#5a6b7d"/>
      <ellipse cx="47" cy="168" rx="8" ry="3" fill="#1a2a3a"/>
      <ellipse cx="63" cy="168" rx="8" ry="3" fill="#1a2a3a"/>
    </svg>""",
    # 2: Student looking up (head tilted, dreaming)
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- body -->
      <path d="M40 72 Q55 64 70 72 L72 132 Q55 136 38 132 Z" fill="#cbd5e1"/>
      <!-- arms relaxed -->
      <path d="M40 74 L34 124 L40 126 L46 78 Z" fill="#cbd5e1"/>
      <path d="M70 74 L76 124 L70 126 L64 78 Z" fill="#cbd5e1"/>
      <!-- legs -->
      <rect x="44" y="130" width="9" height="36" fill="#475569"/>
      <rect x="57" y="130" width="9" height="36" fill="#475569"/>
      <ellipse cx="48" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="62" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <!-- head tilted up + back -->
      <g transform="rotate(-12, 55, 50)">
        <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
        <!-- spiky hair -->
        <path d="M37 42 Q40 24 55 24 Q70 24 73 42 Q70 30 65 30 L62 38 Q55 28 48 38 L45 30 Q40 30 37 42 Z" fill="#1a1a1a"/>
        <!-- eyes (looking up, oval high) -->
        <ellipse cx="49" cy="46" rx="1.7" ry="2.2" fill="#1a1a1a"/>
        <ellipse cx="61" cy="46" rx="1.7" ry="2.2" fill="#1a1a1a"/>
        <!-- mouth small o -->
        <ellipse cx="55" cy="58" rx="1.5" ry="2" fill="#1a1a1a"/>
      </g>
      <!-- little sparkle for "dreaming" feel -->
      <circle cx="84" cy="34" r="2" fill="#f4a825"/>
      <circle cx="92" cy="42" r="1.5" fill="#f4a825"/>
    </svg>""",
    # 3: Student walking with books
    """<svg viewBox="0 0 110 180" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <ellipse cx="55" cy="172" rx="28" ry="4" fill="#000" opacity=".35"/>
      <!-- head -->
      <circle cx="55" cy="48" r="18" fill="#f0d9b5"/>
      <!-- short hair -->
      <path d="M37 44 Q39 30 55 28 Q71 30 73 44 Q72 36 66 34 Q60 30 55 30 Q50 30 44 34 Q38 36 37 44 Z" fill="#3a2a1a"/>
      <!-- eyes -->
      <ellipse cx="49" cy="50" rx="1.7" ry="2.2" fill="#1a1a1a"/>
      <ellipse cx="61" cy="50" rx="1.7" ry="2.2" fill="#1a1a1a"/>
      <!-- mouth confident -->
      <path d="M50 58 Q55 62 60 58" stroke="#1a1a1a" stroke-width="1.4" fill="none" stroke-linecap="round"/>
      <!-- body -->
      <path d="M40 70 Q55 64 70 70 L72 128 Q55 132 38 128 Z" fill="#e2e8f0"/>
      <!-- left arm holding books to chest -->
      <path d="M40 76 L36 104 L52 108 L52 88 Z" fill="#e2e8f0"/>
      <!-- books -->
      <rect x="38" y="96" width="22" height="16" fill="#0d2233"/>
      <rect x="38" y="100" width="22" height="2" fill="#f4a825"/>
      <rect x="38" y="106" width="22" height="2" fill="#0d7377"/>
      <!-- right arm swinging -->
      <path d="M70 74 L78 102 L74 104 L66 78 Z" fill="#e2e8f0"/>
      <!-- legs (walking, one forward) -->
      <path d="M44 128 L42 168 L52 168 L52 130 Z" fill="#334155"/>
      <path d="M58 130 L62 168 L70 166 L66 128 Z" fill="#334155"/>
      <ellipse cx="47" cy="168" rx="7" ry="3" fill="#1a2a3a"/>
      <ellipse cx="65" cy="167" rx="7" ry="3" fill="#1a2a3a"/>
    </svg>""",
]


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<meta name="description" content="__META_DESC_EN__">
<meta name="description" lang="th" content="__META_DESC_TH__">
<link rel="canonical" href="__EVENTS_PAGE__">

<!-- Open Graph -->
<meta property="og:type" content="website">
<meta property="og:url" content="__SITE_URL__">
<meta property="og:title" content="__PAGE_TITLE__">
<meta property="og:description" content="__META_DESC_EN__">
<meta property="og:image" content="__OG_IMAGE__">
<meta property="og:locale" content="en_US">
<meta property="og:locale:alternate" content="th_TH">

<!-- Twitter card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="__PAGE_TITLE__">
<meta name="twitter:description" content="__META_DESC_EN__">
<meta name="twitter:image" content="__OG_IMAGE__">

<!-- JSON-LD structured data: one Event object per upcoming event -->
<script type="application/ld+json">
__JSON_LD__
</script>
<style>
  :root {
    --teal: #0d7377;
    --teal-dark: #095a5d;
    --gold: #f4a825;
    --ink: #0d2233;
    --bg: #f5f7f9;
    --card: #ffffff;
    --text: #1a2530;
    --muted: #5d6b78;
    --border: #e2e8ed;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; }

  /* ── Header ── */
  .site-header { background: var(--teal); color: #fff; }
  .header-bar { max-width: 1180px; margin: 0 auto; padding: 1rem 1.5rem;
                display: flex; align-items: center; justify-content: space-between;
                gap: 1.5rem; flex-wrap: wrap; }
  .brand { font-size: 1.6rem; font-weight: 800; letter-spacing: -.02em; }
  .brand .gold { color: var(--gold); }
  .nav { display: flex; gap: .25rem; flex-wrap: wrap; }
  .nav a { color: rgba(255,255,255,.85); text-decoration: none;
           padding: .5rem .9rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
           transition: background .15s, color .15s; }
  .nav a:hover { background: rgba(255,255,255,.12); color: #fff; }
  .nav a.active { background: rgba(255,255,255,.18); color: #fff; font-weight: 600; }

  .hero { background: var(--teal-dark); color: #fff; padding: 2rem 1.5rem 2.2rem; }
  .hero-inner { max-width: 1180px; margin: 0 auto; }
  .hero h1 { font-size: 1.9rem; font-weight: 700; margin-bottom: .35rem; }
  .hero p { opacity: .85; font-size: .95rem; }

  /* ── Filters ── */
  .filters { background: #fff; border-bottom: 1px solid var(--border);
             padding: 1rem 1.5rem; position: sticky; top: 0; z-index: 10; }
  .filters-inner { max-width: 1180px; margin: 0 auto;
                   display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
  .filter-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em;
                  color: var(--muted); margin-right: .5rem; font-weight: 600; }
  .filter-context { font-size: .85rem; color: var(--muted); margin-right: .5rem; }
  .chip { background: #f0f2f5; color: var(--text); border: 1px solid transparent;
          padding: .45rem .95rem; border-radius: 20px; font-size: .85rem;
          font-weight: 500; cursor: pointer; transition: all .15s;
          display: inline-flex; align-items: center; }
  .chip:hover { background: #e5e9ee; }
  .chip.active { background: var(--teal); color: #fff; border-color: var(--teal); }
  .chip .count { opacity: .7; font-size: .75rem; margin-left: .3rem; font-weight: 400; }
  .chip.active .count { opacity: .9; }

  /* Online toggle — separate from the exclusive filter group */
  .chip.toggle { margin-left: auto; border: 1px dashed #b8c2cd;
                 background: #fff; color: var(--muted); }
  .chip.toggle:hover { background: #f0f2f5; }
  .chip.toggle.active { background: var(--gold); color: #fff;
                        border-color: var(--gold); border-style: solid; }
  .filter-divider { width: 1px; align-self: stretch; background: var(--border);
                    margin: 0 .3rem; }
  @media (max-width: 640px) {
    .chip.toggle { margin-left: 0; }
    .filter-divider { display: none; }
  }

  /* ── Main ── */
  main { max-width: 1180px; margin: 0 auto; padding: 1.5rem; }

  /* Week section */
  .week-section { margin-bottom: 2.2rem; }
  .week-header { font-size: .8rem; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .12em; color: var(--muted);
                 padding: .5rem 0 .9rem; border-bottom: 2px solid var(--border);
                 margin-bottom: 1rem; }

  /* ── Event card ── */
  .card { background: var(--card); border-radius: 10px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(13,34,51,.06), 0 1px 2px rgba(13,34,51,.04);
          margin-bottom: 1rem; display: flex; position: relative;
          border-left: 4px solid var(--gold);
          transition: box-shadow .15s, transform .15s; }
  .card:hover { box-shadow: 0 4px 12px rgba(13,34,51,.1), 0 2px 4px rgba(13,34,51,.06);
                transform: translateY(-1px); }

  /* Logo column (left) */
  .logo-col { width: 80px; min-width: 80px; background: #f7f9fa;
              display: flex; align-items: center; justify-content: center;
              padding: .5rem; border-right: 1px solid var(--border); }
  .logo-col img { max-width: 60px; max-height: 60px; object-fit: contain; display: block; }
  /* White-on-transparent logos get a teal circle behind them (same family as initials-avatar) */
  .logo-col img.needs-bg { width: 52px; height: 52px; max-width: 52px; max-height: 52px;
                           background: var(--teal); border-radius: 50%;
                           padding: 4px; object-fit: contain; }
  .initials-avatar {
    width: 52px; height: 52px; border-radius: 50%;
    background: var(--teal); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.05rem; font-weight: 700; letter-spacing: .03em;
    line-height: 1;
  }

  /* Body / details (centre) */
  .body-col { flex: 1; padding: 1.1rem 1.3rem 1.2rem; min-width: 0; }
  .logo-inline { display: none; align-items: center; gap: .55rem; margin-bottom: .4rem; }
  .logo-inline img { max-height: 28px; max-width: 90px; object-fit: contain; display: block; }
  .logo-inline img.needs-bg { width: 28px; height: 28px; max-width: 28px;
                              background: var(--teal); border-radius: 50%; padding: 4px; }
  .logo-inline .initials-avatar { width: 28px; height: 28px; font-size: .72rem; }

  /* Character column (right) */
  .char-col { width: 120px; min-width: 120px; background: var(--ink);
              overflow: hidden; position: relative; }
  .char-col img,
  .char-col svg { width: 100%; height: 100%; object-fit: cover;
                  object-position: top center; display: block;
                  transform: scaleX(-1); /* face left, towards the event details */ }
  .organizer { font-size: .78rem; font-weight: 700; color: var(--teal);
               text-transform: uppercase; letter-spacing: .06em; margin-bottom: .25rem; }
  .event-name { font-size: 1.05rem; font-weight: 700; color: var(--text);
                margin-bottom: .65rem; line-height: 1.35; }
  .meta { display: flex; gap: .4rem; flex-wrap: wrap; margin-bottom: .9rem; }
  .pill { display: inline-flex; align-items: center; gap: .35rem;
          background: #f0f2f5; padding: .3rem .65rem; border-radius: 14px;
          font-size: .78rem; color: var(--muted); }
  .pill svg { width: 12px; height: 12px; flex-shrink: 0; }

  .card-actions { display: flex; gap: .6rem; align-items: center; }
  .btn-register { background: var(--teal); color: #fff; text-decoration: none;
                  padding: .5rem 1.1rem; border-radius: 6px; font-size: .85rem;
                  font-weight: 600; transition: background .15s; display: inline-block; }
  .btn-register:hover { background: var(--teal-dark); }
  .btn-register.disabled { background: #cbd5d8; color: #fff; cursor: not-allowed; pointer-events: none; }

  /* Empty state */
  .empty { background: #fff; border-radius: 10px; padding: 3rem 2rem;
           text-align: center; color: var(--muted);
           border: 1px dashed var(--border); }
  .empty h3 { color: var(--text); margin-bottom: .5rem; font-size: 1.1rem; }

  .footer-meta { color: var(--muted); font-size: .78rem; text-align: center;
                 padding: 1.5rem 1rem 1rem; }

  /* About section (above the LINE banner) */
  .site-about { max-width: 1180px; margin: 0 auto;
                padding: 1.5rem 1.5rem 2rem; color: var(--muted);
                font-size: .9rem; line-height: 1.6; text-align: center; }
  .site-about p { margin: .25rem 0; }
  .site-about .th { color: var(--text); font-weight: 500; }

  /* LINE OA sticky banner */
  body { padding-bottom: 70px; }  /* room for the fixed banner */
  .line-banner { position: fixed; left: 0; right: 0; bottom: 0;
                 background: var(--teal); color: var(--gold);
                 padding: .85rem 1rem; z-index: 50;
                 box-shadow: 0 -2px 8px rgba(13,34,51,.15);
                 display: flex; align-items: center; justify-content: center; gap: .65rem; }
  .line-banner a { color: var(--gold); text-decoration: none; font-weight: 600; }
  .line-banner a:hover { text-decoration: underline; }
  .line-banner-text { font-size: .95rem; }
  .line-banner-handle { background: rgba(244, 168, 37, .15);
                        border: 1px solid rgba(244, 168, 37, .35);
                        padding: .15rem .55rem; border-radius: 14px;
                        font-size: .82rem; margin-left: .2rem; }
  .line-icon { width: 22px; height: 22px; flex-shrink: 0; }
  @media (max-width: 640px) {
    .line-banner { padding: .65rem .8rem; gap: .4rem; flex-wrap: wrap; }
    .line-banner-text { font-size: .85rem; text-align: center; }
    body { padding-bottom: 95px; }
  }

  /* Mobile */
  @media (max-width: 640px) {
    .header-bar { padding: .8rem 1rem; }
    .brand { font-size: 1.4rem; }
    .hero { padding: 1.5rem 1rem 1.7rem; }
    .hero h1 { font-size: 1.5rem; }
    .filters { padding: .8rem 1rem; }
    main { padding: 1rem; }
    .card { flex-direction: column; }
    .logo-col { display: none; }
    .char-col { width: 100%; min-width: 0; height: 200px; order: -1; }
    .char-col img,
    .char-col svg { width: 100%; height: 100%; object-fit: cover; object-position: top center; }
    .body-col { padding: 1rem 1.1rem 1.1rem; }
    .logo-inline { display: inline-flex; }
  }
</style>
</head>
<body>

<header class="site-header">
  <div class="header-bar">
    <div class="brand">studyevent<span class="gold">z</span></div>
    <nav class="nav">
      <a href="reports/market_intelligence.html">Market Intelligence</a>
      <a href="events.html" class="active">Events</a>
      <a href="reports/thailand_agents.html">Thailand Agents</a>
    </nav>
  </div>
</header>

<section class="hero">
  <div class="hero-inner">
    <h1>Study Abroad Events in Thailand</h1>
    <p id="hero-sub">Find upcoming university events, education fairs, webinars, and study abroad briefings across Thailand, all in one place.<br><br>Updated weekly with events happening in the next 30 days. Follow StudyEventz on LINE to get weekly event updates.</p>
  </div>
</section>

<div class="filters" id="filters">
  <div class="filters-inner">
    <span class="filter-label">Filter</span>
    <span class="filter-context">Events in the next 30 days</span>
    <button class="chip active" data-filter="all">All <span class="count" data-count="all">0</span></button>
    <button class="chip" data-filter="australia">Australia <span class="count" data-count="australia">0</span></button>
    <button class="chip" data-filter="uk">UK <span class="count" data-count="uk">0</span></button>
    <button class="chip" data-filter="bangkok">Bangkok <span class="count" data-count="bangkok">0</span></button>
    <span class="filter-divider"></span>
    <button class="chip toggle" id="online-toggle" aria-pressed="false">
      <span class="label">+ Online</span>
      <span class="count" data-count="online">0</span>
    </button>
  </div>
</div>

<main id="event-root">
  <div class="empty"><h3>Loading…</h3></div>
</main>

<div class="footer-meta" id="footer-meta"></div>

<section class="site-about">
  <p class="th">studyeventz รวบรวมงาน study abroad จากบริษัทแนะแนวทั่วประเทศไทย อัปเดตทุกวันจันทร์</p>
  <p>studyeventz aggregates study abroad events from consultancies across Thailand. Updated every Monday.</p>
</section>

<aside class="line-banner" role="contentinfo" aria-label="LINE Official Account">
  <svg class="line-icon" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
    <path d="M12 3C6.48 3 2 6.62 2 11.07c0 4 3.66 7.34 8.6 7.96.33.07.78.22.9.51.1.26.06.66.03.93 0 0-.12.71-.14.86-.04.26-.2 1.01.88.55 1.09-.46 5.86-3.45 7.99-5.91h.01C21.42 14.31 22 12.77 22 11.07 22 6.62 17.52 3 12 3zM7.92 13.5H6.04a.4.4 0 01-.4-.4V9.34a.4.4 0 11.8 0v3.36h1.48a.4.4 0 110 .8zm1.66-.4a.4.4 0 11-.8 0V9.34a.4.4 0 11.8 0v3.76zm4.4 0a.4.4 0 01-.32.39c-.04.01-.07.01-.11.01a.4.4 0 01-.32-.16l-1.76-2.4v2.16a.4.4 0 11-.8 0V9.34a.4.4 0 01.32-.39c.04-.01.07-.01.11-.01a.4.4 0 01.32.16l1.76 2.4V9.34a.4.4 0 11.8 0v3.76zm2.74-2.28a.4.4 0 110 .8h-1.04v.68h1.04a.4.4 0 110 .8h-1.44a.4.4 0 01-.4-.4V9.34a.4.4 0 01.4-.4h1.44a.4.4 0 110 .8h-1.04v.68h1.04z"/>
  </svg>
  <span class="line-banner-text">รับการแจ้งเตือนงานใหม่ทุกสัปดาห์ → ติดตามเราบน LINE</span>
  <a id="line-link" href="#" target="_blank" rel="noopener">
    <span class="line-banner-handle" id="line-handle">__LINE_HANDLE__</span>
  </a>
</aside>

<script>
// LINE OA — change LINE_HANDLE here (or in build_events_page.py) to update everywhere
const LINE_HANDLE = "__LINE_HANDLE__";
(function setLineBanner() {
  const el = document.getElementById('line-link');
  const handleEl = document.getElementById('line-handle');
  if (el) {
    const cleanHandle = LINE_HANDLE.replace(/^@/, '');
    el.href = `https://line.me/R/ti/p/@${cleanHandle}`;
  }
  if (handleEl) handleEl.textContent = LINE_HANDLE;
})();

const CHARACTERS = __CHARACTERS_JSON__;

function characterMarkup(entry) {
  // String entries are image URLs; objects with .svg are inline fallbacks.
  if (typeof entry === 'string') {
    return `<img src="${entry}" alt="" loading="lazy">`;
  }
  return entry.svg;
}

function logoMarkup(ev) {
  if (ev.logo_url) {
    const cls = ev.logo_needs_bg ? ' class="needs-bg"' : '';
    const style = ev.logo_bg_color ? ` style="background: ${ev.logo_bg_color}"` : '';
    return `<img${cls}${style} src="${ev.logo_url}" alt="${escapeHTML(ev.agent_name)} logo" loading="lazy">`;
  }
  return `<span class="initials-avatar" aria-hidden="true">${escapeHTML(ev.initials || '?')}</span>`;
}

// SVG icons for meta pills
const ICONS = {
  date: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5 1v1H3a1 1 0 00-1 1v10a1 1 0 001 1h10a1 1 0 001-1V3a1 1 0 00-1-1h-2V1h-1v1H6V1H5zm-2 4h10v8H3V5z"/></svg>',
  time: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 2a5 5 0 110 10 5 5 0 010-10zm-.5 2v3.25l2.5 1.5-.5.85L7 8.25V5h.5z"/></svg>',
  location: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a5 5 0 00-5 5c0 3.5 5 9 5 9s5-5.5 5-9a5 5 0 00-5-5zm0 3a2 2 0 110 4 2 2 0 010-4z"/></svg>'
};

function fmtDateRange(start) {
  const opts = { day: 'numeric', month: 'short' };
  const end = new Date(start); end.setDate(end.getDate() + 6);
  const s = start.toLocaleDateString('en-GB', opts);
  const e = end.toLocaleDateString('en-GB', opts);
  return `Week of ${s.replace(' ','–').split('–')[0]}–${e.replace(' ',' ')}`;
}

function startOfWeek(dateStr) {
  // ISO week: Monday start
  const d = new Date(dateStr + 'T00:00:00');
  const day = (d.getDay() + 6) % 7; // 0=Mon
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
}

function weekLabel(monday) {
  const sunday = new Date(monday); sunday.setDate(sunday.getDate() + 6);
  const mDay = monday.getDate();
  const sDay = sunday.getDate();
  const mMonth = monday.toLocaleDateString('en-GB', { month: 'short' });
  const sMonth = sunday.toLocaleDateString('en-GB', { month: 'short' });
  if (mMonth === sMonth) return `Week of ${mDay}–${sDay} ${mMonth}`;
  return `Week of ${mDay} ${mMonth} – ${sDay} ${sMonth}`;
}

function fmtEventDate(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}

function escapeHTML(s) {
  return (s || '').replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

function destinationCountry(ev) {
  // Infer Australia/UK from event name + location.
  const hay = (ev.name + ' ' + ev.location).toLowerCase();
  const auKeys = ['australia', 'sydney', 'melbourne', 'brisbane', 'perth', 'adelaide',
                  'canberra', 'macquarie', 'monash', 'unsw', 'australian'];
  const ukKeys = ['united kingdom', ' uk ', 'uk:', ' uk,', 'london', 'manchester',
                  'edinburgh', 'oxford', 'cambridge', 'britain', 'british'];
  if (auKeys.some(k => hay.includes(k))) return 'australia';
  if (ukKeys.some(k => hay.includes(k))) return 'uk';
  return null;
}

function isOnline(ev) {
  return (ev.location || '').toLowerCase().includes('online');
}

function matchesInPerson(ev, filter) {
  // Apply only the in-person filter. Online events do NOT match here — they're
  // routed through the Online toggle instead.
  if (isOnline(ev)) return false;
  if (filter === 'all') return true;
  if (filter === 'australia' || filter === 'uk') return destinationCountry(ev) === filter;
  if (filter === 'bangkok') return (ev.location || '').toLowerCase().includes('bangkok');
  return false;
}

function eventVisible(ev, filter, showOnline) {
  if (isOnline(ev)) return showOnline;
  return matchesInPerson(ev, filter);
}

function renderCard(ev, idx) {
  const charSvg = characterMarkup(CHARACTERS[idx % CHARACTERS.length]);
  const logo = logoMarkup(ev);
  const regUrl = ev.registration_url || '';
  const btn = regUrl
    ? `<a class="btn-register" href="${escapeHTML(regUrl)}" target="_blank" rel="noopener">Register →</a>`
    : `<span class="btn-register disabled">No link</span>`;
  const datePill = `<span class="pill">${ICONS.date}${escapeHTML(fmtEventDate(ev.date))}</span>`;
  const timePill = ev.time ? `<span class="pill">${ICONS.time}${escapeHTML(ev.time)}</span>` : '';
  const locPill = ev.location ? `<span class="pill">${ICONS.location}${escapeHTML(ev.location)}</span>` : '';
  return `
    <article class="card">
      <div class="logo-col">${logo}</div>
      <div class="body-col">
        <div class="logo-inline">${logo}</div>
        <div class="organizer">${escapeHTML(ev.organizer)}</div>
        <div class="event-name">${escapeHTML(ev.name)}</div>
        <div class="meta">${datePill}${timePill}${locPill}</div>
        <div class="card-actions">${btn}</div>
      </div>
      <div class="char-col">${charSvg}</div>
    </article>
  `;
}

function groupByWeek(events) {
  const groups = new Map();
  for (const ev of events) {
    const monday = startOfWeek(ev.date);
    const key = monday.toISOString().slice(0, 10);
    if (!groups.has(key)) groups.set(key, { monday, events: [] });
    groups.get(key).events.push(ev);
  }
  return [...groups.values()].sort((a, b) => a.monday - b.monday);
}

function render(events, filter, showOnline) {
  const root = document.getElementById('event-root');
  const filtered = events.filter(e => eventVisible(e, filter, showOnline));
  if (filtered.length === 0) {
    const hasOnlineHidden = events.some(isOnline) && !showOnline;
    root.innerHTML = `
      <div class="empty">
        <h3>No upcoming events</h3>
        <p>${filter === 'all'
            ? 'No in-person events found in the next 30 days.' + (hasOnlineHidden ? ' Toggle <strong>+ Online</strong> to see online events.' : '')
            : 'No in-person matches for this filter.' + (hasOnlineHidden ? ' Toggle <strong>+ Online</strong> to include online events.' : '')}</p>
      </div>`;
    return;
  }
  const groups = groupByWeek(filtered);
  let idx = 0;
  root.innerHTML = groups.map(g => `
    <section class="week-section">
      <h2 class="week-header">${weekLabel(g.monday)}</h2>
      ${g.events.map(ev => renderCard(ev, idx++)).join('')}
    </section>
  `).join('');
}

function updateCounts(events) {
  // In-person counts ignore online events (online has its own additive toggle)
  const inPersonFilters = ['all', 'australia', 'uk', 'bangkok'];
  for (const f of inPersonFilters) {
    const n = events.filter(e => matchesInPerson(e, f)).length;
    const el = document.querySelector(`[data-count="${f}"]`);
    if (el) el.textContent = n;
  }
  const onlineN = events.filter(isOnline).length;
  const onlineEl = document.querySelector('[data-count="online"]');
  if (onlineEl) onlineEl.textContent = onlineN;
}

let CURRENT_FILTER = 'all';
let SHOW_ONLINE = false;
let EVENTS = [];

function setOnlineToggle(on) {
  SHOW_ONLINE = on;
  const btn = document.getElementById('online-toggle');
  if (!btn) return;
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', String(on));
  const label = btn.querySelector('.label');
  if (label) label.textContent = on ? 'Online ✓' : '+ Online';
}

document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.chip');
  if (!btn) return;
  if (btn.id === 'online-toggle') {
    setOnlineToggle(!SHOW_ONLINE);
  } else {
    document.querySelectorAll('.chip:not(.toggle)').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    CURRENT_FILTER = btn.dataset.filter;
  }
  render(EVENTS, CURRENT_FILTER, SHOW_ONLINE);
});

fetch('data/events.json', { cache: 'no-store' })
  .then(r => {
    if (!r.ok) throw new Error('Failed to load events data');
    return r.json();
  })
  .then(data => {
    EVENTS = data.events || [];
    updateCounts(EVENTS);
    render(EVENTS, CURRENT_FILTER, SHOW_ONLINE);
    const meta = document.getElementById('footer-meta');
    if (data.generated_at) {
      meta.textContent = `${EVENTS.length} event${EVENTS.length === 1 ? '' : 's'} · Updated ${new Date(data.generated_at).toLocaleString('en-GB')}`;
    }
  })
  .catch(err => {
    document.getElementById('event-root').innerHTML = `
      <div class="empty">
        <h3>Could not load events</h3>
        <p>${escapeHTML(err.message)}</p>
        <p style="margin-top:.5rem;font-size:.85rem">If you're viewing this locally, serve via <code>python -m http.server</code> rather than <code>file://</code>.</p>
      </div>`;
  });
</script>

</body>
</html>
"""


def build_html() -> tuple[int, str]:
    """Render events.html. Returns (count, mode) where mode is 'png' or 'svg-fallback'."""
    images = discover_character_images()
    if images:
        characters = images
        mode = "png"
        # Pick the first character as the absolute og:image URL
        og_image = f"{SITE_URL}/{images[0]}"
    else:
        characters = [{"svg": s} for s in CHARACTER_SVGS]
        mode = "svg-fallback"
        og_image = f"{SITE_URL}/events.html"

    # Load the freshly-written events.json so the JSON-LD reflects this build
    try:
        events_data = json.loads(JSON_OUT.read_text(encoding="utf-8")).get("events", [])
    except Exception:
        events_data = []
    json_ld = build_event_json_ld(events_data)

    replacements = {
        "__PAGE_TITLE__":      PAGE_TITLE,
        "__META_DESC_EN__":    META_DESC_EN,
        "__META_DESC_TH__":    META_DESC_TH,
        "__EVENTS_PAGE__":     EVENTS_PAGE,
        "__SITE_URL__":        SITE_URL,
        "__OG_IMAGE__":        og_image,
        "__LINE_HANDLE__":     LINE_HANDLE,
        "__JSON_LD__":         json_ld,
        "__CHARACTERS_JSON__": json.dumps(characters),
    }
    html = HTML
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    HTML_OUT.write_text(html, encoding="utf-8")
    return len(characters), mode


def ensure_pngquant() -> bool:
    """Return True if pngquant is on PATH, installing via brew if needed."""
    if shutil.which("pngquant"):
        return True
    if shutil.which("brew") is None:
        print(
            "ERROR: pngquant not found and Homebrew is unavailable to install it. "
            "Install pngquant manually and retry.",
            file=sys.stderr,
        )
        return False
    print("pngquant not found — installing via Homebrew …", flush=True)
    result = subprocess.run(["brew", "install", "pngquant"])
    if result.returncode != 0:
        print("ERROR: 'brew install pngquant' failed.", file=sys.stderr)
        return False
    return shutil.which("pngquant") is not None


def _compress_png(png: Path) -> tuple[int, int, int]:
    """Run pngquant on one PNG. Returns (before_bytes, after_bytes, exit_code)."""
    before = png.stat().st_size
    # --quality=70: max quality 70 (best-effort, may exit 99 if it can't hit it)
    # --skip-if-larger: keep original if compression made it bigger
    # --force --ext .png: overwrite the same filename atomically
    result = subprocess.run(
        [
            "pngquant",
            "--quality=70",
            "--skip-if-larger",
            "--force",
            "--ext", ".png",
            str(png),
        ],
        capture_output=True,
        text=True,
    )
    after = png.stat().st_size
    # Exit codes:
    #   0  = success
    #   98 = cannot save (typically: result would be larger than input — original kept by --skip-if-larger)
    #   99 = couldn't meet quality target — original kept
    # Treat all three as success; anything else is a real failure.
    if result.returncode not in (0, 98, 99):
        print(
            f"  {png.name}: pngquant failed (exit {result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
    return before, after, result.returncode


def optimize_images() -> None:
    """Run pngquant on every PNG under assets/characters/ and assets/logos/,
    overwriting in place. SVGs are left alone (pngquant only handles PNGs)."""
    targets: list[tuple[str, Path, list[Path]]] = []
    if CHARACTERS_DIR.exists():
        pngs = sorted(CHARACTERS_DIR.rglob("*.png"), key=lambda p: _natural_key(p.name))
        targets.append(("assets/characters/", CHARACTERS_DIR, pngs))
    if LOGOS_DIR.exists():
        pngs = sorted(LOGOS_DIR.rglob("*.png"), key=lambda p: _natural_key(p.name))
        targets.append(("assets/logos/", LOGOS_DIR, pngs))

    total_pngs = sum(len(pngs) for _, _, pngs in targets)
    if total_pngs == 0:
        print("No PNGs found in assets/characters/ or assets/logos/ — nothing to optimize.")
        return
    if not ensure_pngquant():
        sys.exit(1)

    print(f"Optimizing {total_pngs} PNG(s) with pngquant (target quality 70) …")
    grand_before = grand_after = 0
    for label, root, pngs in targets:
        if not pngs:
            continue
        print(f"\n  [{label}] {len(pngs)} file(s):")
        section_before = section_after = 0
        for png in pngs:
            before, after, code = _compress_png(png)
            rel = png.relative_to(root)
            pct = (1 - after / before) * 100 if before else 0.0
            if code == 99:
                note = " (kept original — couldn't reach quality target)"
            elif code == 98:
                note = " (already optimized — no further gain)"
            else:
                note = ""
            print(f"    {rel}: {before // 1024} KB → {after // 1024} KB ({pct:+.0f}%){note}")
            section_before += before
            section_after += after
        if section_before:
            pct = (1 - section_after / section_before) * 100
            print(f"    Subtotal: {section_before // 1024} KB → {section_after // 1024} KB ({pct:+.0f}%)")
        grand_before += section_before
        grand_after += section_after
    if grand_before:
        pct = (1 - grand_after / grand_before) * 100
        print(f"\n  Grand total: {grand_before // 1024} KB → {grand_after // 1024} KB ({pct:+.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--optimize",
        action="store_true",
        help="Run pngquant on assets/characters/ before building (overwrites in place).",
    )
    args = ap.parse_args()

    if args.optimize:
        optimize_images()

    n = export_events_json()
    char_count, mode = build_html()
    write_seo_files()
    print(f"Wrote {JSON_OUT} ({n} events)")
    print(f"Wrote {HTML_OUT} ({char_count} characters, mode={mode})")
    print(f"Wrote {SITEMAP_OUT} and {ROBOTS_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
