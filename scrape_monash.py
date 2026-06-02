#!/usr/bin/env python3
"""
Monash University agent database scraper.

Monash uses a Salesforce Lightning / Aura app hosted at:
  https://unicrm.my.salesforce-sites.com/AgentDB/s/

Direct HTTP requests to the Aura API return empty responses because they
require a valid Salesforce session cookie.  This script uses Playwright to
load the page, intercept every Aura API response, and collect all agent
records across all countries.

Usage:
    python3 scrape_monash.py                    # scrape all countries
    python3 scrape_monash.py --country Thailand # one country only
    python3 scrape_monash.py --dry-run          # print agents, no DB write
"""

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "data" / "agents.db"
AURA_URL = "https://unicrm.my.salesforce-sites.com/AgentDB/aura"
# Go directly to the Salesforce-hosted app — avoids Cloudflare on Monash and iframe complexity
SF_APP_PAGE = "https://unicrm.my.salesforce-sites.com/AgentDB/s/"
MONASH_PAGE = "https://www.monash.edu/study/how-to-apply/application-process/international-students/agent-database"

UNIVERSITY_NAME = "Monash University"

# ─── helpers ─────────────────────────────────────────────────────────────────

def get_university_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM universities WHERE name LIKE ?", (f"%Monash%",)
    ).fetchone()
    if row:
        return row[0]
    conn.execute(
        "INSERT INTO universities (name, website, agent_page_url) VALUES (?, ?, ?)",
        (UNIVERSITY_NAME, "https://www.monash.edu", MONASH_PAGE),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM universities WHERE name = ?", (UNIVERSITY_NAME,)
    ).fetchone()[0]


def parse_record(rec: dict, country_fallback: str = "") -> dict:
    """Flatten a Salesforce agent record into DB fields."""
    def g(*keys):
        for k in keys:
            v = rec.get(k)
            if v and str(v).strip() not in ("", "null", "None"):
                return str(v).strip()
        return ""

    return {
        "company_name": g("Agency_Name__c", "Name", "name"),
        "contact_name": g("Contact_Name__c", "Contact__c"),
        "country":      g("Country__c", "Billing_Country__c") or country_fallback,
        "region":       g("Region__c", "State__c"),
        "city":         g("City__c", "BillingCity"),
        "email":        g("Email__c", "Email"),
        "phone":        g("Phone__c", "Phone"),
        "website":      g("Website__c", "Website"),
        "address":      g("Address__c", "BillingStreet"),
        "raw_text":     json.dumps(rec, ensure_ascii=False)[:4000],
        "source_url":   MONASH_PAGE,
    }


def upsert_agents(conn: sqlite3.Connection, uni_id: int, agents: list) -> int:
    inserted = 0
    for a in agents:
        if not a.get("company_name"):
            continue
        try:
            conn.execute(
                """INSERT INTO agents
                   (university_id, company_name, contact_name, country, region,
                    city, email, phone, website, address, raw_text, source_url)
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
                     scraped_at=CURRENT_TIMESTAMP""",
                (
                    uni_id,
                    a["company_name"], a["contact_name"],
                    a["country"], a["region"], a["city"],
                    a["email"], a["phone"], a["website"],
                    a["address"], a["raw_text"], a["source_url"],
                ),
            )
            inserted += 1
        except sqlite3.Error as e:
            print(f"  ⚠  DB error for {a['company_name']!r}: {e}")
    conn.commit()
    return inserted


# ─── Playwright scraper ───────────────────────────────────────────────────────

def scrape_via_playwright(target_country: Optional[str] = None, dry_run: bool = False):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    collected: list = []   # raw Aura response bodies keyed by action

    def handle_response(response):
        if AURA_URL not in response.url:
            return
        try:
            body = response.text()
            if not body.strip():
                return
            data = json.loads(body)
        except Exception:
            return

        actions = data.get("actions", [])
        for action in actions:
            descriptor = action.get("descriptor", "")
            ret = action.get("returnValue")
            if ret is None:
                continue

            # getAllAgenciesWithPagination
            if "getAllAgenciesWithPagination" in descriptor:
                records = []
                if isinstance(ret, list):
                    records = ret
                elif isinstance(ret, dict):
                    records = ret.get("records", ret.get("agencies", []))
                    if not records:
                        # sometimes nested: {"data": {"records": [...]}}
                        for v in ret.values():
                            if isinstance(v, list) and v:
                                records = v
                                break
                for r in records:
                    if isinstance(r, dict):
                        collected.append(r)

            # getAllCountries — just log it
            elif "getAllCountries" in descriptor:
                countries = ret if isinstance(ret, list) else ret.get("countries", [])
                print(f"  Countries available: {len(countries)}")

    print(f"Launching Playwright to load {MONASH_PAGE} …")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,   # non-headless helps against Cloudflare
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-AU",
        )
        page = ctx.new_page()
        page.on("response", handle_response)

        # Navigate — Cloudflare challenge may appear; wait up to 90 s
        try:
            page.goto(MONASH_PAGE, wait_until="domcontentloaded", timeout=90_000)
        except PWTimeout:
            print("  ⚠  Page load timed out — trying to continue anyway")

        # Dismiss cookie banner if present
        for selector in [
            "button[id*='accept']", "button[class*='accept']",
            "button:has-text('Accept')", "button:has-text('Accept All')",
            "button:has-text('I Accept')", "#CybotCookiebotDialogBodyButtonAccept",
            ".cookie-accept", "[data-testid='cookie-accept']",
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"  Dismissed cookie banner ({selector})")
                    break
            except Exception:
                pass

        # Wait for the Salesforce Lightning app to load and fire initial requests
        print("  Waiting for Aura API responses …")
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass

        # If we only have partial data or need a specific country, try to
        # trigger the country dropdown programmatically.
        if target_country:
            print(f"  Filtering for country: {target_country}")
            # Give the LWC time to render
            page.wait_for_timeout(3000)

            # Try to find and select the country dropdown
            selected = False
            for combo_sel in [
                "lightning-combobox select",
                "select[name*='country']", "select[name*='Country']",
                "lightning-select select",
                "div[data-id='countrySelect'] select",
            ]:
                try:
                    combo = page.locator(combo_sel).first
                    if combo.is_visible(timeout=3000):
                        combo.select_option(label=target_country)
                        print(f"  Selected '{target_country}' via {combo_sel}")
                        selected = True
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass

            if not selected:
                # Try clicking a visible button/option that matches the country name
                try:
                    page.locator(f"text={target_country}").first.click(timeout=5000)
                    page.wait_for_timeout(3000)
                    print(f"  Clicked text '{target_country}'")
                except Exception:
                    print(f"  ⚠  Could not select country dropdown — collecting whatever loaded")

        # Paginate: keep clicking "Next" if present
        page_num = 1
        while True:
            next_btns = [
                "button:has-text('Next')",
                "button[class*='next']",
                "a:has-text('Next')",
                "lightning-button:has-text('Next')",
                ".slds-button:has-text('Next')",
            ]
            clicked_next = False
            for sel in next_btns:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000) and btn.is_enabled(timeout=1000):
                        btn.click()
                        page_num += 1
                        print(f"  → Page {page_num} …")
                        page.wait_for_timeout(2500)
                        clicked_next = True
                        break
                except Exception:
                    pass
            if not clicked_next:
                break

        # Final idle wait
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        # Take a screenshot for debugging
        debug_path = Path(__file__).parent / "data" / "monash_debug.png"
        page.screenshot(path=str(debug_path))
        print(f"  Screenshot saved → {debug_path}")

        browser.close()

    print(f"\n  Intercepted {len(collected)} raw Aura records")
    return collected


# ─── Alternative: intercept via route + XHR replay ───────────────────────────

def scrape_via_api_replay(target_country: Optional[str] = None, dry_run: bool = False):
    """
    Replay the Aura API using session cookies saved from a Playwright run.
    Requires: data/monash_cookies.json and data/monash_fwuid.txt
    """
    import requests as req_lib

    cookies_path = Path(__file__).parent / "data" / "monash_cookies.json"
    fwuid_path   = Path(__file__).parent / "data" / "monash_fwuid.txt"

    if not cookies_path.exists() or not fwuid_path.exists():
        print("  No saved session — run without --replay first to capture session")
        return []

    cookies = {c["name"]: c["value"] for c in json.loads(cookies_path.read_text())}
    fwuid   = fwuid_path.read_text().strip()
    print(f"  Using fwuid: {fwuid[:40]}…")
    print(f"  Using {len(cookies)} cookies: {list(cookies.keys())[:4]}")

    aura_ctx = json.dumps({
        "mode": "PROD",
        "fwuid": fwuid,
        "app": "c:AgentDatabaseApp",
        "loaded": {},
        "dn": [],
        "globals": {},
        "uad": True,
    })

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://unicrm.my.salesforce-sites.com",
        "Referer": MONASH_PAGE,
        "X-SFDC-Page-Scope-Id": "",
        "X-SFDC-Request-Id": "",
    }

    def fetch_page(page_num: int):
        if target_country:
            # Country-filtered action
            action_name = "searchAgenciesByCountryWithPagination"
            descriptor = f"apex://AgencyServerSideCC/ACTION${action_name}"
            params = {"pageNumber": page_num, "country": target_country}
        else:
            action_name = "getAllAgenciesWithPagination"
            descriptor = f"apex://AgencyServerSideCC/ACTION${action_name}"
            params = {"pageNumber": page_num}

        msg = json.dumps({"actions": [{
            "id": f"{page_num}00;a",
            "descriptor": descriptor,
            "callingDescriptor": "markup://c:Agencies",
            "params": params,
            "version": None,
        }]})
        url = f"{AURA_URL}?r={page_num}&other.AgencyServerSideCC.{action_name}=1"
        r = req_lib.post(
            url,
            data={"message": msg, "aura.context": aura_ctx, "aura.pageURI": "/AgentDB/s/"},
            cookies=cookies,
            headers=headers,
            timeout=30,
        )
        print(f"  Page {page_num}: HTTP {r.status_code}, {len(r.content)} bytes")
        if not r.content.strip():
            return [], 0
        try:
            data = r.json()
        except Exception as e:
            print(f"  JSON error: {e} | body: {r.text[:300]}")
            return [], 0

        for action in data.get("actions", []):
            ret = action.get("returnValue")
            if ret is None:
                continue
            if isinstance(ret, dict) and "agencies" in ret:
                total = ret.get("total", 0)
                return ret["agencies"], total
            if isinstance(ret, list):
                return ret, 0
        return [], 0

    all_records = []
    total_records = None
    page_num = 1

    while True:
        records, total = fetch_page(page_num)
        if total and total_records is None:
            total_records = total
            print(f"  Total records: {total_records}")
        if not records:
            print("  No records — stopping")
            break
        all_records.extend(r for r in records if isinstance(r, dict))
        print(f"  → {len(records)} records (cumulative: {len(all_records)})")
        if total_records and len(all_records) >= total_records:
            break
        page_num += 1
        time.sleep(0.4)

    return all_records


def scrape_via_api_replay_pages(
    target_country: Optional[str] = None,
    start_page: int = 2,
    already_collected: int = 0,
):
    """Replay Aura API for pages start_page onwards using saved session cookies."""
    import requests as req_lib

    cookies_path = Path(__file__).parent / "data" / "monash_cookies.json"
    fwuid_path   = Path(__file__).parent / "data" / "monash_fwuid.txt"
    if not cookies_path.exists() or not fwuid_path.exists():
        return []

    cookies = {c["name"]: c["value"] for c in json.loads(cookies_path.read_text())}
    fwuid   = fwuid_path.read_text().strip()

    aura_ctx = json.dumps({
        "mode": "PROD",
        "fwuid": fwuid,
        "app": "c:AgentDatabaseApp",
        "loaded": {},
        "dn": [],
        "globals": {},
        "uad": True,
    })
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://unicrm.my.salesforce-sites.com",
        "Referer": MONASH_PAGE,
    }

    if target_country:
        action_name = "searchAgenciesByCountryWithPagination"
        get_params = lambda p: {"pageNumber": p, "country": target_country}
    else:
        action_name = "getAllAgenciesWithPagination"
        get_params = lambda p: {"pageNumber": p}

    extra = []
    page_num = start_page
    total_records = None

    while True:
        params = get_params(page_num)
        msg = json.dumps({"actions": [{
            "id": f"{page_num}00;a",
            "descriptor": f"apex://AgencyServerSideCC/ACTION${action_name}",
            "callingDescriptor": "markup://c:Agencies",
            "params": params,
            "version": None,
        }]})
        url = f"{AURA_URL}?r={page_num}&other.AgencyServerSideCC.{action_name}=1"
        try:
            r = req_lib.post(
                url,
                data={"message": msg, "aura.context": aura_ctx, "aura.pageURI": "/AgentDB/s/"},
                cookies=cookies, headers=headers, timeout=20,
            )
        except Exception as e:
            print(f"  Request error p{page_num}: {e}")
            break

        if r.status_code == 401 or not r.content.strip():
            print(f"  Page {page_num}: session expired (HTTP {r.status_code})")
            break

        try:
            data = r.json()
        except Exception:
            print(f"  Page {page_num}: JSON parse error")
            break

        records = []
        for action in data.get("actions", []):
            ret = action.get("returnValue")
            if ret is None:
                continue
            if isinstance(ret, dict):
                if total_records is None:
                    total_records = ret.get("total", 0)
                recs = ret.get("agencies", [])
                if not recs:
                    for v in ret.values():
                        if isinstance(v, list):
                            recs = v
                            break
                records.extend(recs)
            elif isinstance(ret, list):
                records.extend(ret)

        if not records:
            break
        extra.extend(r for r in records if isinstance(r, dict))
        print(f"  Replay page {page_num}: +{len(records)} (extra total: {len(extra)})")

        if total_records and (already_collected + len(extra)) >= total_records:
            break
        page_num += 1
        time.sleep(0.4)

    return extra


# ─── Cookie + fwuid extraction helper ────────────────────────────────────────

def save_session_from_playwright(target_country: Optional[str] = None):
    """
    Runs Playwright, captures cookies + fwuid, saves them for API replay.
    Also intercepts Aura responses to get data in the same pass.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    collected_all: list = []       # from getAllAgenciesWithPagination (unfiltered)
    collected_filtered: list = []  # from searchAgenciesByCountryWithPagination
    total_pages: list = [1]        # mutable to update from inside closure
    fwuid_found: list = []
    ctx_payload_found: list = []   # full aura.context JSON for replay

    def _extract_agencies(ret):
        if isinstance(ret, list):
            return ret
        if isinstance(ret, dict):
            for key in ("agencies", "records", "data", "items", "results"):
                if isinstance(ret.get(key), list):
                    return ret[key]
            for v in ret.values():
                if isinstance(v, list):
                    return v
        return []

    def handle_request_finished(request):
        url = request.url

        # Extract fwuid from lightning.force.com static URL
        if "auraFW/javascript/" in url and not fwuid_found:
            m = re.search(r'auraFW/javascript/([^/?]+)', url)
            if m:
                fwuid_found.append(m.group(1))

        if AURA_URL not in url:
            return

        # Extract aura.context from URL-decoded POST body for replay
        try:
            from urllib.parse import unquote_plus
            post_data = request.post_data or ""
            decoded = unquote_plus(post_data)
            if not ctx_payload_found:
                m = re.search(r'"fwuid"\s*:\s*"([^"]+)"', decoded)
                if m and not fwuid_found:
                    fwuid_found.append(m.group(1))
                # Save full context for replay
                m2 = re.search(r'aura\.context=(\{[^&]+)', post_data)
                if m2:
                    ctx_payload_found.append(unquote_plus(m2.group(1)))
        except Exception:
            pass

        # Read response body
        try:
            resp = request.response()
            if resp is None:
                return
            body_bytes = resp.body()
            if not body_bytes:
                return
            body = body_bytes.decode("utf-8", errors="replace")
            data = json.loads(body)
        except Exception:
            return

        # Match by URL — Aura responses have empty descriptor fields
        is_country_filtered = "searchAgenciesByCountryWithPagination" in url
        is_all_agents = "getAllAgenciesWithPagination" in url or "getAgencies" in url

        if not (is_country_filtered or is_all_agents):
            return

        for action in data.get("actions", []):
            ret = action.get("returnValue")
            if ret is None:
                continue
            records = _extract_agencies(ret)
            # Update total page count
            if isinstance(ret, dict):
                total = ret.get("total", 0)
                page_size = ret.get("pageSize", 20) or 20
                if total:
                    total_pages[0] = max(total_pages[0], -(-total // page_size))
            if records:
                if is_country_filtered:
                    collected_filtered.extend(r for r in records if isinstance(r, dict))
                    print(f"  [country] +{len(records)} records (total {len(collected_filtered)})")
                else:
                    collected_all.extend(r for r in records if isinstance(r, dict))
                    print(f"  [all] +{len(records)} records (total {len(collected_all)})")

    print(f"Launching Playwright → {MONASH_PAGE}")
    with sync_playwright() as p:
        # Use system Chrome for a real browser fingerprint
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        launch_kwargs = dict(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        import os
        if os.path.exists(chrome_path):
            launch_kwargs["executable_path"] = chrome_path
            print("  Using system Google Chrome")
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-AU",
        )
        page = ctx.new_page()
        # Apply stealth patches to evade Cloudflare bot detection
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(page)
            print("  Stealth mode active")
        except Exception as e:
            print(f"  ⚠  Stealth init error: {e}")

        # Use requestfinished (body fully received) instead of response event
        page.on("requestfinished", handle_request_finished)

        # Go to the Monash page (embeds the SF LWC in an iframe)
        try:
            page.goto(MONASH_PAGE, wait_until="domcontentloaded", timeout=90_000)
        except PWTimeout:
            print("  ⚠  Page load timed out — continuing")

        # Accept cookies — use exact text match for "Accept all"
        page.wait_for_timeout(2000)
        for selector in [
            "button:text-is('Accept all')",
            "button:text-is('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"  Cookie modal dismissed ({selector})")
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # Wait for networkidle — the SF iframe should load and fire Aura requests
        print("  Waiting for page + iframe to load …")
        try:
            page.wait_for_load_state("networkidle", timeout=45_000)
        except PWTimeout:
            print("  ⚠  networkidle timed out — continuing")

        # Extra wait for LWC bootstrap
        page.wait_for_timeout(8000)

        # Scroll to where the iframe would be (below the fold)
        page.evaluate("window.scrollBy(0, 800)")
        page.wait_for_timeout(3000)

        total_all = len(collected_all)
        print(f"  After initial load: {total_all} unfiltered records, "
              f"fwuid={'yes' if fwuid_found else 'no'}, "
              f"est. {total_pages[0]} pages total")

        # Try to select country if specified — triggers searchAgenciesByCountryWithPagination
        if target_country:
            _try_select_country(page, target_country)
            page.wait_for_timeout(2000)

        # Paginate: click "Next" until no more pages
        page_num = 1
        while True:
            if target_country:
                pre = len(collected_filtered)
            else:
                pre = len(collected_all)
            clicked = _click_next_page(page)
            if not clicked:
                break
            page.wait_for_timeout(3000)
            if target_country:
                now = len(collected_filtered)
            else:
                now = len(collected_all)
            if now == pre:
                break
            page_num += 1
            print(f"  Paginated to page {page_num}")

        # Save cookies for API replay
        cookies = ctx.cookies()
        cookies_path = Path(__file__).parent / "data" / "monash_cookies.json"
        cookies_path.write_text(json.dumps(cookies, indent=2))
        print(f"  Saved {len(cookies)} cookies → {cookies_path}")

        if fwuid_found:
            fwuid_path = Path(__file__).parent / "data" / "monash_fwuid.txt"
            fwuid_path.write_text(fwuid_found[0])
            print(f"  Saved fwuid ({fwuid_found[0][:40]}…) → {fwuid_path}")
        else:
            print("  ⚠  fwuid not captured")

        if ctx_payload_found:
            ctx_path = Path(__file__).parent / "data" / "monash_aura_ctx.json"
            ctx_path.write_text(ctx_payload_found[0])
            print(f"  Saved aura.context → {ctx_path}")

        debug_path = Path(__file__).parent / "data" / "monash_debug.png"
        page.screenshot(path=str(debug_path))
        print(f"  Screenshot → {debug_path}")

        browser.close()

    # Return the appropriate collection
    if target_country and collected_filtered:
        result = collected_filtered
    elif target_country and not collected_filtered:
        print("  ⚠  Country-filtered collection empty, falling back to all-countries")
        result = [r for r in collected_all
                  if str(r.get("Country__c", "")).lower() == target_country.lower()]
    else:
        result = collected_all

    print(f"\n  Intercepted {len(result)} records via Playwright")

    # If cookies were saved, immediately try to get remaining pages via API replay
    # (must be done before session expires)
    cookies_path = Path(__file__).parent / "data" / "monash_cookies.json"
    fwuid_path = Path(__file__).parent / "data" / "monash_fwuid.txt"
    if cookies_path.exists() and fwuid_path.exists() and len(result) > 0:
        # Estimate if there are more pages
        page_size = 20
        need_more = (target_country and len(result) % page_size == 0) or \
                    (not target_country and len(result) % page_size == 0)
        if need_more:
            print("  Replaying API for remaining pages while session is fresh …")
            extra = scrape_via_api_replay_pages(
                target_country=target_country,
                start_page=2,
                already_collected=len(result),
            )
            if extra:
                result = result + extra
                print(f"  Total after replay: {len(result)}")

    return result


def _try_select_country(page, country: str):
    """Try to select a country in the Salesforce LWC agent DB app."""
    from playwright.sync_api import TimeoutError as PWTimeout
    page.wait_for_timeout(2000)

    # Strategy 1: native <select> element (lightning-combobox renders one)
    for sel in [
        "lightning-combobox select",
        "select[name*='ountry']",
        "select[name*='Country']",
        "lightning-select select",
    ]:
        try:
            combo = page.locator(sel).first
            if combo.is_visible(timeout=3000):
                combo.select_option(label=country)
                print(f"  Country '{country}' selected via {sel}")
                page.wait_for_timeout(4000)
                return
        except Exception:
            pass

    # Strategy 2: custom combobox button + option list
    for btn_sel in [
        "button.slds-combobox__form-element",
        "[aria-haspopup='listbox']",
        "input[aria-autocomplete='list']",
    ]:
        try:
            btn = page.locator(btn_sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                page.wait_for_timeout(1000)
                opt = page.locator(f"[role='option']:has-text('{country}')").first
                opt.click(timeout=5000)
                print(f"  Country '{country}' via custom combobox ({btn_sel})")
                page.wait_for_timeout(4000)
                return
        except Exception:
            pass

    print(f"  ⚠  Could not select country '{country}' — will collect all countries")


def _click_next_page(page):
    from playwright.sync_api import TimeoutError as PWTimeout
    for sel in [
        "button:has-text('>')",          # Monash SF uses ">" symbol
        "button:text-is('>')",
        "button[title='Next']",
        "button[title='Next Page']",
        "button:has-text('Next')",
        ".slds-button:has-text('Next')",
        "a:has-text('Next')",
        "lightning-button:has-text('Next')",
        "[aria-label='Next Page']",
        "[aria-label='Next']",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000) and btn.is_enabled(timeout=500):
                btn.click()
                return True
        except Exception:
            pass
    return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape Monash agent database")
    parser.add_argument("--country",  default=None,
                        help="Filter to one country (e.g. Thailand)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print agents, do not write to DB")
    parser.add_argument("--replay",   action="store_true",
                        help="Replay via saved session cookies (skip Playwright)")
    args = parser.parse_args()

    # ── Scrape ────────────────────────────────────────────────────────────────
    if args.replay:
        raw_records = scrape_via_api_replay(args.country, args.dry_run)
    else:
        raw_records = save_session_from_playwright(args.country)

    if not raw_records:
        print("\n✗  No records collected — check monash_debug.png for page state")
        return

    # ── Parse ─────────────────────────────────────────────────────────────────
    agents = [parse_record(r, args.country or "") for r in raw_records]
    agents = [a for a in agents if a.get("company_name")]

    # Filter by country if requested
    if args.country:
        agents = [
            a for a in agents
            if a["country"].lower() == args.country.lower() or not a["country"]
        ]

    print(f"\nParsed {len(agents)} agents"
          + (f" for {args.country}" if args.country else ""))

    if args.dry_run:
        for a in agents[:30]:
            print(f"  {a['company_name']:<50}  {a['country']:<20}  {a['city']}")
        if len(agents) > 30:
            print(f"  … and {len(agents)-30} more")
        return

    # ── Write to DB ───────────────────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    uni_id = get_university_id(conn)
    print(f"University ID: {uni_id} ({UNIVERSITY_NAME})")

    inserted = upsert_agents(conn, uni_id, agents)
    print(f"✓  {inserted} agents upserted into agents table")

    # Log the scrape
    conn.execute(
        """INSERT INTO scrape_log (university_id, status, agents_found, method, notes)
           VALUES (?, 'success', ?, 'playwright_aura_intercept', ?)""",
        (uni_id, inserted,
         f"Playwright network interception of Salesforce Aura API; "
         f"country filter: {args.country or 'ALL'}"),
    )
    conn.execute(
        """UPDATE universities SET last_scraped=?, scrape_status='scraped'
           WHERE id=?""",
        (datetime.utcnow().isoformat(), uni_id),
    )
    conn.commit()
    conn.close()

    print(f"\nDone.  Run normalise_agents.py to apply canonical names.")


if __name__ == "__main__":
    main()
