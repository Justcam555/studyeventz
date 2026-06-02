#!/usr/bin/env python3
"""
enrich_agents.py — Deep Apify enrichment for Thailand + Nepal agents.

Phases:
  1. Migrate DB      — add extended columns to agent_social
  2. TikTok discovery — re-scan active websites for tiktok.com/@handle
  3. Apify scraping   — IG, TikTok, YouTube, LinkedIn (where handles exist)
  4. Rebuild HTML     — regenerate agent-profile.html with rich data

Actors used (same as one-education-report.html):
  Instagram  : apify/instagram-scraper
  TikTok     : clockworks/tiktok-scraper
  YouTube    : streamers/youtube-scraper
  LinkedIn   : bebity/linkedin-company-scraper

Usage:
  python3 enrich_agents.py                      # Thailand then Nepal
  python3 enrich_agents.py --country Thailand
  python3 enrich_agents.py --refresh            # re-scrape already done
  python3 enrich_agents.py --report-only        # rebuild HTML, no scraping
  python3 enrich_agents.py --discover-only      # phase 2 only (TikTok discovery)
"""

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests
from apify_client import ApifyClient
from bs4 import BeautifulSoup

DB_PATH    = Path(__file__).parent / "data" / "agents.db"
PROFILE_HTML = Path("/Users/camtest/Desktop/marketintelligencereports/agent-profile.html")
PLACES_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
COUNTRIES  = ["Thailand", "Nepal", "Cambodia", "Vietnam", "Indonesia", "Sri Lanka"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── DB migration ──────────────────────────────────────────────────────────────

NEW_COLUMNS = [
    # LINE OA
    ("line_oa_handle",       "TEXT"),
    ("line_oa_friends",      "INTEGER"),
    ("line_oa_verified",     "TEXT"),
    # TikTok
    ("tiktok_handle",        "TEXT"),
    ("tiktok_url",           "TEXT"),
    ("tiktok_followers",     "INTEGER"),
    ("tiktok_video_count",   "INTEGER"),
    ("tiktok_total_views",   "INTEGER"),
    ("tiktok_top_video_views", "INTEGER"),
    ("tiktok_avg_views",     "REAL"),
    ("tiktok_engagement_rate", "REAL"),
    ("tiktok_last_post",     "TEXT"),
    ("tiktok_videos",        "TEXT"),   # JSON
    # Instagram extras
    ("ig_post_count",        "INTEGER"),
    ("ig_last_post",         "TEXT"),
    # YouTube
    ("yt_channel_name",      "TEXT"),
    ("yt_channel_url",       "TEXT"),
    ("yt_subscribers",       "INTEGER"),
    ("yt_total_views",       "INTEGER"),
    ("yt_video_count",       "INTEGER"),
    ("yt_top_video_title",   "TEXT"),
    ("yt_top_video_views",   "INTEGER"),
    ("yt_videos",            "TEXT"),   # JSON
    # LinkedIn extras
    ("li_employee_count",    "INTEGER"),
    # Timestamp for Apify enrichment
    ("platform_enriched_at", "DATETIME"),
]


def migrate_db(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(agent_social)").fetchall()}
    for col, dtype in NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE agent_social ADD COLUMN {col} {dtype}")
    conn.commit()
    print("✓ DB migration complete")


# ── Agent fetch ───────────────────────────────────────────────────────────────

def get_agents(conn, country, refresh=False):
    extra = "" if refresh else "AND (s.platform_enriched_at IS NULL)"
    return conn.execute(f"""
        SELECT s.id, s.agent_id, s.canonical_name, s.country,
               s.website_url, s.website_active,
               s.instagram_handle, s.instagram_url,
               s.linkedin_url, s.tiktok_handle, s.tiktok_url,
               s.google_rating, s.google_reviews, s.google_maps_url,
               s.presence_score, a.city, a.email, a.phone,
               s.facebook_url, s.line_oa_handle
        FROM agent_social s
        JOIN agents a ON a.id = s.agent_id
        WHERE LOWER(s.country) = LOWER(?)
          {extra}
        ORDER BY s.presence_score DESC, s.canonical_name
    """, (country,)).fetchall()


def upsert(conn, social_id, data):
    sets = ", ".join(f"{k}=?" for k in data)
    conn.execute(
        f"UPDATE agent_social SET {sets} WHERE id=?",
        list(data.values()) + [social_id]
    )
    conn.commit()


# ── Phase 1: TikTok handle discovery ─────────────────────────────────────────

TIKTOK_PAT = re.compile(
    r"(?:https?://)?(?:www\.)?tiktok\.com/@([\w.]+)", re.I
)
JUNK_TT = {"foryou", "explore", "following", "trending", "discover"}

# LINE OA patterns
LINE_PAGE_PAT = re.compile(
    r"(?:https?://)?page\.line\.me/([\w.@%-]+)", re.I
)
LINE_TI_PAT = re.compile(
    r"(?:https?://)?line\.me/(?:R/)?ti/p/@?([\w.%-]+)", re.I
)
JUNK_LINE = {"download", "invite", "social", "PC", "pc", "about", "en"}


def discover_line_oa(html):
    """Extract LINE OA handle from page HTML. Returns (handle, full_url) or (None, None)."""
    if not html:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    hrefs = " ".join(a.get("href", "") for a in soup.find_all("a", href=True))
    text = hrefs + " " + soup.get_text(" ")
    for pat in (LINE_PAGE_PAT, LINE_TI_PAT):
        for m in pat.finditer(text):
            from urllib.parse import unquote
            handle = unquote(m.group(1)).strip("/").split("?")[0].lstrip("@")
            if handle in JUNK_LINE or len(handle) < 3:
                continue
            full = f"https://page.line.me/{handle}"
            return handle, full
    return None, None


def scrape_line_oa(handle):
    """Fetch page.line.me/{handle}, parse __NEXT_DATA__ for friendCount + badgeType."""
    url = f"https://page.line.me/{handle.lstrip('@')}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return {}
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return {}
        outer = json.loads(m.group(1))
        inner_str = outer["props"]["pageProps"].get("initialDataString", "{}")
        inner = json.loads(inner_str)
        account_info = inner.get("account", {}).get("accountInfo", {})
        profile = inner.get("account", {}).get("profile", {})
        friends = account_info.get("friendCount")
        badge = profile.get("badgeType")  # "certified" / "verified" / None
        return {
            "line_oa_friends": friends,
            "line_oa_verified": badge,
        }
    except Exception as e:
        print(f"    LINE OA scrape error: {e}")
        return {}


def discover_tiktok(url, html):
    """Extract tiktok.com/@handle from page HTML; return (handle, full_url) or (None, None)."""
    if not html:
        return None, None
    hrefs = ""
    soup = BeautifulSoup(html, "lxml")
    hrefs = " ".join(a.get("href", "") for a in soup.find_all("a", href=True))
    text  = hrefs + " " + soup.get_text(" ")
    for m in TIKTOK_PAT.finditer(text):
        handle = m.group(1).strip("/").split("?")[0].lower()
        if handle in JUNK_TT or len(handle) < 3:
            continue
        full = m.group(0)
        if not full.startswith("http"):
            full = "https://www.tiktok.com/@" + handle
        return handle, full
    return None, None


def fetch_page(url):
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


# ── Phase 2: Apify scrapers ───────────────────────────────────────────────────

def scrape_instagram(client, handle):
    """apify/instagram-profile-scraper — purpose-built profile scraper, returns followersCount."""
    handle = handle.lstrip("@")
    run = client.actor("apify/instagram-profile-scraper").call(run_input={
        "usernames": [handle],
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def scrape_instagram_batch(client, handles):
    """Batch version — scrape multiple IG profiles in one Apify run. Returns dict handle→item."""
    clean = [h.lstrip("@") for h in handles if h]
    if not clean:
        return {}
    run = client.actor("apify/instagram-profile-scraper").call(run_input={
        "usernames": clean,
    })
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    return {item.get("username", "").lower(): item for item in items if item.get("username")}


def scrape_facebook(client, fb_url):
    """apify/facebook-pages-scraper — page follower/like count."""
    run = client.actor("apify/facebook-pages-scraper").call(run_input={
        "startUrls": [{"url": fb_url}],
        "maxPosts": 0,
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def scrape_tiktok(client, handle):
    """clockworks/tiktok-scraper — profiles mode, top 10 videos."""
    h = handle if handle.startswith("@") else f"@{handle}"
    run = client.actor("clockworks/tiktok-scraper").call(run_input={
        "profiles": [h],
        "resultsPerPage": 10,
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def scrape_youtube(client, name, city):
    """streamers/youtube-scraper — keyword search, 10 results."""
    query = f"{name} {city or ''} education".strip()
    run = client.actor("streamers/youtube-scraper").call(run_input={
        "searchQueries": [query],
        "maxResults": 10,
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def scrape_linkedin(client, linkedin_url):
    """harvestapi/linkedin-company — company page URL."""
    url = linkedin_url if linkedin_url.startswith("http") else "https://" + linkedin_url
    run = client.actor("harvestapi/linkedin-company").call(run_input={
        "companies": [url],
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


# ── Parse scraped data ────────────────────────────────────────────────────────

def parse_instagram(items):
    """Return dict of IG stats or {}. Works with apify/instagram-profile-scraper output."""
    if not items:
        return {}
    item = items[0]
    # Check for error (profile not found / blocked)
    if item.get("error") or item.get("errorDescription"):
        return {}
    # Some actor versions nest profile data under an "profile" or "data" key
    if not item.get("followersCount") and isinstance(item.get("profile"), dict):
        item = item["profile"]
    elif not item.get("followersCount") and isinstance(item.get("data"), dict):
        item = item["data"]
    # Field name varies across Apify actor versions
    followers = (item.get("followersCount") or item.get("followers")
                 or item.get("followerCount") or item.get("userFollowerCount")
                 or item.get("edge_followed_by", {}).get("count"))
    posts     = (item.get("postsCount") or item.get("mediaCount")
                 or item.get("postCount") or item.get("igtvVideoCount")
                 or item.get("edge_owner_to_timeline_media", {}).get("count"))
    last_post = None
    for lp in (item.get("latestPosts") or item.get("posts") or []):
        ts = lp.get("timestamp") or lp.get("takenAtTimestamp")
        if ts:
            try:
                last_post = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                try:
                    last_post = str(ts)[:10]
                except Exception:
                    pass
            break
    return {
        "instagram_followers": followers,
        "ig_post_count": posts,
        "ig_last_post": last_post,
    }


def parse_facebook(items):
    """Return dict of Facebook page stats or {}."""
    if not items:
        return {}
    item = items[0]
    followers = (item.get("followers") or item.get("followersCount")
                 or item.get("pageFollowers") or item.get("followerCount"))
    likes     = (item.get("likes") or item.get("pageLikes")
                 or item.get("likesCount") or item.get("fanCount"))
    # Prefer followers over likes; fall back to likes if no followers field
    count = followers or likes
    return {
        "facebook_followers": count,
    }


def parse_tiktok(items):
    """Return dict of TikTok stats + videos JSON or {}."""
    if not items:
        return {}
    # Follower count is in authorMeta of any video
    first = items[0]
    meta  = first.get("authorMeta") or {}
    followers  = meta.get("fans") or meta.get("followers")
    vid_count  = meta.get("video") or meta.get("videoCount")

    videos = []
    total_views = 0
    total_engage = 0
    last_post = None
    for it in items:
        views    = it.get("playCount") or it.get("viewsCount") or 0
        likes    = it.get("diggCount") or it.get("likesCount") or 0
        comments = it.get("commentCount") or 0
        shares   = it.get("shareCount") or 0
        caption  = (it.get("text") or it.get("description") or "")[:100]
        ts       = it.get("createTime") or it.get("createTimeISO")
        date_str = None
        if ts:
            try:
                date_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                if not last_post or date_str > last_post:
                    last_post = date_str
            except Exception:
                date_str = str(ts)[:10]
        eng = round((likes + comments) / views * 100, 2) if views > 0 else 0
        total_views  += views
        total_engage += likes + comments
        videos.append({
            "caption": caption,
            "views": views,
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "engagement": eng,
            "date": date_str,
        })

    avg_views = round(total_views / len(videos), 0) if videos else 0
    overall_eng = round(total_engage / total_views * 100, 2) if total_views > 0 else 0
    top_views = max((v["views"] for v in videos), default=0)
    return {
        "tiktok_followers":     followers,
        "tiktok_video_count":   vid_count,
        "tiktok_total_views":   total_views,
        "tiktok_top_video_views": top_views,
        "tiktok_avg_views":     avg_views,
        "tiktok_engagement_rate": overall_eng,
        "tiktok_last_post":     last_post,
        "tiktok_videos":        json.dumps(videos, ensure_ascii=False),
    }


def parse_youtube(items, agent_name):
    """Return dict of YT stats + videos JSON or {}."""
    if not items:
        return {}
    videos = []
    best_channel = None
    best_subs = None  # None = no channel identified yet; 0 = channel found, count unavailable
    agent_lower = agent_name.lower()

    best_channel_url = None
    for it in items:
        title    = it.get("title") or ""
        channel  = it.get("channelName") or (it.get("channel") or {}).get("name") or ""
        views    = it.get("viewCount") or 0
        # Apify streamers/youtube-scraper uses numberOfSubscribers; fallbacks for actor version changes
        subs     = (it.get("numberOfSubscribers") or it.get("channelSubscriberCount")
                    or it.get("subscriberCount") or it.get("subscribers") or 0)
        vid_url  = it.get("url") or it.get("videoUrl") or ""
        ch_url   = it.get("channelUrl") or ""
        date_str = (it.get("date") or it.get("publishedAt") or "")[:10]

        # Identify best matching channel (prefer name overlap)
        chan_lower = channel.lower()
        is_match = any(w in chan_lower for w in agent_lower.split() if len(w) > 3)
        current_best = best_subs or 0
        if is_match or subs > current_best:
            if is_match or not best_channel:
                best_channel = channel
                best_subs = subs  # 0 is valid: channel found but count not returned by Apify
                best_channel_url = ch_url or best_channel_url

        videos.append({
            "title": title[:80],
            "channel": channel,
            "views": views,
            "subscribers": subs,
            "url": vid_url,
            "date": date_str,
            "owned": is_match,
        })

    # Sort by views descending
    videos.sort(key=lambda x: x["views"], reverse=True)
    top = videos[0] if videos else {}

    return {
        "yt_channel_name":     best_channel,
        "yt_channel_url":      best_channel_url or None,
        "yt_subscribers":      best_subs,  # None = no channel found; 0 = found but count unavailable
        "yt_total_views":      sum(v["views"] for v in videos),
        "yt_video_count":      len(videos),
        "yt_top_video_title":  top.get("title"),
        "yt_top_video_views":  top.get("views"),
        "yt_videos":           json.dumps(videos, ensure_ascii=False),
    }


def parse_linkedin(items):
    """Return dict of LinkedIn stats or {}."""
    if not items:
        return {}
    item = items[0]
    # harvestapi returns followerCount, employeeCount
    followers = (item.get("followerCount") or item.get("followersCount")
                 or item.get("followers") or item.get("numberOfFollowers"))
    employees = (item.get("employeeCount") or item.get("staffCount")
                 or item.get("numberOfEmployees") or item.get("employeeCountRange"))
    return {
        "linkedin_followers": followers,
        "li_employee_count":  employees,
    }


# ── Enhanced presence score ───────────────────────────────────────────────────

def calc_score(row):
    """Recalculate presence score with enriched data (0–10)."""
    score = 0.0
    if row.get("website_active"):               score += 2.0
    if row.get("google_rating"):
        score += 1.5
        if (row.get("google_rating") or 0) >= 4.0:  score += 0.5
    if (row.get("google_reviews") or 0) >= 10: score += 0.5
    if row.get("facebook_url"):                 score += 1.5  # placeholder

    # Instagram
    ig_f = row.get("instagram_followers") or 0
    if row.get("instagram_handle"):             score += 0.5
    if ig_f >= 500:                             score += 0.5
    if ig_f >= 2000:                            score += 0.5

    # TikTok
    tt_v = row.get("tiktok_total_views") or 0
    if row.get("tiktok_handle"):                score += 0.5
    if tt_v >= 10000:                           score += 0.5
    if tt_v >= 100000:                          score += 0.5

    # LinkedIn
    if row.get("linkedin_url"):                 score += 0.5
    li_f = row.get("linkedin_followers") or 0
    if li_f >= 500:                             score += 0.5

    # YouTube
    if row.get("yt_channel_name"):              score += 0.5

    # LINE OA
    line_f = row.get("line_oa_friends") or 0
    if row.get("line_oa_handle"):               score += 0.5
    if line_f >= 1000:                          score += 0.5
    if line_f >= 10000:                         score += 0.5

    return min(round(score, 1), 10.0)


# ── Instagram batch enrichment ────────────────────────────────────────────────

def enrich_ig_batch(conn, country, client, refresh=False):
    """Scrape all IG profiles for a country in a single Apify run."""
    rows = conn.execute("""
        SELECT s.id, s.canonical_name, s.instagram_handle, s.instagram_followers
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
          AND s.instagram_handle IS NOT NULL AND TRIM(s.instagram_handle) != ''
        ORDER BY s.canonical_name
    """, (country,)).fetchall()

    # Filter: skip already-enriched unless refresh
    to_scrape = rows if refresh else [r for r in rows if r[3] is None]

    print(f"\n{'='*65}")
    print(f"  {country} Instagram batch — {len(to_scrape)}/{len(rows)} handles to scrape")
    print(f"{'='*65}")

    if not to_scrape:
        print("  Nothing to scrape.")
        return

    handles = [r[2] for r in to_scrape]
    print(f"  Handles: {', '.join('@'+h for h in handles)}")
    print(f"\n  🚀 Launching single Apify run for {len(handles)} profiles…")

    results = scrape_instagram_batch(client, handles)
    print(f"  ✅ Got {len(results)} results back from Apify\n")

    updated = 0
    for sid, name, handle, _ in to_scrape:
        key = handle.lstrip("@").lower()
        item = results.get(key)
        if not item:
            print(f"  ❌ {name[:45]}: no result for @{handle}")
            continue
        parsed = parse_instagram([item])
        if parsed.get("instagram_followers") is not None:
            upsert(conn, sid, parsed)
            f = parsed.get("instagram_followers")
            p = parsed.get("ig_post_count")
            print(f"  ✅ {name[:45]}: {f:,} followers, {p or '?'} posts")
            updated += 1
        else:
            err = item.get("errorDescription") or item.get("error") or "no data"
            print(f"  ⚠️  {name[:45]}: @{handle} — {err}")

    print(f"\n  ✓ Instagram batch complete — {updated}/{len(to_scrape)} updated")


# ── TikTok batch enrichment ───────────────────────────────────────────────────

def enrich_tiktok_batch(conn, country, client, refresh=False):
    """Scrape all TikTok profiles for a country in a single Apify run."""
    rows = conn.execute("""
        SELECT s.id, s.canonical_name, s.tiktok_handle, s.tiktok_followers
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
          AND s.tiktok_handle IS NOT NULL AND TRIM(s.tiktok_handle) != ''
        ORDER BY s.canonical_name
    """, (country,)).fetchall()

    to_scrape = rows if refresh else [r for r in rows if r[3] is None]

    print(f"\n{'='*65}")
    print(f"  {country} TikTok batch — {len(to_scrape)}/{len(rows)} handles to scrape")
    print(f"{'='*65}")

    if not to_scrape:
        print("  Nothing to scrape.")
        return

    handles = [r[2] if r[2].startswith("@") else f"@{r[2]}" for r in to_scrape]
    print(f"  Handles: {', '.join(handles)}")
    print(f"\n  🚀 Launching single Apify run for {len(handles)} TikTok profiles…")

    try:
        run = client.actor("clockworks/tiktok-scraper").call(run_input={
            "profiles": handles,
            "resultsPerPage": 10,
        })
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:
        print(f"  ❌ TikTok batch run failed: {e}")
        return

    # Group items by author handle
    by_handle = {}
    for it in items:
        h = (it.get("authorMeta") or {}).get("name") or ""
        if h:
            by_handle.setdefault(h.lower(), []).append(it)

    print(f"  ✅ Got data for {len(by_handle)} handles\n")

    updated = 0
    for sid, name, handle, _ in to_scrape:
        key = handle.lstrip("@").lower()
        agent_items = by_handle.get(key, [])
        if not agent_items:
            print(f"  ❌ {name[:45]}: no result for @{handle}")
            continue
        parsed = parse_tiktok(agent_items)
        if parsed:
            upsert(conn, sid, parsed)
            f = parsed.get("tiktok_followers")
            v = parsed.get("tiktok_total_views", 0)
            print(f"  ✅ {name[:45]}: {f or 'N/A'} followers, {v:,} views")
            updated += 1

    print(f"\n  ✓ TikTok batch complete — {updated}/{len(to_scrape)} updated")


# ── Facebook batch enrichment ─────────────────────────────────────────────────

def enrich_facebook_batch(conn, country, client, refresh=False):
    """Scrape all Facebook pages for a country in a single Apify run."""
    rows = conn.execute("""
        SELECT s.id, s.canonical_name, s.facebook_url, s.facebook_followers
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
          AND s.facebook_url IS NOT NULL AND TRIM(s.facebook_url) != ''
        ORDER BY s.canonical_name
    """, (country,)).fetchall()

    to_scrape = rows if refresh else [r for r in rows if r[3] is None]

    print(f"\n{'='*65}")
    print(f"  {country} Facebook batch — {len(to_scrape)}/{len(rows)} pages to scrape")
    print(f"{'='*65}")

    if not to_scrape:
        print("  Nothing to scrape.")
        return

    urls = [r[2] for r in to_scrape]
    print(f"\n  🚀 Launching single Apify run for {len(urls)} Facebook pages…")

    try:
        run = client.actor("apify/facebook-pages-scraper").call(run_input={
            "startUrls": [{"url": u} for u in urls],
            "maxPosts": 0,
        })
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:
        print(f"  ❌ Facebook batch run failed: {e}")
        return

    # Index by page URL
    by_url = {}
    for it in items:
        page_url = it.get("url") or it.get("pageUrl") or ""
        if page_url:
            by_url[page_url.rstrip("/")] = it

    print(f"  ✅ Got data for {len(by_url)} pages\n")

    updated = 0
    for sid, name, fb_url, _ in to_scrape:
        key = fb_url.rstrip("/")
        item = by_url.get(key)
        if not item:
            # Try stripping http/www variants
            for k in by_url:
                if k.split("facebook.com/")[-1] == key.split("facebook.com/")[-1]:
                    item = by_url[k]
                    break
        if not item:
            print(f"  ❌ {name[:45]}: no result for {fb_url[:50]}")
            continue
        parsed = parse_facebook([item])
        if parsed.get("facebook_followers") is not None:
            upsert(conn, sid, parsed)
            f = parsed.get("facebook_followers")
            print(f"  ✅ {name[:45]}: {f:,} followers")
            updated += 1
        else:
            print(f"  ⚠️  {name[:45]}: no follower count returned")

    print(f"\n  ✓ Facebook batch complete — {updated}/{len(to_scrape)} updated")


# ── LinkedIn batch enrichment ─────────────────────────────────────────────────

def enrich_linkedin_batch(conn, country, client, refresh=False):
    """Scrape all LinkedIn company pages for a country in a single Apify run."""
    rows = conn.execute("""
        SELECT s.id, s.canonical_name, s.linkedin_url, s.linkedin_followers
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
          AND s.linkedin_url IS NOT NULL AND TRIM(s.linkedin_url) != ''
        ORDER BY s.canonical_name
    """, (country,)).fetchall()

    to_scrape = rows if refresh else [r for r in rows if r[3] is None]

    print(f"\n{'='*65}")
    print(f"  {country} LinkedIn batch — {len(to_scrape)}/{len(rows)} pages to scrape")
    print(f"{'='*65}")

    if not to_scrape:
        print("  Nothing to scrape.")
        return

    # harvestapi/linkedin-company only processes the first URL when given a list,
    # so we loop per-URL. Still cheaper than old mode because we skip YouTube.
    print(f"\n  🚀 Scraping {len(to_scrape)} LinkedIn pages (1 run each)…")

    updated = 0
    seen_urls = set()
    for sid, name, li_url, _ in to_scrape:
        url = li_url if li_url.startswith("http") else "https://" + li_url
        if url in seen_urls:
            print(f"  ⏭  {name[:45]}: duplicate URL — skip")
            continue
        seen_urls.add(url)
        try:
            run = client.actor("harvestapi/linkedin-company").call(run_input={
                "companies": [url],
            })
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            parsed = parse_linkedin(items)
            if parsed.get("linkedin_followers") is not None:
                upsert(conn, sid, parsed)
                f = parsed.get("linkedin_followers")
                e = parsed.get("li_employee_count")
                print(f"  ✅ {name[:45]}: {f:,} followers, {e or '?'} employees")
                updated += 1
            else:
                print(f"  ⚠️  {name[:45]}: no follower count returned")
        except Exception as e:
            print(f"  ❌ {name[:45]}: {e}")
        time.sleep(0.5)

    print(f"\n  ✓ LinkedIn complete — {updated}/{len(to_scrape)} updated")


# ── Batch enrichment orchestrator (cost-efficient) ───────────────────────────

def enrich_country_batch(conn, country, client, refresh=False):
    """
    Cost-efficient enrichment: one Apify run per platform per country.
    Skips YouTube (keyword-based = unreliable, expensive).
    """
    print(f"\n{'='*65}")
    print(f"  {country} — BATCH MODE (1 Apify run per platform)")
    print(f"{'='*65}")

    # Phase 0: Website scan for TikTok + LINE discovery (no Apify cost)
    agents = conn.execute("""
        SELECT s.id, s.canonical_name, s.website_url, s.website_active,
               s.tiktok_handle, s.line_oa_handle
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
          AND (s.tiktok_handle IS NULL OR s.line_oa_handle IS NULL)
          AND s.website_active = 1 AND s.website_url IS NOT NULL
        ORDER BY s.canonical_name
    """, (country,)).fetchall()

    if agents:
        print(f"\n  Phase 0: Scanning {len(agents)} websites for TikTok/LINE handles…")
        for sid, name, website_url, _, tt_handle, line_handle in agents:
            html = fetch_page(website_url)
            if not html:
                continue
            updates = {}
            if not tt_handle:
                tt_h, tt_u = discover_tiktok(website_url, html)
                if tt_h:
                    updates["tiktok_handle"] = tt_h
                    updates["tiktok_url"]    = tt_u
                    print(f"  🎵 {name[:45]}: TikTok @{tt_h}")
            if not line_handle:
                line_h, _ = discover_line_oa(html)
                if line_h:
                    updates["line_oa_handle"] = line_h
                    print(f"  💚 {name[:45]}: LINE {line_h}")
            if updates:
                upsert(conn, sid, updates)
            time.sleep(0.3)

    # Phase 1–4: One Apify run per platform
    enrich_ig_batch(conn, country, client, refresh)
    enrich_tiktok_batch(conn, country, client, refresh)
    enrich_facebook_batch(conn, country, client, refresh)
    enrich_linkedin_batch(conn, country, client, refresh)

    # Recalculate presence scores
    rows = conn.execute("""
        SELECT id, website_active, google_rating, google_reviews, facebook_url,
               instagram_handle, instagram_followers, tiktok_handle, tiktok_total_views,
               linkedin_url, linkedin_followers, yt_channel_name,
               line_oa_handle, line_oa_friends
        FROM agent_social WHERE LOWER(country) = LOWER(?)
    """, (country,)).fetchall()

    for row in rows:
        score = calc_score(dict(zip(
            ["website_active","google_rating","google_reviews","facebook_url",
             "instagram_handle","instagram_followers","tiktok_handle","tiktok_total_views",
             "linkedin_url","linkedin_followers","yt_channel_name",
             "line_oa_handle","line_oa_friends"],
            list(row)[1:]
        )))
        conn.execute("UPDATE agent_social SET presence_score=?, platform_enriched_at=? WHERE id=?",
                     (score, datetime.now().isoformat(), row[0]))
    conn.commit()
    print(f"\n  ✓ {country} batch enrichment complete")


# ── LINE OA standalone enrichment ────────────────────────────────────────────

def enrich_line_oa(conn, country, refresh=False):
    """Scan agent websites for LINE OA handles, then scrape friend counts."""
    agents = conn.execute("""
        SELECT s.id, s.canonical_name, s.website_url, s.website_active,
               s.line_oa_handle
        FROM agent_social s
        WHERE LOWER(s.country) = LOWER(?)
        ORDER BY s.presence_score DESC, s.canonical_name
    """, (country,)).fetchall()

    print(f"\n{'='*65}")
    print(f"  {country} LINE OA enrichment — {len(agents)} agents")
    print(f"{'='*65}")

    found = 0
    for i, (sid, name, website_url, website_active, line_handle) in enumerate(agents, 1):
        print(f"\n[{i}/{len(agents)}] {name[:55]}")
        updates = {}

        # Discovery — scan website if no handle yet (or refresh)
        if (not line_handle or refresh) and website_active and website_url:
            html = fetch_page(website_url)
            if html:
                h, _ = discover_line_oa(html)
                if h:
                    updates["line_oa_handle"] = h
                    line_handle = h
                    print(f"  💚 Found: {h}")
                else:
                    print(f"  💚 Not found on website")
            else:
                print(f"  💚 Website unreachable")
        elif line_handle:
            print(f"  💚 Existing handle: {line_handle}")
        else:
            print(f"  💚 No website — skip")

        # Scrape if we have a handle
        if line_handle and (refresh or not conn.execute(
                "SELECT line_oa_friends FROM agent_social WHERE id=?", (sid,)
                ).fetchone()[0]):
            parsed = scrape_line_oa(line_handle)
            updates.update(parsed)
            f = parsed.get("line_oa_friends")
            b = parsed.get("line_oa_verified")
            print(f"  💚 Friends: {f or 'N/A'}, badge: {b or 'none'}")
            if f:
                found += 1

        if updates:
            upsert(conn, sid, updates)
        time.sleep(0.5)

    print(f"\n  ✓ LINE OA complete — {found}/{len(agents)} agents with friend count")


# ── Main enrichment loop ──────────────────────────────────────────────────────

def enrich_country(conn, country, client, refresh=False,
                   discover_only=False, report_only=False):
    agents = get_agents(conn, country, refresh)
    print(f"\n{'='*65}")
    print(f"  {country} — {len(agents)} agents to enrich")
    print(f"{'='*65}")

    for i, row in enumerate(agents, 1):
        (sid, agent_id, name, ctry, website_url, website_active,
         ig_handle, ig_url, li_url, tt_handle, tt_url,
         g_rating, g_reviews, g_maps_url,
         presence, city, email, phone, fb_url, line_handle) = row

        print(f"\n[{i}/{len(agents)}] {name[:55]}")
        updates = {}

        # ── Phase 0: Website fetch (shared for LINE + TikTok discovery) ──
        site_html = None
        if website_active and website_url and (not tt_handle or not line_handle):
            print(f"  🔍 Fetching website for social discovery…")
            site_html = fetch_page(website_url)

        # ── Phase 0a: LINE OA discovery ────────────────────────────────
        if not line_handle and site_html:
            line_h, line_url = discover_line_oa(site_html)
            if line_h:
                updates["line_oa_handle"] = line_h
                line_handle = line_h
                print(f"  💚 LINE OA found: {line_h}")
            else:
                print(f"  💚 LINE OA: not found on website")
        elif line_handle:
            print(f"  💚 LINE OA: {line_handle} (existing)")

        # ── Phase 1: TikTok discovery ──────────────────────────────────
        if not tt_handle and site_html:
            tt_h, tt_u = discover_tiktok(website_url, site_html)
            if tt_h:
                updates["tiktok_handle"] = tt_h
                updates["tiktok_url"]    = tt_u
                tt_handle = tt_h
                print(f"  🎵 TikTok found: @{tt_h}")
            else:
                print(f"  🎵 TikTok: not found on website")
        elif tt_handle:
            print(f"  🎵 TikTok: @{tt_handle} (existing)")

        if discover_only or report_only:
            if updates:
                upsert(conn, sid, updates)
            continue

        # ── Phase 2: Instagram ─────────────────────────────────────────
        if ig_handle:
            print(f"  📸 Scraping Instagram @{ig_handle}…")
            try:
                items = scrape_instagram(client, ig_handle)
                parsed = parse_instagram(items)
                updates.update(parsed)
                f = parsed.get("instagram_followers")
                p = parsed.get("ig_post_count")
                print(f"  📸 Instagram: {f or 'N/A'} followers, {p or 'N/A'} posts")
            except Exception as e:
                print(f"  📸 Instagram ERROR: {e}")
        else:
            print(f"  📸 Instagram: no handle — skip")

        # ── Phase 2a: LINE OA scraping ────────────────────────────────
        if line_handle:
            print(f"  💚 Scraping LINE OA {line_handle}…")
            try:
                parsed = scrape_line_oa(line_handle)
                updates.update(parsed)
                f = parsed.get("line_oa_friends")
                b = parsed.get("line_oa_verified")
                print(f"  💚 LINE OA: {f or 'N/A'} friends, badge={b or 'none'}")
            except Exception as e:
                print(f"  💚 LINE OA ERROR: {e}")
        else:
            print(f"  💚 LINE OA: no handle — skip")

        # ── Phase 2b: Facebook ─────────────────────────────────────────
        if fb_url:
            print(f"  📘 Scraping Facebook page…")
            try:
                items = scrape_facebook(client, fb_url)
                parsed = parse_facebook(items)
                updates.update(parsed)
                f = parsed.get("facebook_followers")
                print(f"  📘 Facebook: {f or 'N/A'} followers/likes")
            except Exception as e:
                print(f"  📘 Facebook ERROR: {e}")
        else:
            print(f"  📘 Facebook: no URL — skip")

        # ── Phase 3: TikTok ────────────────────────────────────────────
        if tt_handle:
            print(f"  🎵 Scraping TikTok @{tt_handle}…")
            try:
                items = scrape_tiktok(client, tt_handle)
                parsed = parse_tiktok(items)
                updates.update(parsed)
                f = parsed.get("tiktok_followers")
                v = parsed.get("tiktok_total_views")
                print(f"  🎵 TikTok: {f or 'N/A'} followers, {v or 0:,} total views")
            except Exception as e:
                print(f"  🎵 TikTok ERROR: {e}")
        else:
            print(f"  🎵 TikTok: no handle — skip")

        # ── Phase 4: YouTube ───────────────────────────────────────────
        if website_active or (presence or 0) >= 2:
            print(f"  📺 Scraping YouTube for '{name[:35]}'…")
            try:
                items = scrape_youtube(client, name, city)
                parsed = parse_youtube(items, name)
                updates.update(parsed)
                ch = parsed.get("yt_channel_name")
                tv = parsed.get("yt_top_video_views")
                print(f"  📺 YouTube: channel='{ch or 'none'}', top={tv or 0:,} views")
            except Exception as e:
                print(f"  📺 YouTube ERROR: {e}")
        else:
            print(f"  📺 YouTube: no website — skip")

        # ── Phase 5: LinkedIn ──────────────────────────────────────────
        if li_url:
            print(f"  💼 Scraping LinkedIn…")
            try:
                items = scrape_linkedin(client, li_url)
                parsed = parse_linkedin(items)
                updates.update(parsed)
                f = parsed.get("linkedin_followers")
                e = parsed.get("li_employee_count")
                print(f"  💼 LinkedIn: {f or 'N/A'} followers, {e or 'N/A'} employees")
            except Exception as e:
                print(f"  💼 LinkedIn ERROR: {e}")
        else:
            print(f"  💼 LinkedIn: no URL — skip")

        # ── Update DB ─────────────────────────────────────────────────
        updates["platform_enriched_at"] = datetime.now().isoformat()

        # Recalculate presence score with enriched data
        full_row = dict(zip(
            ["website_active", "google_rating", "google_reviews", "facebook_url",
             "instagram_handle", "instagram_followers", "tiktok_handle",
             "tiktok_total_views", "linkedin_url", "linkedin_followers", "yt_channel_name",
             "line_oa_handle", "line_oa_friends"],
            [website_active, g_rating, g_reviews, fb_url,
             ig_handle, updates.get("instagram_followers"),
             tt_handle or updates.get("tiktok_handle"),
             updates.get("tiktok_total_views"),
             li_url, updates.get("linkedin_followers"),
             updates.get("yt_channel_name"),
             line_handle or updates.get("line_oa_handle"),
             updates.get("line_oa_friends")]
        ))
        updates["presence_score"] = calc_score(full_row)

        upsert(conn, sid, updates)
        print(f"  📊 Score: {updates['presence_score']}/10")
        time.sleep(1.0)

    print(f"\n  ✓ {country} enrichment complete")


# ── HTML profile generator ────────────────────────────────────────────────────

def fmt_num(n):
    if n is None:
        return None
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def stars_html(rating):
    if not rating:
        return ""
    full = int(rating)
    half = (rating - full) >= 0.5
    return "★" * full + ("½" if half else "")


def rebuild_profiles(conn):
    """Export all enriched data and regenerate agent-profile.html."""
    print("\n⚙  Rebuilding agent-profile.html…")

    rows = conn.execute("""
        SELECT
            s.agent_id, s.canonical_name, s.country,
            s.website_url, s.website_active,
            s.facebook_url, s.facebook_followers,
            s.instagram_handle, s.instagram_url, s.instagram_followers,
            s.ig_post_count, s.ig_last_post,
            s.tiktok_handle, s.tiktok_url, s.tiktok_followers,
            s.tiktok_video_count, s.tiktok_total_views, s.tiktok_top_video_views,
            s.tiktok_avg_views, s.tiktok_engagement_rate, s.tiktok_last_post,
            s.tiktok_videos,
            s.linkedin_url, s.linkedin_followers, s.li_employee_count,
            s.google_rating, s.google_reviews, s.google_maps_url,
            s.yt_channel_name, s.yt_channel_url, s.yt_subscribers,
            s.yt_total_views, s.yt_video_count, s.yt_top_video_title,
            s.yt_top_video_views, s.yt_videos,
            s.presence_score, s.platform_enriched_at, s.researched_at,
            a.email, a.phone,
            GROUP_CONCAT(DISTINCT a.city) as all_cities,
            s.line_oa_handle, s.line_oa_friends, s.line_oa_verified,
            GROUP_CONCAT(DISTINCT u.name) as unis
        FROM agent_social s
        JOIN agents a ON (
                        (a.canonical_name = s.canonical_name AND TRIM(a.canonical_name) != '')
                        OR (TRIM(COALESCE(a.canonical_name,'')) = '' AND a.parent_company = s.canonical_name)
                      )
                      AND LOWER(a.country) = LOWER(s.country)
        JOIN universities u ON u.id = a.university_id
        WHERE s.country IN ('Thailand','Nepal','Cambodia','Vietnam','Indonesia','Sri Lanka')
        GROUP BY s.id
        ORDER BY s.country, s.presence_score DESC
    """).fetchall()

    cols = [
        "id","name","country","website_url","website_active","facebook_url","facebook_followers",
        "instagram_handle","instagram_url","instagram_followers",
        "ig_post_count","ig_last_post",
        "tiktok_handle","tiktok_url","tiktok_followers",
        "tiktok_video_count","tiktok_total_views","tiktok_top_video_views",
        "tiktok_avg_views","tiktok_engagement_rate","tiktok_last_post","tiktok_videos",
        "linkedin_url","linkedin_followers","li_employee_count",
        "google_rating","google_reviews","google_maps_url",
        "yt_channel_name","yt_channel_url","yt_subscribers",
        "yt_total_views","yt_video_count","yt_top_video_title",
        "yt_top_video_views","yt_videos",
        "presence_score","platform_enriched_at","researched_at",
        "email","phone","all_cities",
        "line_oa_handle","line_oa_friends","line_oa_verified",
        "unis"
    ]

    # Bangkok district → city normalisation map
    BKK_DISTRICTS = {
        "pathum wan","watthana","bang rak","ratchathewi","khlong toei",
        "chatuchak","huai khwang","silom","khan na yao","bang bon",
        "laksi","pathumwan","din daeng","bang phlat","lat phrao",
        "sathon","phra nakhon","pom prap sattru phai","samphanthawong",
        "bang sue","phaya thai","dusit","thawi watthana","taling chan",
        "bang khae","nong khaem","rat burana","thon buri","khlong san",
        "bangkok noi","bangkok yai","phra khanong","min buri","lat krabang",
        "bang na","bueng kum","saphan sung","wang thonglang","klong luang",
        "klongluang",
    }

    def normalise_city(raw):
        if not raw:
            return None
        c = raw.strip()
        # Strip bracketed suffixes like "Bangkok (Chidlom)"
        c = c.split("(")[0].strip()
        # Strip "Mueang " prefix
        if c.lower().startswith("mueang "):
            c = c[7:].strip()
        # Map to Bangkok
        if c.lower() in BKK_DISTRICTS:
            c = "Bangkok"
        # Discard junk
        if c.lower() in ("thailand", ""):
            return None
        return c

    agents = []
    for row in rows:
        d = dict(zip(cols, row))
        # Parse JSON fields
        for fld in ("tiktok_videos", "yt_videos"):
            if d.get(fld):
                try:
                    d[fld] = json.loads(d[fld])
                except Exception:
                    d[fld] = []
            else:
                d[fld] = []
        # Parse universities
        d["unis"] = sorted(set(d["unis"].split(","))) if d.get("unis") else []
        # Normalise city: collapse districts → city, handle multi-branch
        raw_cities = [normalise_city(c) for c in (d.get("all_cities") or "").split(",")]
        distinct = list(dict.fromkeys(c for c in raw_cities if c))  # unique, order-preserving
        if len(distinct) == 0:
            d["city"] = None
        elif len(distinct) == 1:
            d["city"] = distinct[0]
        else:
            d["city"] = "Multiple"
        # Numeric safety
        for fld in ("google_rating", "presence_score", "tiktok_avg_views",
                    "tiktok_engagement_rate"):
            if d.get(fld) is not None:
                d[fld] = round(float(d[fld]), 2)
        agents.append(d)

    # Deduplicate: keep highest presence_score per (country, canonical_name).
    # Tiebreak: prefer higher agent_id — SOCIAL_INDEX in agent-network.html was
    # built from the later duplicate rows (higher agent_id), so using the higher
    # id here ensures byId[id] matches what SOCIAL_INDEX links to.
    seen_keys = {}
    for d in agents:
        key = (d.get("country", ""), d.get("name", ""))
        score = d.get("presence_score") or 0
        prev = seen_keys.get(key)
        if prev is None:
            seen_keys[key] = d
        elif score > (prev.get("presence_score") or 0):
            seen_keys[key] = d
        elif score == (prev.get("presence_score") or 0) and (d.get("id") or 0) > (prev.get("id") or 0):
            seen_keys[key] = d
    agents = list(seen_keys.values())
    agents.sort(key=lambda a: (a.get("country",""), -(a.get("presence_score") or 0)))

    data_json = json.dumps(agents, ensure_ascii=False, separators=(",", ":"),
                           default=str)
    print(f"  Embedded {len(agents)} agent profiles ({len(data_json)//1024}KB)")

    html = _build_html(data_json)
    PROFILE_HTML.write_text(html, encoding="utf-8")
    print(f"  ✓ Written → {PROFILE_HTML} ({len(html)//1024}KB)")


def _build_html(data_json):
    today = datetime.now().strftime("%d %B %Y")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Profile — Market Intelligence Hub</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" crossorigin="anonymous">
<style>
/* ── Reset & Base ── */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:#f4f5f7;color:#1a1a2e;font-size:14px;line-height:1.6}}

/* ── Site nav ── */
.site-nav-bar{{background:#0f0f0f;display:flex;align-items:center;justify-content:space-between;padding:0 40px;height:44px;position:sticky;top:0;z-index:999;font-family:'DM Sans',-apple-system,sans-serif}}
.site-nav-home{{font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#f7f4ee;text-decoration:none;font-weight:500;white-space:nowrap}}
.site-nav-home:hover{{color:#b8963e}}
.site-nav-links{{display:flex;gap:28px}}
.site-nav-links a{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#888;text-decoration:none;transition:color .15s;white-space:nowrap}}
.site-nav-links a:hover{{color:#f7f4ee}}
.site-nav-links a.active{{color:#b8963e}}

/* ── Cover / Hero ── */
.cover{{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 60%,#0f3460 100%);color:#fff;padding:48px 80px 40px}}
.cover-label{{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#e94560;margin-bottom:12px}}
.cover-name{{font-size:clamp(24px,4vw,40px);font-weight:700;letter-spacing:-0.5px;line-height:1.2;margin-bottom:8px}}
.cover-sub{{font-size:16px;font-weight:400;color:#a8b2d8;margin-bottom:32px}}
.cover-meta{{display:flex;gap:32px;flex-wrap:wrap;border-top:1px solid rgba(255,255,255,.1);padding-top:20px;align-items:center}}
.cover-meta-item{{display:flex;flex-direction:column;gap:2px}}
.cover-meta-item .label{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#a8b2d8}}
.cover-meta-item .value{{font-size:14px;font-weight:600}}
.score-block{{margin-left:auto;text-align:center;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:16px 24px}}
.score-num{{font-size:48px;font-weight:800;letter-spacing:-2px;line-height:1;color:#FBBC05}}
.score-label{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:#a8b2d8;margin-top:2px}}

/* ── Layout ── */
.container{{max-width:960px;margin:0 auto;padding:36px 24px 80px}}
.section{{margin-bottom:36px}}
.section-header{{display:flex;align-items:center;gap:10px;margin-bottom:20px;padding-bottom:12px;border-bottom:2px solid #e8eaf0}}
.section-header .icon{{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}}
.section-header h3{{font-size:16px;font-weight:700;letter-spacing:-0.2px}}
.section-header .badge{{margin-left:auto;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:3px 9px;border-radius:12px;background:#d4edda;color:#155724}}
.section-header .badge.warning{{background:#fff3cd;color:#856404}}
.section-header .badge.info{{background:#e3f2fd;color:#0d47a1}}
.section-header .badge.na{{background:#f8f9fa;color:#6c757d}}

/* ── KPI Strip ── */
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:32px}}
.stat-card{{background:#fff;border-radius:10px;padding:18px 18px 14px;box-shadow:0 1px 4px rgba(0,0,0,.06);position:relative;overflow:hidden}}
.stat-card::before{{content:"";position:absolute;top:0;left:0;right:0;height:3px}}
.stat-card.tiktok::before{{background:#69C9D0}}
.stat-card.instagram::before{{background:linear-gradient(90deg,#C13584,#E1306C,#F56040)}}
.stat-card.youtube::before{{background:#FF0000}}
.stat-card.linkedin::before{{background:#0a66c2}}
.stat-card.maps::before{{background:#34A853}}
.stat-card.neutral::before{{background:#a8b2d8}}
.stat-label{{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888;margin-bottom:5px}}
.stat-value{{font-size:26px;font-weight:700;letter-spacing:-1px;line-height:1;color:#1a1a2e}}
.stat-sub{{font-size:11px;color:#aaa;margin-top:3px}}
.verified-badge{{display:inline-block;background:#d4edda;color:#155724;font-size:9px;font-weight:700;letter-spacing:1px;padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle;text-transform:uppercase}}

/* ── Data card ── */
.data-card{{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:hidden;margin-bottom:20px}}
.data-card-header{{background:#f8f9fc;padding:14px 20px;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#555;border-bottom:1px solid #eee;display:flex;align-items:center;gap:8px}}
.data-card-body{{padding:20px}}
table{{width:100%;border-collapse:collapse}}
thead{{background:#f8f9fc}}
th{{text-align:left;padding:10px 14px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#888;font-weight:600;border-bottom:1px solid #eee}}
td{{padding:10px 14px;font-size:13px;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafbfd}}
.rank{{font-weight:700;color:#1a1a2e;font-size:14px}}
.views-bar-wrap{{display:flex;align-items:center;gap:8px;min-width:120px}}
.views-bar{{height:6px;border-radius:3px;background:#e8eaf0;flex:1;position:relative;overflow:hidden}}
.views-bar-fill{{position:absolute;top:0;left:0;height:100%;border-radius:3px}}
.bar-tiktok{{background:#69C9D0}}
.bar-youtube{{background:#FF4444}}
.views-num{{font-size:12px;font-weight:600;color:#333;white-space:nowrap}}
.engagement-pill{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700}}
.pill-high{{background:#d4edda;color:#155724}}
.pill-medium{{background:#fff3cd;color:#856404}}
.pill-low{{background:#f8d7da;color:#721c24}}
.owned-badge{{display:inline-block;background:#e94560;color:#fff;border-radius:3px;padding:2px 6px;font-size:10px;font-weight:600}}
.third-badge{{display:inline-block;background:#f0f2f8;color:#555;border-radius:3px;padding:2px 6px;font-size:10px;font-weight:600}}

/* ── Maps ── */
.maps-card{{background:#fff;border-radius:10px;padding:24px 28px;box-shadow:0 1px 4px rgba(0,0,0,.06);display:flex;gap:40px;align-items:flex-start}}
.maps-rating-number{{font-size:56px;font-weight:800;letter-spacing:-2px;color:#1a1a2e;line-height:1}}
.stars{{font-size:20px;color:#FBBC05;letter-spacing:2px;margin:4px 0}}
.maps-rating-sub{{font-size:12px;color:#888}}

/* ── Platform row (meta) ── */
.meta-row{{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:20px}}
.meta-chip{{display:flex;align-items:center;gap:6px;background:#f4f5f7;border-radius:6px;padding:8px 12px;font-size:12px}}
.meta-chip .icon{{font-size:14px}}
.meta-chip .val{{font-weight:600;color:#1a1a2e}}
.meta-chip .lbl{{color:#888;font-size:11px;margin-left:2px}}

/* ── Score breakdown ── */
.score-breakdown{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.score-row{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8f9fc;border-radius:6px}}
.score-row .platform-name{{width:90px;font-size:12px;font-weight:600;color:#555}}
.score-bar-wrap{{flex:1;height:6px;background:#e8eaf0;border-radius:3px;overflow:hidden}}
.score-bar-fill-inner{{height:100%;border-radius:3px;background:#e94560}}
.score-row .pts{{font-size:12px;font-weight:700;color:#333;min-width:40px;text-align:right}}

/* ── Coming soon ── */
.coming-soon{{background:#fff;border-radius:10px;padding:28px;box-shadow:0 1px 4px rgba(0,0,0,.06);text-align:center;border:2px dashed #e8eaf0}}
.coming-soon .icon{{font-size:32px;margin-bottom:12px}}
.coming-soon h4{{font-size:15px;font-weight:700;margin-bottom:6px;color:#1a1a2e}}
.coming-soon p{{font-size:13px;color:#888}}

/* ── Uni list ── */
.uni-logo-grid{{display:flex;flex-wrap:wrap;gap:12px;padding:16px}}
.uni-logo-tile{{display:flex;flex-direction:column;align-items:center;justify-content:center;background:#f8f9fa;border:1px solid #e8eaf0;border-radius:10px;padding:12px 14px;width:130px;min-height:80px;text-align:center;transition:box-shadow .15s}}
.uni-logo-tile:hover{{box-shadow:0 2px 8px rgba(0,0,0,.1)}}
.uni-logo-tile img{{max-width:100px;max-height:48px;object-fit:contain;margin-bottom:6px}}
.uni-logo-tile span{{font-size:10px;color:#555;line-height:1.3;font-weight:500}}

/* ── Brand icons ── */
.section-header .icon i {{ font-size:15px }}
.section-header .icon svg {{ display:block }}
.stat-label i, .stat-label svg {{ font-size:13px; vertical-align:middle; margin-right:3px }}
.meta-chip i, .meta-chip svg {{ font-size:13px; vertical-align:middle }}
.fa-tiktok {{ color:#69C9D0 }}
.fa-instagram {{ background: linear-gradient(45deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text }}
.fa-youtube {{ color:#FF0000 }}
.fa-facebook {{ color:#1877F2 }}
.fa-linkedin {{ color:#0a66c2 }}
.fa-google {{ color:#4285F4 }}

/* ── Links ── */
.ext-link{{display:inline-flex;align-items:center;gap:4px;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#0f3460;text-decoration:none;border:1px solid #d0d8e8;padding:4px 10px;border-radius:4px;transition:all .15s}}
.ext-link:hover{{background:#0f3460;color:#fff}}

/* ── Meta Ads ── */
.meta-ads-grid{{display:flex;gap:16px;flex-wrap:wrap;padding:20px}}
.meta-ads-stat{{text-align:center;min-width:90px}}
.meta-ads-stat .val{{font-size:28px;font-weight:800;letter-spacing:-1px;color:#1877F2;line-height:1}}
.meta-ads-stat .lbl{{font-size:10px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:2px}}
.meta-ads-unis{{padding:0 20px 16px;display:flex;flex-wrap:wrap;gap:6px}}
.meta-ads-uni-tag{{font-size:11px;font-weight:600;background:#e8f0fe;color:#1877F2;padding:3px 10px;border-radius:12px}}

/* ── Not found ── */
#not-found{{display:none;max-width:600px;margin:80px auto;text-align:center;padding:40px}}
#profile{{display:none}}

/* ── Responsive ── */
@media(max-width:640px){{
  .cover{{padding:32px 20px 28px}}
  .container{{padding:24px 16px 60px}}
  .stat-grid{{grid-template-columns:1fr 1fr}}
  .score-breakdown{{grid-template-columns:1fr}}
  .maps-card{{flex-direction:column;gap:20px}}
  .cover-meta{{gap:20px}}
  .score-block{{margin-left:0;width:100%}}
  .site-nav-bar{{padding:0 16px}}
  .site-nav-links{{gap:14px}}
  .site-nav-links a{{font-size:9px;letter-spacing:1px}}
}}
/* ── Events section ── */
.events-list{{display:flex;flex-direction:column;gap:10px}}
.event-card{{background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.06);border-left:3px solid #1a4a6b}}
.event-card-name{{font-size:14px;font-weight:700;color:#1a1a2e;margin-bottom:6px;line-height:1.3}}
.event-card-meta{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:11px;color:#6c757d;margin-bottom:6px}}
.badge-format{{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;letter-spacing:.5px;text-transform:uppercase}}
.badge-format.online{{background:#e3f2fd;color:#0d47a1}}
.badge-format.inperson{{background:#e8f5e9;color:#1b5e20}}
.badge-format.hybrid{{background:#fff8e1;color:#e65100}}
.event-card-unis{{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}}
.event-uni-tag{{font-size:10px;background:#f4f5f7;border:1px solid #e8eaf0;border-radius:6px;padding:2px 8px;color:#333;font-weight:500}}
.event-card-details{{font-size:12px;color:#555;margin-top:6px;line-height:1.5}}
.event-register-link{{font-size:11px;color:#1a4a6b;text-decoration:none;font-weight:600;margin-top:4px;display:inline-block}}
.event-register-link:hover{{text-decoration:underline}}
</style>
</head>
<body>

<div class="site-nav-bar">
  <a class="site-nav-home" href="index.html">Market Intelligence Hub</a>
  <nav class="site-nav-links">
    <a href="index.html">Home</a>
    <a href="agent-network.html" class="active">Agent Network</a>
    <a href="MQ_mention.html">MQ Coverage</a>
    <a href="one-education-report.html">Competitor Review</a>
  </nav>
</div>

<div id="not-found">
  <p style="font-size:32px;margin-bottom:12px">404</p>
  <h2 style="font-size:22px;margin-bottom:8px">Agent Not Found</h2>
  <p style="color:#666">Return to <a href="agent-network.html" style="color:#e94560">Agent Network</a>.</p>
</div>

<div id="profile"></div>

<script>
const TODAY = "{today}";
const UNI_LOGOS = {{}};
const META_ADS_DATA = {{}}; // keyed by facebook_url → {{active_ads_30d, universities_mentioned, est_reach_min, est_reach_max, ad_library_url}}
const AGENT_EVENTS = {{}};
const AGENTS = {data_json};

const LINE_SVG = `<svg viewBox="0 0 24 24" width="17" height="17" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M19.365 9.863c.349 0 .63.285.63.631 0 .345-.281.63-.63.63H17.61v1.125h1.755c.349 0 .63.283.63.63 0 .344-.281.629-.63.629h-2.386c-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.627-.63h2.386c.349 0 .63.285.63.63 0 .349-.281.63-.63.63H17.61v1.125h1.755zm-3.855 3.016c0 .27-.174.51-.432.596-.064.021-.133.031-.199.031-.211 0-.391-.09-.51-.25l-2.443-3.317v2.94c0 .344-.279.629-.631.629-.346 0-.626-.285-.626-.629V8.108c0-.27.173-.51.43-.595.06-.023.136-.033.194-.033.195 0 .375.104.495.254l2.462 3.33V8.108c0-.345.282-.63.63-.63.345 0 .63.285.63.63v4.771zm-5.741 0c0 .344-.282.629-.631.629-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.627-.63.349 0 .631.285.631.63v4.771zm-2.466.629H4.917c-.345 0-.63-.285-.63-.629V8.108c0-.345.285-.63.63-.63.348 0 .63.285.63.63v4.141h1.756c.348 0 .629.283.629.63 0 .344-.281.629-.629.629M24 10.314C24 4.943 18.615.572 12 .572S0 4.943 0 10.314c0 4.811 4.27 8.842 10.035 9.608.391.082.923.258 1.058.59.12.301.079.766.038 1.08l-.164 1.02c-.045.301-.24 1.186 1.049.645 1.291-.539 6.916-4.078 9.436-6.975C23.176 14.393 24 12.458 24 10.314"/></svg>`;
const ICONS = {{
  tiktok:    `<i class="fa-brands fa-tiktok"></i>`,
  instagram: `<i class="fa-brands fa-instagram"></i>`,
  youtube:   `<i class="fa-brands fa-youtube"></i>`,
  facebook:  `<i class="fa-brands fa-facebook"></i>`,
  linkedin:  `<i class="fa-brands fa-linkedin"></i>`,
  google:    `<i class="fa-brands fa-google"></i>`,
  line:      LINE_SVG,
  globe:     `<i class="fa-solid fa-globe"></i>`,
  users:     `<i class="fa-solid fa-users"></i>`,
  play:      `<i class="fa-solid fa-play"></i>`,
  chart:     `<i class="fa-solid fa-chart-bar"></i>`,
  comment:   `<i class="fa-solid fa-comment"></i>`,
  calendar:  `<i class="fa-solid fa-calendar-days"></i>`,
  trophy:    `<i class="fa-solid fa-trophy"></i>`,
  star:      `<i class="fa-solid fa-star"></i>`,
  check:     `<i class="fa-solid fa-circle-check"></i>`,
  building:  `<i class="fa-solid fa-building"></i>`,
  video:     `<i class="fa-solid fa-film"></i>`,
}};

const byId = {{}};
AGENTS.forEach(a => {{ byId[a.id] = a; }});

function fmt(n) {{
  if (n == null || n === "" || n === undefined) return null;
  n = parseInt(n);
  if (isNaN(n)) return null;
  if (n >= 1000000) return (n/1000000).toFixed(1) + "M";
  if (n >= 1000) return (n/1000).toFixed(1) + "K";
  return n.toString();
}}

function esc(s) {{
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

function stars(r) {{
  if (!r) return "";
  const full = Math.floor(r), half = (r - full) >= 0.5;
  return "★".repeat(full) + (half ? "½" : "");
}}

function engPill(e) {{
  if (e == null) return "";
  const cls = e >= 5 ? "pill-high" : e >= 2 ? "pill-medium" : "pill-low";
  return `<span class="engagement-pill ${{cls}}">${{e.toFixed(1)}}%</span>`;
}}

function scoreClass(s) {{
  if (s >= 7) return "Strong";
  if (s >= 5) return "Moderate";
  if (s >= 3) return "Limited";
  return "Minimal";
}}

function renderEvents(agentName) {{
  const data = AGENT_EVENTS[agentName];
  if (!data || !data.events || data.events.length === 0) return "";
  const events = data.events;
  const pageUrl = data.events_page_url || "";
  const cards = events.map(e => {{
    const fmt = e.format ? `<span class="badge-format ${{e.format.replace('-','')}}">${{esc(e.format)}}</span>` : "";
    const loc  = e.location ? `<span>📍 ${{esc(e.location)}}</span>` : "";
    const time = e.time     ? `<span>🕐 ${{esc(e.time)}}</span>` : "";
    const unis = (e.universities || []).map(u => `<span class="event-uni-tag">${{esc(u)}}</span>`).join("");
    const reg  = e.registration_url ? `<a class="event-register-link" href="${{esc(e.registration_url)}}" target="_blank" rel="noopener">Register ↗</a>` : "";
    const details = e.details ? `<div class="event-card-details">${{esc(e.details)}}</div>` : "";
    return `<div class="event-card">
      <div class="event-card-name">${{esc(e.name || "Event")}}</div>
      <div class="event-card-meta">${{e.date ? `<span>📅 ${{esc(e.date)}}</span>` : ""}}${{time}}${{loc}}${{fmt}}</div>
      ${{unis ? `<div class="event-card-unis">${{unis}}</div>` : ""}}
      ${{details}}${{reg}}
    </div>`;
  }}).join("");
  const pageLink = pageUrl ? ` <a href="${{esc(pageUrl)}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:auto;font-size:11px">View events page ↗</a>` : "";
  return `<div class="section">
    <div class="section-header">
      <div class="icon" style="background:#e8edf5">📅</div>
      <h3>Upcoming Events</h3>
      <span class="badge info">${{events.length}} event${{events.length !== 1 ? "s" : ""}}</span>
      ${{pageLink}}
    </div>
    <div class="events-list">${{cards}}</div>
  </div>`;
}}

function renderProfile(a) {{
  document.title = a.name + " — Market Intelligence Hub";
  const score = a.presence_score || 0;
  const city  = a.city || "";
  const enriched = a.platform_enriched_at
    ? new Date(a.platform_enriched_at).toLocaleDateString("en-GB",{{day:"numeric",month:"short",year:"numeric"}})
    : "Basic scan only";

  // ── KPI values ──
  const kpis = [
    {{ cls:"tiktok",    icon:ICONS.tiktok,    lbl:"TikTok Views", val:fmt(a.tiktok_total_views), sub: a.tiktok_handle ? "@"+a.tiktok_handle : null }},
    {{ cls:"instagram", icon:ICONS.instagram, lbl:"IG Followers",  val:fmt(a.instagram_followers), sub: a.instagram_handle ? "@"+a.instagram_handle : null }},
    {{ cls:"youtube",   icon:ICONS.youtube,   lbl:"YT Top Video",  val:fmt(a.yt_top_video_views), sub: a.yt_channel_name || null }},
    {{ cls:"maps",      icon:ICONS.google,    lbl:"Google Rating",  val: a.google_rating ? a.google_rating.toFixed(1)+"★" : null, sub: a.google_reviews ? a.google_reviews+" reviews" : null }},
    {{ cls:"linkedin",  icon:ICONS.linkedin,  lbl:"LI Followers",  val:fmt(a.linkedin_followers), sub: a.li_employee_count ? a.li_employee_count+" employees" : null }},
    ...(a.line_oa_handle ? [{{ cls:"neutral", icon:ICONS.line, lbl:"LINE Friends", val:fmt(a.line_oa_friends), sub: a.line_oa_handle }}] : []),
  ];

  // ── Events ──
  const eventsHtml = renderEvents(a.name);

  const kpiHtml = kpis.map(k => {{
    const hasData = k.val != null;
    return `<div class="stat-card ${{k.cls}}">
      <div class="stat-label">${{k.icon}} ${{k.lbl}}</div>
      <div class="stat-value">${{hasData ? k.val : '<span style="color:#ccc;font-size:18px">N/A</span>'}}</div>
      <div class="stat-sub">${{k.sub || (hasData ? '<span class="verified-badge">✓ verified</span>' : "not found")}}</div>
    </div>`;
  }}).join("");

  // ── TikTok section ──
  let tiktokHtml = "";
  if (a.tiktok_handle) {{
    const vids = (a.tiktok_videos || []).slice(0,10);
    const maxV = Math.max(...vids.map(v => v.views || 0), 1);
    const rows = vids.map((v,i) => {{
      const pct = Math.round(v.views / maxV * 100);
      return `<tr>
        <td class="rank">${{i+1}}</td>
        <td style="max-width:300px;color:#333">${{esc(v.caption || "—")}}</td>
        <td><div class="views-bar-wrap">
          <div class="views-bar"><div class="views-bar-fill bar-tiktok" style="width:${{pct}}%"></div></div>
          <span class="views-num">${{fmt(v.views) || 0}}</span>
        </div></td>
        <td>${{engPill(v.engagement)}}</td>
        <td style="font-size:11px;color:#999">${{v.date || "—"}}</td>
      </tr>`;
    }}).join("");
    const metaChips = [
      a.tiktok_followers   ? `<div class="meta-chip"><span class="icon">${{ICONS.users}}</span><span class="val">${{fmt(a.tiktok_followers)}}</span><span class="lbl">followers</span></div>` : "",
      a.tiktok_total_views ? `<div class="meta-chip"><span class="icon">${{ICONS.play}}</span><span class="val">${{fmt(a.tiktok_total_views)}}</span><span class="lbl">total views</span></div>` : "",
      a.tiktok_avg_views   ? `<div class="meta-chip"><span class="icon">${{ICONS.chart}}</span><span class="val">${{fmt(a.tiktok_avg_views)}}</span><span class="lbl">avg views</span></div>` : "",
      a.tiktok_engagement_rate != null ? `<div class="meta-chip"><span class="icon">${{ICONS.comment}}</span><span class="val">${{a.tiktok_engagement_rate.toFixed(1)}}%</span><span class="lbl">engagement</span></div>` : "",
      a.tiktok_last_post   ? `<div class="meta-chip"><span class="icon">${{ICONS.calendar}}</span><span class="val">${{a.tiktok_last_post}}</span><span class="lbl">last post</span></div>` : "",
    ].filter(Boolean).join("");
    tiktokHtml = `
      <div class="section">
        <div class="section-header">
          <div class="icon" style="background:#e8f8f9">${{ICONS.tiktok}}</div>
          <h3>TikTok</h3>
          <a href="https://tiktok.com/@${{a.tiktok_handle}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:auto">@${{a.tiktok_handle}} ↗</a>
        </div>
        <div class="meta-row">${{metaChips}}</div>
        ${{rows ? `<div class="data-card">
          <div class="data-card-header">${{ICONS.video}} Top Videos</div>
          <table><thead><tr><th>#</th><th>Caption</th><th>Views</th><th>Engagement</th><th>Date</th></tr></thead>
          <tbody>${{rows}}</tbody></table>
        </div>` : ""}}
      </div>`;
  }} else {{
    tiktokHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e8f8f9">${{ICONS.tiktok}}</div><h3>TikTok</h3><span class="badge na" style="margin-left:auto">Not found</span></div>
      <div class="coming-soon"><div class="icon">${{ICONS.tiktok}}</div><h4>No TikTok Profile Found</h4><p>No TikTok account was identified for this agent.</p></div>
    </div>`;
  }}

  // ── Instagram section ──
  let igHtml = "";
  if (a.instagram_handle) {{
    const igMeta = [
      a.instagram_followers != null ? `<div class="meta-chip"><span class="icon">${{ICONS.users}}</span><span class="val">${{fmt(a.instagram_followers) || "N/A"}}</span><span class="lbl">followers</span></div>` : "",
      a.ig_post_count       != null ? `<div class="meta-chip"><span class="icon">${{ICONS.video}}</span><span class="val">${{a.ig_post_count}}</span><span class="lbl">posts</span></div>` : "",
      a.ig_last_post                 ? `<div class="meta-chip"><span class="icon">${{ICONS.calendar}}</span><span class="val">${{a.ig_last_post}}</span><span class="lbl">last post</span></div>` : "",
    ].filter(Boolean).join("");
    igHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#fce4ec">${{ICONS.instagram}}</div><h3>Instagram</h3>
      <a href="${{a.instagram_url || 'https://instagram.com/'+a.instagram_handle}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:auto">@${{a.instagram_handle}} ↗</a></div>
      <div class="meta-row">${{igMeta || '<p style="color:#888;font-size:13px">Profile found — follower count requires Instagram Graph API.</p>'}}</div>
    </div>`;
  }} else {{
    igHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#fce4ec">${{ICONS.instagram}}</div><h3>Instagram</h3><span class="badge na" style="margin-left:auto">Not found</span></div>
      <div class="coming-soon"><div class="icon">${{ICONS.instagram}}</div><h4>No Instagram Profile Found</h4><p>No Instagram account was identified for this agent.</p></div>
    </div>`;
  }}

  // ── YouTube section ──
  let ytHtml = "";
  const ytVids = (a.yt_videos || []).slice(0,10);
  if (ytVids.length) {{
    const maxV = Math.max(...ytVids.map(v => v.views||0), 1);
    const ytMeta = [
      a.yt_channel_name ? `<div class="meta-chip"><span class="icon">${{ICONS.youtube}}</span><span class="val">${{esc(a.yt_channel_name)}}</span><span class="lbl">channel</span></div>` : "",
      a.yt_subscribers  ? `<div class="meta-chip"><span class="icon">${{ICONS.users}}</span><span class="val">${{fmt(a.yt_subscribers)}}</span><span class="lbl">subscribers</span></div>` : "",
      a.yt_top_video_views ? `<div class="meta-chip"><span class="icon">${{ICONS.trophy}}</span><span class="val">${{fmt(a.yt_top_video_views)}}</span><span class="lbl">top video</span></div>` : "",
    ].filter(Boolean).join("");
    const ytRows = ytVids.map((v,i) => {{
      const pct = Math.round((v.views||0) / maxV * 100);
      const badge = v.owned
        ? `<span class="owned-badge">Owned</span>`
        : `<span class="third-badge">3rd Party</span>`;
      return `<tr>
        <td class="rank">${{i+1}}</td>
        <td style="max-width:280px">${{badge}} <span style="color:#333">${{esc(v.title)}}</span></td>
        <td style="font-size:11px;color:#666">${{esc(v.channel||"")}}</td>
        <td><div class="views-bar-wrap">
          <div class="views-bar"><div class="views-bar-fill bar-youtube" style="width:${{pct}}%"></div></div>
          <span class="views-num">${{fmt(v.views)||"0"}}</span>
        </div></td>
        <td style="font-size:11px;color:#999">${{v.date||"—"}}</td>
      </tr>`;
    }}).join("");
    ytHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#ffebee">${{ICONS.youtube}}</div><h3>YouTube</h3><span class="badge info" style="margin-left:auto">Search results</span></div>
      <div class="meta-row">${{ytMeta}}</div>
      <div class="data-card">
        <div class="data-card-header">${{ICONS.video}} Top Search Results</div>
        <table><thead><tr><th>#</th><th>Video</th><th>Channel</th><th>Views</th><th>Date</th></tr></thead>
        <tbody>${{ytRows}}</tbody></table>
      </div>
    </div>`;
  }} else {{
    ytHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#ffebee">${{ICONS.youtube}}</div><h3>YouTube</h3><span class="badge na" style="margin-left:auto">No results</span></div>
      <div class="coming-soon"><div class="icon">${{ICONS.youtube}}</div><h4>No YouTube Videos Found</h4><p>No search results found for this agent on YouTube.</p></div>
    </div>`;
  }}

  // ── Google Maps section ──
  let mapsHtml = "";
  if (a.google_rating) {{
    const mapsLink = a.google_maps_url
      ? `<a href="${{a.google_maps_url}}" target="_blank" rel="noopener" class="ext-link">View on Maps ↗</a>` : "";
    mapsHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e8f5e9">${{ICONS.google}}</div><h3>Google Business</h3></div>
      <div class="maps-card">
        <div class="maps-rating-block">
          <div class="maps-rating-number">${{a.google_rating.toFixed(1)}}</div>
          <div class="stars">${{stars(a.google_rating)}}</div>
          <div class="maps-rating-sub">${{a.google_reviews || 0}} reviews</div>
        </div>
        <div>
          <h4 style="font-size:15px;font-weight:700;margin-bottom:8px">${{esc(a.name)}}</h4>
          ${{city ? `<p style="font-size:13px;color:#555;margin-bottom:14px">📍 ${{esc(city)}}</p>` : ""}}
          <div style="background:#f4f5f7;border-radius:8px;padding:12px 16px;font-size:13px;margin-bottom:14px">
            ${{a.google_reviews >= 50 ? `<strong style="color:#1a1a2e">Strong presence</strong> — ${{a.google_reviews}} customer reviews on Google Maps` :
               a.google_reviews >= 10 ? `<strong style="color:#1a1a2e">Growing presence</strong> — ${{a.google_reviews}} reviews on Google Maps` :
               `<strong style="color:#1a1a2e">Early stage</strong> — ${{a.google_reviews || 0}} reviews on Google Maps`}}
          </div>
          ${{mapsLink}}
        </div>
      </div>
    </div>`;
  }} else {{
    mapsHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e8f5e9">${{ICONS.google}}</div><h3>Google Business</h3><span class="badge na" style="margin-left:auto">Not listed</span></div>
      <div class="coming-soon"><div class="icon">${{ICONS.google}}</div><h4>No Google Business Listing</h4><p>No verified Google Business profile was found for this agent.</p></div>
    </div>`;
  }}

  // ── LinkedIn section ──
  let liHtml = "";
  if (a.linkedin_url) {{
    const liMeta = [
      a.linkedin_followers  != null ? `<div class="meta-chip"><span class="icon">${{ICONS.users}}</span><span class="val">${{fmt(a.linkedin_followers)||"N/A"}}</span><span class="lbl">followers</span></div>` : "",
      a.li_employee_count   != null ? `<div class="meta-chip"><span class="icon">${{ICONS.building}}</span><span class="val">${{a.li_employee_count}}</span><span class="lbl">employees</span></div>` : "",
    ].filter(Boolean).join("");
    const liUrl = a.linkedin_url.startsWith("http") ? a.linkedin_url : "https://"+a.linkedin_url;
    liHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e3f2fd">${{ICONS.linkedin}}</div><h3>LinkedIn</h3>
      <a href="${{liUrl}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:auto">View page ↗</a></div>
      <div class="meta-row">${{liMeta || '<p style="color:#888;font-size:13px">LinkedIn page found. Follower data may require scraper update.</p>'}}</div>
    </div>`;
  }} else {{
    liHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e3f2fd">${{ICONS.linkedin}}</div><h3>LinkedIn</h3><span class="badge na" style="margin-left:auto">Not found</span></div>
      <div class="coming-soon"><div class="icon">${{ICONS.linkedin}}</div><h4>No LinkedIn Company Page Found</h4><p>No LinkedIn company page was identified for this agent.</p></div>
    </div>`;
  }}

  // ── LINE OA section — show always for Thailand, only if handle exists elsewhere ──
  const isThailand = a.country === "Thailand";
  let lineHtml = "";
  if (a.line_oa_handle) {{
    const lineMeta = [
      a.line_oa_friends != null ? `<div class="meta-chip"><span class="icon">${{ICONS.users}}</span><span class="val">${{fmt(a.line_oa_friends)}}</span><span class="lbl">friends</span></div>` : "",
      a.line_oa_verified ? `<div class="meta-chip"><span class="icon">${{ICONS.check}}</span><span class="val" style="text-transform:capitalize">${{esc(a.line_oa_verified)}}</span><span class="lbl">badge</span></div>` : "",
    ].filter(Boolean).join("");
    lineHtml = `<div class="section">
      <div class="section-header">
        <div class="icon" style="background:#e6f9ed;color:#06C755">${{ICONS.line}}</div>
        <h3>LINE Official Account</h3>
        <a href="https://page.line.me/${{a.line_oa_handle}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:auto">page.line.me/${{esc(a.line_oa_handle)}} ↗</a>
      </div>
      <div class="meta-row">${{lineMeta || '<p style="color:#888;font-size:13px">LINE OA found — friend count unavailable.</p>'}}</div>
    </div>`;
  }} else if (isThailand) {{
    lineHtml = `<div class="section">
      <div class="section-header"><div class="icon" style="background:#e6f9ed;color:#06C755">${{ICONS.line}}</div><h3>LINE Official Account</h3><span class="badge na" style="margin-left:auto">Not found</span></div>
      <div class="coming-soon"><div class="icon" style="color:#06C755">${{ICONS.line}}</div><h4>No LINE OA Found</h4><p>LINE Official Account is the primary messaging channel in Thailand. No account was identified on this agent's website.</p></div>
    </div>`;
  }}
  // For non-Thailand agents with no handle: lineHtml stays "" (section hidden)

  // ── Facebook ──
  // ── Meta Ads block ──
  const metaAds = a.facebook_url ? (META_ADS_DATA[a.facebook_url] || null) : null;
  let metaAdsHtml = "";
  if (metaAds) {{
    const pageName = a.facebook_url.replace(/^https?:\\/\\/(www\\.)?facebook\\.com\\//, "").split("/")[0];
    const libUrl = metaAds.ad_library_url || `https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=TH&q=${{encodeURIComponent(pageName)}}`;
    const uniTags = (metaAds.universities_mentioned || []).map(u =>
      `<span class="meta-ads-uni-tag">${{esc(u)}}</span>`
    ).join("");
    const reachStr = metaAds.est_reach_max
      ? `${{metaAds.est_reach_min >= 1000 ? (metaAds.est_reach_min/1000).toFixed(0)+"K" : metaAds.est_reach_min}}–${{metaAds.est_reach_max >= 1000 ? (metaAds.est_reach_max/1000).toFixed(0)+"K" : metaAds.est_reach_max}}`
      : null;
    metaAdsHtml = `<div class="section">
      <div class="section-header">
        <div class="icon" style="background:#e8f0fe">${{ICONS.facebook}}</div>
        <h3>Active Ads</h3>
        <span class="badge${{metaAds.active_ads_30d > 0 ? '' : ' na'}}" style="margin-left:auto">${{metaAds.active_ads_30d}} active in 30d</span>
        <a href="${{libUrl}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:8px">Ad Library ↗</a>
      </div>
      <div class="data-card" style="margin-bottom:0">
        <div class="meta-ads-grid">
          <div class="meta-ads-stat"><div class="val">${{metaAds.total_ads || 0}}</div><div class="lbl">Total ads</div></div>
          <div class="meta-ads-stat"><div class="val">${{metaAds.active_ads_30d || 0}}</div><div class="lbl">Active now</div></div>
          ${{reachStr ? `<div class="meta-ads-stat"><div class="val" style="font-size:18px">${{reachStr}}</div><div class="lbl">Est. reach</div></div>` : ""}}
        </div>
        ${{uniTags ? `<div class="meta-ads-unis"><span style="font-size:10px;color:#888;letter-spacing:1px;text-transform:uppercase;margin-right:6px;align-self:center">Unis mentioned:</span>${{uniTags}}</div>` : ""}}
      </div>
    </div>`;
  }} else if (a.facebook_url) {{
    // Has FB page but no ads data scraped yet
    const pageName = a.facebook_url.replace(/^https?:\\/\\/(www\\.)?facebook\\.com\\//, "").split("/")[0];
    const libUrl = `https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=TH&q=${{encodeURIComponent(pageName)}}`;
    metaAdsHtml = `<div class="section">
      <div class="section-header">
        <div class="icon" style="background:#e8f0fe">${{ICONS.facebook}}</div>
        <h3>Active Ads</h3>
        <span class="badge na" style="margin-left:auto">Not yet scraped</span>
        <a href="${{libUrl}}" target="_blank" rel="noopener" class="ext-link" style="margin-left:8px">Ad Library ↗</a>
      </div>
    </div>`;
  }}

  const fbFollowers = a.facebook_followers || 0;
  const fbHtml = a.facebook_url ? `<div class="section">
    <div class="section-header">
      <div class="icon" style="background:#e8f0fe">${{ICONS.facebook}}</div>
      <h3>Facebook</h3>
      ${{fbFollowers ? `<span class="badge" style="margin-left:auto">${{fbFollowers.toLocaleString()}} followers</span>` : `<span class="badge info" style="margin-left:auto">Page found</span>`}}
    </div>
    <div class="data-card">
      <div style="display:flex;gap:24px;flex-wrap:wrap;padding:20px">
        ${{fbFollowers ? `<div style="text-align:center;min-width:100px">
          <div style="font-size:32px;font-weight:800;color:#1877F2;letter-spacing:-1px">${{fbFollowers >= 1000 ? (fbFollowers/1000).toFixed(1)+"K" : fbFollowers}}</div>
          <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:2px">Followers</div>
        </div>` : ""}}
        <div style="flex:1;min-width:160px;display:flex;flex-direction:column;justify-content:center;gap:6px">
          <div style="font-size:13px;color:#555">
            ${{ICONS.facebook}} <a href="${{a.facebook_url}}" target="_blank" rel="noopener" style="color:#1877F2;text-decoration:none">${{esc(a.facebook_url.replace(/^https?:\\/\\/(www\\.)?/,"").slice(0,55))}}</a>
          </div>
        </div>
      </div>
    </div>
  </div>` : "";

  // ── Score breakdown ──
  const scoreItems = [
    {{ lbl:"Website",   pts: a.website_active ? 2.0 : 0, max:2.0, col:"#0f3460" }},
    {{ lbl:"Google",    pts: a.google_rating ? (a.google_rating >= 4 ? 2.5 : 1.5) : 0, max:2.5, col:"#34A853" }},
    {{ lbl:"TikTok",    pts: a.tiktok_handle ? (a.tiktok_total_views >= 100000 ? 1.5 : a.tiktok_total_views >= 10000 ? 1.0 : 0.5) : 0, max:1.5, col:"#69C9D0" }},
    {{ lbl:"Instagram", pts: a.instagram_handle ? (((a.instagram_followers||0) >= 2000) ? 1.5 : ((a.instagram_followers||0) >= 500) ? 1.0 : 0.5) : 0, max:1.5, col:"#C13584" }},
    {{ lbl:"Facebook",  pts: a.facebook_url ? ((a.facebook_followers||0) >= 10000 ? 1.5 : (a.facebook_followers||0) >= 1000 ? 1.0 : 0.5) : 0, max:1.5, col:"#1877f2" }},
    {{ lbl:"LinkedIn",  pts: a.linkedin_url ? (((a.linkedin_followers||0) >= 500) ? 1.0 : 0.5) : 0, max:1.0, col:"#0a66c2" }},
    {{ lbl:"YouTube",   pts: a.yt_channel_name ? 0.5 : 0, max:0.5, col:"#FF0000" }},
    ...(isThailand || a.line_oa_handle ? [{{ lbl:"LINE OA", pts: a.line_oa_handle ? (((a.line_oa_friends||0) >= 10000) ? 1.5 : ((a.line_oa_friends||0) >= 1000) ? 1.0 : 0.5) : 0, max:1.5, col:"#06C755" }}] : []),
  ];
  const scoreBreakdownHtml = scoreItems.map(s => {{
    const pct = s.max > 0 ? Math.round(s.pts / s.max * 100) : 0;
    return `<div class="score-row">
      <div class="platform-name">${{s.lbl}}</div>
      <div class="score-bar-wrap"><div class="score-bar-fill-inner" style="width:${{pct}}%;background:${{s.col}}"></div></div>
      <div class="pts">${{s.pts.toFixed(1)}} / ${{s.max.toFixed(1)}}</div>
    </div>`;
  }}).join("");

  // ── Uni logo tiles ──
  const uniLogoHtml = (a.unis||[]).map(u => {{
    const logo = UNI_LOGOS[u];
    return logo
      ? `<div class="uni-logo-tile"><img src="${{logo}}" alt="${{esc(u)}}"><span>${{esc(u)}}</span></div>`
      : `<div class="uni-logo-tile"><span style="font-size:11px;color:#1a1a2e;font-weight:600">${{esc(u)}}</span></div>`;
  }}).join("") || `<div style="padding:16px;color:#888;font-style:italic">No university data</div>`;

  // ── Assemble ──
  const contactRows = [
    a.city  ? `<tr><td style="font-size:11px;color:#888;letter-spacing:1px;text-transform:uppercase;padding:6px 14px;width:80px">City</td><td style="padding:6px 14px">${{esc(a.city)}}</td></tr>` : "",
    a.email ? `<tr><td style="font-size:11px;color:#888;letter-spacing:1px;text-transform:uppercase;padding:6px 14px">Email</td><td style="padding:6px 14px"><a href="mailto:${{esc(a.email)}}" style="color:#0f3460">${{esc(a.email)}}</a></td></tr>` : "",
    a.phone ? `<tr><td style="font-size:11px;color:#888;letter-spacing:1px;text-transform:uppercase;padding:6px 14px">Phone</td><td style="padding:6px 14px">${{esc(a.phone)}}</td></tr>` : "",
    a.website_url ? `<tr><td style="font-size:11px;color:#888;letter-spacing:1px;text-transform:uppercase;padding:6px 14px">Website</td><td style="padding:6px 14px"><a href="${{a.website_url}}" target="_blank" rel="noopener" style="color:#0f3460">${{esc(a.website_url.replace(/^https?:\\/\\//, "").slice(0,50))}}</a></td></tr>` : "",
  ].filter(Boolean).join("");

  document.getElementById("profile").innerHTML = `
    <div class="cover">
      <div class="cover-label">${{esc(a.country)}} · Education Agent</div>
      <div class="cover-name">${{esc(a.name)}}</div>
      <div class="cover-sub">Digital Presence Intelligence Report</div>
      <div class="cover-meta">
        ${{city ? `<div class="cover-meta-item"><div class="label">Location</div><div class="value">📍 ${{esc(city)}}</div></div>` : ""}}
        <div class="cover-meta-item"><div class="label">Market</div><div class="value">${{esc(a.country)}}</div></div>
        <div class="cover-meta-item"><div class="label">Partners</div><div class="value">${{(a.unis||[]).length}} universities</div></div>
        <div class="cover-meta-item"><div class="label">Data collected</div><div class="value">${{enriched}}</div></div>
        <div class="score-block">
          <div class="score-num">${{score.toFixed(1)}}</div>
          <div class="score-label">Presence Score / 10</div>
          <div style="font-size:11px;color:#FBBC05;margin-top:4px">${{scoreClass(score)}}</div>
        </div>
      </div>
    </div>

    <div class="container">
      <!-- KPI Strip -->
      <div class="stat-grid">${{kpiHtml}}</div>

      <!-- Score breakdown -->
      <div style="margin-bottom:28px">
        <div class="section-header" style="margin-bottom:14px">
          <div class="icon" style="background:#f4f5f7">📊</div>
          <h3>Presence Breakdown</h3>
        </div>
        <div class="score-breakdown">${{scoreBreakdownHtml}}</div>
      </div>

      <!-- University partners -->
      <div style="margin-bottom:36px">
        <div class="section-header" style="margin-bottom:14px">
          <div class="icon" style="background:#f4f5f7">🎓</div>
          <h3>University Partners</h3>
        </div>
        <div class="data-card" style="margin-bottom:0">
          <div class="uni-logo-grid">${{uniLogoHtml}}</div>
        </div>
      </div>

      ${{tiktokHtml}}
      ${{igHtml}}
      ${{ytHtml}}
      ${{mapsHtml}}
      ${{liHtml}}
      ${{lineHtml}}
      ${{fbHtml}}
      ${{metaAdsHtml}}
      ${{eventsHtml}}

      <!-- Contact -->
      ${{contactRows ? `<div class="section">
        <div class="section-header"><div class="icon" style="background:#f4f5f7">📋</div><h3>Contact Details</h3></div>
        <div class="data-card">
          <table><tbody>${{contactRows}}</tbody></table>
        </div>
      </div>` : ""}}

    </div>`;
}}

// ── Init ──
const id = parseInt(new URLSearchParams(window.location.search).get("id"));
if (!id || !byId[id]) {{
  document.getElementById("not-found").style.display = "block";
}} else {{
  document.getElementById("profile").style.display = "block";
  renderProfile(byId[id]);
}}
</script>
</body>
</html>"""


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enrich agents with Apify social data")
    parser.add_argument("--country",       help="Single country (Thailand or Nepal)")
    parser.add_argument("--refresh",       action="store_true", help="Re-scrape all agents")
    parser.add_argument("--report-only",   action="store_true", help="Rebuild HTML without scraping")
    parser.add_argument("--discover-only", action="store_true", help="TikTok discovery only")
    parser.add_argument("--line-only",     action="store_true", help="LINE OA discovery + scrape only (no Apify)")
    parser.add_argument("--ig-batch",      action="store_true", help="Batch-scrape all IG profiles in one Apify run")
    parser.add_argument("--batch",         action="store_true", help="Cost-efficient mode: 1 Apify run per platform per country (no YouTube)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    migrate_db(conn)

    if args.report_only:
        rebuild_profiles(conn)
        conn.close()
        return

    if args.ig_batch:
        api_token = os.environ.get("APIFY_API_TOKEN")
        if not api_token:
            raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")
        client = ApifyClient(api_token)
        countries = [args.country] if args.country else COUNTRIES
        for country in countries:
            enrich_ig_batch(conn, country, client, refresh=args.refresh)
        rebuild_profiles(conn)
        conn.close()
        return

    if args.batch:
        api_token = os.environ.get("APIFY_API_TOKEN")
        if not api_token:
            raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")
        client = ApifyClient(api_token)
        countries = [args.country] if args.country else COUNTRIES
        for country in countries:
            enrich_country_batch(conn, country, client, refresh=args.refresh)
        rebuild_profiles(conn)
        conn.close()
        print("\n✅  Batch enrichment complete.")
        return

    if args.line_only:
        countries = [args.country] if args.country else COUNTRIES
        for country in countries:
            enrich_line_oa(conn, country, refresh=args.refresh)
        rebuild_profiles(conn)
        conn.close()
        return

    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")
    client = ApifyClient(api_token)

    countries = [args.country] if args.country else COUNTRIES
    for country in countries:
        enrich_country(conn, country, client,
                       refresh=args.refresh,
                       discover_only=args.discover_only,
                       report_only=args.report_only)

    rebuild_profiles(conn)
    conn.close()
    print("\n✅  Enrichment complete.")


if __name__ == "__main__":
    main()
