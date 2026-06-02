#!/usr/bin/env python3
"""
fix_linkedin.py — Fill LinkedIn followers/employees for agents where it's missing.
Uses the correct actor: harvestapi/linkedin-company (field: companies=[url])

Usage:
  python3 fix_linkedin.py                   # Thailand + Nepal
  python3 fix_linkedin.py --country Thailand
"""
import argparse, os, sqlite3, time, json
from pathlib import Path
from apify_client import ApifyClient

DB_PATH   = Path(__file__).parent / "data" / "agents.db"
COUNTRIES = ["Thailand", "Nepal"]


def fix_country(conn, country, client):
    rows = conn.execute("""
        SELECT id, canonical_name, linkedin_url
        FROM agent_social
        WHERE LOWER(country) = LOWER(?)
          AND linkedin_url IS NOT NULL
          AND (linkedin_followers IS NULL AND li_employee_count IS NULL)
        ORDER BY canonical_name
    """, (country,)).fetchall()

    print(f"\n{country} — {len(rows)} agents need LinkedIn data")
    for i, (sid, name, li_url) in enumerate(rows, 1):
        url = li_url if li_url.startswith("http") else "https://" + li_url
        print(f"  [{i}/{len(rows)}] {name[:50]} → {url[:60]}")
        try:
            run = client.actor("harvestapi/linkedin-company").call(
                run_input={"companies": [url]}, timeout_secs=120
            )
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
            if items:
                item = items[0]
                followers = item.get("followerCount") or item.get("followersCount")
                employees = item.get("employeeCount") or item.get("staffCount") or item.get("employeeCountRange")
                conn.execute(
                    "UPDATE agent_social SET linkedin_followers=?, li_employee_count=? WHERE id=?",
                    (followers, employees, sid)
                )
                conn.commit()
                print(f"    ✓ followers={followers}, employees={employees}")
            else:
                print(f"    — no data returned")
        except Exception as e:
            print(f"    ERROR: {e}")
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--country")
    args = parser.parse_args()

    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN not set")
    client = ApifyClient(api_token)
    conn = sqlite3.connect(DB_PATH)

    countries = [args.country] if args.country else COUNTRIES
    for country in countries:
        fix_country(conn, country, client)

    conn.close()
    print("\n✅ LinkedIn fix complete.")


if __name__ == "__main__":
    main()
