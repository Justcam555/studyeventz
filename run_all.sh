#!/usr/bin/env bash
# ============================================================
#  Australian University Agent Scraper — Full Pipeline
#  Run with:  bash run_all.sh
#  Or give this script to Claude Code and say "run it"
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colours ──────────────────────────────────────────────────
GREEN='\033[0;32m'; AMBER='\033[0;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${AMBER}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   🎓  AU University Agent Scraper — Full Pipeline   ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Step 0: Check Python ─────────────────────────────────────
info "Checking Python..."
PYTHON=$(command -v python3 || command -v python || error "Python not found. Install Python 3.8+")
PY_VER=$($PYTHON --version 2>&1)
success "Using $PY_VER at $PYTHON"

# ── Step 1: Install dependencies ─────────────────────────────
info "Installing Python dependencies..."
$PYTHON -m pip install -q -r requirements.txt \
  && success "Dependencies installed" \
  || error "pip install failed. Try: pip install -r requirements.txt manually"

# ── Step 2: Create directories ───────────────────────────────
mkdir -p data reports
success "Directories ready"

# ── Step 3: Check source Excel exists ────────────────────────
EXCEL="data/australian_university_agent_pages.xlsx"
if [[ ! -f "$EXCEL" ]]; then
  error "Source file not found: $EXCEL\nPlace the Excel file at $SCRIPT_DIR/$EXCEL and re-run."
fi
success "Source data found: $EXCEL"

# ── Step 4: Load university list into DB ─────────────────────
info "Loading university list into database..."
$PYTHON scrape.py --load-only \
  && success "Universities loaded into agents.db" \
  || error "Failed to initialise database"

# ── Step 5: Scrape all universities ──────────────────────────
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
info "Starting scrape of all Australian universities..."
info "This will take a few minutes (polite ~2s delay between requests)"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

$PYTHON scrape.py \
  && success "Scrape complete" \
  || warn "Scrape finished with some errors (normal for JS-rendered pages)"

# ── Step 6: Print coverage summary ───────────────────────────
echo ""
info "Coverage summary:"
$PYTHON query.py coverage

# ── Step 7: Stats by country ─────────────────────────────────
echo ""
info "Agent distribution by country:"
$PYTHON query.py stats --by country

# ── Step 8: Export full agent database to Excel ──────────────
echo ""
info "Exporting full agent database to Excel..."
AGENTS_EXPORT="reports/all_agents_$(date +%Y%m%d).xlsx"
$PYTHON query.py agents --export "$AGENTS_EXPORT" \
  && success "Full agent list → $AGENTS_EXPORT" \
  || warn "Export failed"

# ── Step 9: Export stats ──────────────────────────────────────
STATS_EXPORT="reports/stats_by_country_$(date +%Y%m%d).xlsx"
$PYTHON query.py stats --by country --export "$STATS_EXPORT" \
  && success "Country stats → $STATS_EXPORT" \
  || warn "Stats export failed"

# ── Step 10: Generate social media report ────────────────────
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
info "Generating social media report (HTML + Excel)..."
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

$PYTHON social_report.py --format both --output reports/ \
  && success "Social media report generated in reports/" \
  || warn "Report generation failed"

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                  ✅  Pipeline complete!              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  📁  All outputs in: $SCRIPT_DIR/reports/"
echo ""
ls -lh reports/ 2>/dev/null | grep -v "^total" | awk '{print "     " $NF "  ("$5")"}' || true
echo ""
echo "  💡  Next steps:"
echo "      Query agents:    python query.py agents --country 'China'"
echo "      Search:          python query.py search 'IDP Education'"
echo "      Country report:  python social_report.py --country 'India'"
echo ""
