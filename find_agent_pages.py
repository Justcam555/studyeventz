#!/usr/bin/env python3
"""
find_agent_pages.py — Locate each UK university's international agent /
representative directory page.

Input : data/uk_universities.json   (produced by build_uk_uni_list.py)
Output: data/uk_agent_pages.json    (one record per university)
Misses: logs/uk_agent_pages_missing.log

For every university we try, in order:

  Step 1 — URL pattern probing
      Probe a set of common paths directly on the university's own domain
      (see CANDIDATE_PATHS). The first that returns a usable HTTP 200 wins.
      A light soft-404 guard rejects pages that 200 but really don't exist
      (redirected to the homepage, "page not found" body, etc.) — without it,
      catch-all university CMSes report almost everything as found.

  Step 2 — Search fallback
      Query  '{university} international agents representatives list'  and take
      the top result that sits on the university's own domain. Uses the Google
      Custom Search API when GOOGLE_API_KEY + GOOGLE_CSE_ID are set, otherwise
      falls back to scraping DuckDuckGo's HTML endpoint.

All outbound HTTP is throttled to 1 request / 2 seconds (global). The run is
resumable: results are saved incrementally and an existing output file is
reused so re-runs only process universities not yet resolved.

Usage
-----
    python find_agent_pages.py                 # full run (resumes if possible)
    python find_agent_pages.py --limit 5       # only the first 5 (smoke test)
    python find_agent_pages.py --no-resume     # ignore existing output, start fresh
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs, unquote, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
IN_PATH = ROOT / "data" / "uk_universities.json"
OUT_PATH = ROOT / "data" / "uk_agent_pages.json"
MISSING_LOG = ROOT / "logs" / "uk_agent_pages_missing.log"

CANDIDATE_PATHS = [
    "/international/agents",
    "/international/representatives",
    "/international/partners",
    "/study/agents",
    "/partners/agents",
    "/agents",
    "/representatives",
]

# Rotated per request to reduce the chance of being rate-limited / fingerprinted.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def request_headers() -> dict:
    """Headers with a randomly rotated User-Agent."""
    return {**BASE_HEADERS, "User-Agent": random.choice(USER_AGENTS)}


REQUEST_INTERVAL = 2.0      # seconds between outbound requests (global throttle)
SEARCH_MIN_DELAY = 3.0      # extra randomized delay before each search request
SEARCH_MAX_DELAY = 7.0      # (DuckDuckGo rate-limits aggressively)
TIMEOUT = 12

# Search backend config (optional — DuckDuckGo is the fallback).
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "") or os.environ.get("GOOGLE_CSE_CX", "")

# Signals that a 200 response is really a "not found" page.
SOFT_404_MARKERS = ("page not found", "page cannot be found", "404", "not found",
                    "no longer available", "page you requested")

_last_request = 0.0


def throttle():
    """Enforce >= REQUEST_INTERVAL seconds between any two outbound requests."""
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def search_throttle():
    """Randomized 3-7s spacing before a search request (DDG rate-limit dodge)."""
    global _last_request
    time.sleep(random.uniform(SEARCH_MIN_DELAY, SEARCH_MAX_DELAY))
    _last_request = time.monotonic()


def http_get(url, **kw):
    """Throttled GET. Returns a Response or None on any error."""
    throttle()
    try:
        return requests.get(url, headers=request_headers(), timeout=TIMEOUT,
                            allow_redirects=True, **kw)
    except requests.RequestException:
        return None


def base_domain(netloc: str) -> str:
    """Registrable-ish domain for same-site matching (strip a leading www.)."""
    host = netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def looks_soft_404(resp, requested_path: str) -> bool:
    """True if a 200 response is actually a missing page in disguise."""
    final = urlparse(resp.url)
    final_path = final.path.rstrip("/") or "/"
    # Redirected back to the homepage => the path didn't exist.
    if requested_path.rstrip("/") not in ("", "/") and final_path == "/":
        return True
    # URL itself signals an error/not-found landing page.
    if any(m in resp.url.lower() for m in ("404", "not-found", "page-not-found", "/error")):
        return True
    # Body / <title> signals.
    body = resp.text[:6000].lower()
    title = ""
    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.title and soup.title.string:
        title = soup.title.string.lower()
    if any(m in title for m in SOFT_404_MARKERS):
        return True
    return False


def probe_patterns(scheme: str, netloc: str):
    """Return (url, ) of the first candidate path that resolves, else None."""
    base = f"{scheme}://{netloc}"
    for path in CANDIDATE_PATHS:
        url = base + path
        resp = http_get(url)
        if resp is None:
            continue
        if resp.status_code == 200 and not looks_soft_404(resp, path):
            return resp.url  # the final (possibly redirected-within-site) URL
    return None


def search_google(query: str):
    """Google Custom Search; returns a list of result URLs (may be empty)."""
    search_throttle()
    try:
        params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query, "num": 10}
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                         params=params, headers=request_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return [item.get("link", "") for item in r.json().get("items", [])]
    except (requests.RequestException, ValueError):
        return []


def search_duckduckgo(query: str):
    """Scrape DuckDuckGo's HTML endpoint; returns a list of result URLs."""
    search_throttle()
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query}, headers=request_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            return []
    except requests.RequestException:
        return []
    out = []
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if not href:
            continue
        # DDG wraps links as //duckduckgo.com/l/?uddg=<encoded-real-url>
        if "uddg=" in href:
            q = parse_qs(urlparse(href).query)
            if q.get("uddg"):
                href = unquote(q["uddg"][0])
        if href.startswith("http"):
            out.append(href)
    return out


def search_fallback(query: str, domain: str):
    """Run a web search and return the first result on the university's domain."""
    use_google = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
    results = search_google(query) if use_google else search_duckduckgo(query)
    for url in results:
        host = urlparse(url).netloc.lower()
        if host.endswith(domain):
            return url
    return None


def resolve(uni: dict) -> dict:
    """Find the agent-directory URL for one university."""
    name = uni["name"]
    website = uni.get("website") or ""
    parsed = urlparse(website)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    domain = base_domain(netloc)

    record = {
        "hesa_id": uni.get("hesa_provider_id"),
        "university_name": name,
        "university_domain": domain,
        "agent_page_url": None,
        "url_source": "not_found",
    }
    if not netloc:
        return record  # no usable website on record

    # Step 1 — pattern probing
    hit = probe_patterns(scheme, netloc)
    if hit:
        record["agent_page_url"] = hit
        record["url_source"] = "pattern"
        return record

    # Step 2 — search fallback
    query = f"{name} international agents representatives list"
    hit = search_fallback(query, domain)
    if hit:
        record["agent_page_url"] = hit
        record["url_source"] = "search"
    return record


def load_universities() -> list[dict]:
    data = json.loads(IN_PATH.read_text(encoding="utf-8"))
    return data["universities"] if isinstance(data, dict) else data


def save_results(results: dict):
    """Write the output JSON (results keyed by hesa_id for stable resume)."""
    records = sorted(results.values(), key=lambda r: r["university_name"].lower())
    counts = {"pattern": 0, "search": 0, "not_found": 0}
    for r in records:
        counts[r["url_source"]] = counts.get(r["url_source"], 0) + 1
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_backend": "google_cse" if (GOOGLE_API_KEY and GOOGLE_CSE_ID) else "duckduckgo",
        "total": len(records),
        "counts": counts,
        "results": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return counts


def write_missing_log(results: dict):
    MISSING_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"# not_found agent pages — generated {stamp}"]
    for r in sorted(results.values(), key=lambda r: r["university_name"].lower()):
        if r["url_source"] == "not_found":
            lines.append(f"{r['hesa_id']}\t{r['university_name']}\t{r['university_domain']}")
    MISSING_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Find UK universities' international agent directory pages.")
    ap.add_argument("--limit", type=int, help="process only the first N universities (smoke test)")
    ap.add_argument("--no-resume", action="store_true", help="ignore existing output and start fresh")
    args = ap.parse_args()

    unis = load_universities()
    if args.limit:
        unis = unis[: args.limit]

    # Resume: reuse already-resolved records (those that found something).
    results: dict[str, dict] = {}
    if OUT_PATH.exists() and not args.no_resume:
        prior = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for r in prior.get("results", []):
            if r.get("url_source") != "not_found":
                results[r["hesa_id"]] = r
        if results:
            print(f"Resuming: {len(results)} universities already resolved, skipping those.")

    backend = "Google CSE" if (GOOGLE_API_KEY and GOOGLE_CSE_ID) else "DuckDuckGo (no Google CSE key set)"
    print(f"Search backend: {backend}")
    print(f"Processing {len(unis)} universities (throttle {REQUEST_INTERVAL}s/request)...\n")

    for i, uni in enumerate(unis, 1):
        hid = uni.get("hesa_provider_id")
        if hid in results:
            continue
        rec = resolve(uni)
        results[hid] = rec
        tag = rec["url_source"]
        url = rec["agent_page_url"] or ""
        print(f"  [{i}/{len(unis)}] {rec['university_name'][:38]:38} {tag:9} {url}")
        save_results(results)          # incremental save (resume safety)

    counts = save_results(results)
    write_missing_log(results)
    print(f"\nDone. pattern={counts['pattern']} search={counts['search']} not_found={counts['not_found']}")
    print(f"  -> {OUT_PATH}")
    print(f"  -> {MISSING_LOG} ({counts['not_found']} misses)")


if __name__ == "__main__":
    main()
