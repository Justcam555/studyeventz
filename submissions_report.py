#!/usr/bin/env python3
"""
submissions_report.py — Review pending event submissions sitting in D1.

Reads the remote `submissions` table via the wrangler CLI and prints
each pending row in a copy-paste-friendly format you can use to
approve or reject manually.

Usage:
    python3 submissions_report.py                  # all pending
    python3 submissions_report.py --status all     # everything
    python3 submissions_report.py --status approved
    python3 submissions_report.py --status rejected

Mutating (run from the repo root):
    python3 submissions_report.py --approve <id>
    python3 submissions_report.py --reject  <id> --note "duplicate"
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent / "backend"
DB_NAME = "studyeventz_analytics"


def _wrangler_or_die():
    if not shutil.which("wrangler"):
        sys.exit(
            "wrangler CLI not found. Install with one of:\n"
            "  brew install cloudflare-wrangler\n"
            "  npm i -g wrangler\n"
            "Then run: wrangler login"
        )


def query(sql: str) -> list[dict]:
    _wrangler_or_die()
    result = subprocess.run(
        ["wrangler", "d1", "execute", DB_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, cwd=BACKEND_DIR,
    )
    if result.returncode != 0:
        print(f"wrangler stderr:\n{result.stderr}", file=sys.stderr)
        sys.exit(f"wrangler failed (exit {result.returncode})")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"could not parse wrangler output: {e}\nstdout (first 500): {result.stdout[:500]}")
    if not data:
        return []
    return data[0].get("results", []) if isinstance(data, list) else data.get("results", [])


def execute(sql: str) -> int:
    """Run a non-query statement; return rows-affected (best effort)."""
    _wrangler_or_die()
    result = subprocess.run(
        ["wrangler", "d1", "execute", DB_NAME, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, cwd=BACKEND_DIR,
    )
    if result.returncode != 0:
        print(f"wrangler stderr:\n{result.stderr}", file=sys.stderr)
        sys.exit(f"wrangler failed (exit {result.returncode})")
    return 1


def _sql_escape(s: str) -> str:
    return (s or "").replace("'", "''")


def render(rows: list[dict]) -> None:
    if not rows:
        print("  (no submissions)")
        return
    for r in rows:
        print()
        print(f"  ── #{r['id']}  {r['status'].upper()}  ──  received {r['received_at']}")
        print(f"     organizer:    {r['organizer']}")
        print(f"     event_name:   {r['event_name']}")
        print(f"     event_date:   {r['event_date']}   event_time: {r.get('event_time') or '(none)'}")
        if r.get("location"):
            print(f"     location:     {r['location']}")
        print(f"     register:     {r['registration_url']}")
        if r.get("submitter_name") or r.get("submitter_email"):
            print(f"     submitter:    {r.get('submitter_name') or '(no name)'}  <{r.get('submitter_email') or 'no email'}>")
        if r.get("notes"):
            print(f"     notes:        {r['notes']}")
        if r.get("ip_hash"):
            print(f"     ip_hash:      {r['ip_hash']}")
        if r.get("reviewer_notes"):
            print(f"     reviewer:     {r['reviewer_notes']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", default="pending",
                    choices=["pending", "approved", "rejected", "all"],
                    help="Filter by status (default: pending)")
    ap.add_argument("--approve", type=int, metavar="ID", help="Mark a submission approved")
    ap.add_argument("--reject", type=int, metavar="ID", help="Mark a submission rejected")
    ap.add_argument("--note", default="", help="Reviewer note to attach with --approve/--reject")
    args = ap.parse_args()

    if args.approve and args.reject:
        sys.exit("Pass --approve OR --reject, not both")

    if args.approve:
        note = _sql_escape(args.note)
        execute(
            f"UPDATE submissions SET status='approved', reviewed_at=CURRENT_TIMESTAMP, "
            f"reviewer_notes='{note}' WHERE id={int(args.approve)} AND status='pending'"
        )
        print(f"Marked submission {args.approve} approved.")
        print("Reminder: nothing is published automatically — you still need to copy")
        print("the event into agents.db (and a future cron pickup). See README.")
        return 0

    if args.reject:
        note = _sql_escape(args.note)
        execute(
            f"UPDATE submissions SET status='rejected', reviewed_at=CURRENT_TIMESTAMP, "
            f"reviewer_notes='{note}' WHERE id={int(args.reject)} AND status='pending'"
        )
        print(f"Marked submission {args.reject} rejected.")
        return 0

    where = "" if args.status == "all" else f"WHERE status='{args.status}'"
    rows = query(f"SELECT * FROM submissions {where} ORDER BY received_at DESC LIMIT 100")

    label = "all" if args.status == "all" else args.status
    print(f"\n=== {len(rows)} {label} submission(s) ===")
    render(rows)

    if args.status == "pending" and rows:
        print()
        print("To approve:  python3 submissions_report.py --approve <id> [--note '...']")
        print("To reject:   python3 submissions_report.py --reject  <id> [--note '...']")

    return 0


if __name__ == "__main__":
    sys.exit(main())
