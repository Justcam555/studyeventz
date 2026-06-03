#!/usr/bin/env python3
"""
analytics_report.py — Local summary of ingested studyeventz events.

Queries the remote D1 database via the wrangler CLI. Output prints to the
terminal. Nothing leaves your machine beyond what wrangler already sends to
Cloudflare.

Usage:
    python3 analytics_report.py                # last 30 days
    python3 analytics_report.py --days 7       # last week
    python3 analytics_report.py --raw          # also dump the latest 20 rows
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent / "backend"
DB_NAME = "studyeventz_analytics"


def _check_wrangler():
    if not shutil.which("wrangler"):
        sys.exit(
            "wrangler CLI not found. Install with one of:\n"
            "  brew install cloudflare-wrangler\n"
            "  npm i -g wrangler\n"
            "Then run: wrangler login"
        )
    if not BACKEND_DIR.exists():
        sys.exit(f"Backend dir not found at {BACKEND_DIR}")


def query(sql: str) -> list[dict]:
    """Run a SQL query via wrangler against the remote D1 database."""
    result = subprocess.run(
        ["wrangler", "d1", "execute", DB_NAME, "--remote", "--json", "--command", sql],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
    )
    if result.returncode != 0:
        print(f"wrangler stderr:\n{result.stderr}", file=sys.stderr)
        sys.exit(f"wrangler failed (exit {result.returncode})")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse wrangler output: {e}\nstdout (first 500 chars): {result.stdout[:500]}")
    if not data:
        return []
    # wrangler returns a list with one stmt-result; results key holds rows
    return data[0].get("results", []) if isinstance(data, list) else data.get("results", [])


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Look back this many days (default 30)")
    ap.add_argument("--raw", action="store_true", help="Also dump latest 20 rows")
    args = ap.parse_args()

    _check_wrangler()

    where = f"WHERE received_at >= datetime('now', '-{args.days} days')"

    # Totals
    section(f"Totals (last {args.days} days)")
    rows = query(f"""
        SELECT type, COUNT(*) AS n
        FROM events {where}
        GROUP BY type ORDER BY n DESC
    """)
    total = sum(r["n"] for r in rows)
    print(f"  {total} events across {len(rows)} type(s)")
    for r in rows:
        print(f"    {r['n']:5d}  {r['type']}")

    if total == 0:
        print("\n(nothing to summarise yet — check that the frontend is POSTing to the worker)")
        return 0

    # Impressions per event
    section("Impressions per event")
    rows = query(f"""
        SELECT event_id, agent_name,
               MAX(event_name) AS event_name,
               COUNT(*) AS n
        FROM events {where} AND type='event_impression'
        GROUP BY event_id ORDER BY n DESC LIMIT 50
    """)
    for r in rows:
        agent = (r["agent_name"] or "")[:25]
        name = (r["event_name"] or "")[:55]
        print(f"  {r['n']:4d}  {agent:<25}  {name}")

    # Register clicks per event
    section("Register clicks per event")
    rows = query(f"""
        SELECT event_id, agent_name,
               MAX(event_name) AS event_name,
               COUNT(*) AS n
        FROM events {where} AND type='event_register_click'
        GROUP BY event_id ORDER BY n DESC LIMIT 50
    """)
    if not rows:
        print("  (no register clicks recorded yet)")
    for r in rows:
        agent = (r["agent_name"] or "")[:25]
        name = (r["event_name"] or "")[:55]
        print(f"  {r['n']:4d}  {agent:<25}  {name}")

    # Logo clicks per agent
    section("Logo clicks per agent")
    rows = query(f"""
        SELECT agent_name, COUNT(*) AS n
        FROM events {where} AND type='logo_click'
        GROUP BY agent_name ORDER BY n DESC LIMIT 30
    """)
    if not rows:
        print("  (no logo clicks recorded yet)")
    for r in rows:
        print(f"  {r['n']:4d}  {(r['agent_name'] or '')[:60]}")

    # Location + calendar clicks
    section("Other click types")
    for typ in ("location_click", "calendar_click", "line_click"):
        rows = query(f"SELECT COUNT(*) AS n FROM events {where} AND type='{typ}'")
        n = rows[0]["n"] if rows else 0
        print(f"  {n:5d}  {typ}")

    # CTR per event (register / impression)
    section("CTR per event (register / impression)")
    rows = query(f"""
        WITH imp AS (
            SELECT event_id, COUNT(*) AS imps,
                   MAX(event_name) AS event_name
            FROM events {where} AND type='event_impression'
            GROUP BY event_id
        ),
        clk AS (
            SELECT event_id, COUNT(*) AS clks
            FROM events {where} AND type='event_register_click'
            GROUP BY event_id
        )
        SELECT imp.event_id, imp.event_name, imp.imps,
               COALESCE(clk.clks, 0) AS clks,
               ROUND(100.0 * COALESCE(clk.clks, 0) / imp.imps, 2) AS ctr_pct
        FROM imp LEFT JOIN clk ON imp.event_id = clk.event_id
        ORDER BY ctr_pct DESC, imp.imps DESC LIMIT 30
    """)
    if not rows:
        print("  (no impressions yet — CTR will populate once both impressions and clicks land)")
    else:
        print(f"  {'CTR':<8}{'IMP':<6}{'CLK':<6}  EVENT")
        for r in rows:
            ctr = f"{r['ctr_pct']}%" if r["ctr_pct"] is not None else "0%"
            print(f"  {ctr:<8}{r['imps']:<6}{r['clks']:<6}  {(r['event_name'] or '')[:55]}")

    if args.raw:
        section("Latest 20 raw rows")
        rows = query("""
            SELECT id, received_at, type, agent_name, event_name, ip_hash
            FROM events ORDER BY id DESC LIMIT 20
        """)
        for r in rows:
            print(f"  {r['id']:5d}  {r['received_at']}  {r['type']:<22}  "
                  f"{(r['agent_name'] or '')[:20]:<20}  {(r['event_name'] or '')[:40]}  ip={r['ip_hash']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
