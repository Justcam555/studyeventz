#!/usr/bin/env bash
# Safe weekly StudyEventz refresh. Never publish an all-zero event result.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"
MIN_CURRENT_EVENTS="${MIN_CURRENT_EVENTS:-1}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$ROOT_DIR/data/snapshots"
BACKUP_DB="$BACKUP_DIR/agents_before_events_${STAMP}.db"

mkdir -p "$BACKUP_DIR"
cp data/agents.db "$BACKUP_DB"

restore_database() {
  cp "$BACKUP_DB" data/agents.db
  echo "Restored database snapshot; no event pages were published." >&2
}

if ! "$PYTHON_BIN" scrape_events.py --all; then
  restore_database
  exit 1
fi

CURRENT_EVENTS="$(sqlite3 data/agents.db "SELECT COUNT(*) FROM events WHERE date BETWEEN date('now') AND date('now', '+30 days');")"
if [ "$CURRENT_EVENTS" -lt "$MIN_CURRENT_EVENTS" ]; then
  echo "Safety stop: only $CURRENT_EVENTS current events found (minimum: $MIN_CURRENT_EVENTS)." >&2
  restore_database
  exit 1
fi

"$PYTHON_BIN" build_events_page.py
git add -A
if git diff --cached --quiet; then
  echo "No event-site changes to publish."
else
  git commit -m "Weekly events update"
  git push origin main
fi

# Keep the most recent ten database snapshots for diagnosis/recovery.
ls -1t "$BACKUP_DIR"/agents_before_events_*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
