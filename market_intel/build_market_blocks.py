#!/usr/bin/env python3
"""
build_market_blocks.py — Generate HTML market size blocks from processed JSON.

Takes data/processed/market_size_YYYY-MM.json and outputs:
  - A standalone HTML file (market_blocks_YYYY-MM.html) for review
  - Per-country HTML snippets ready to embed in agent-network.html

Usage:
    python3 build_market_blocks.py                     # latest JSON file
    python3 build_market_blocks.py --file data/processed/market_size_2025-12.json
    python3 build_market_blocks.py --country Thailand  # single country preview
"""

import argparse
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROC_DIR = BASE_DIR / "data" / "processed"
OUT_DIR  = BASE_DIR / "data" / "processed"

AEI_URL = "https://www.education.gov.au/international-education-data-and-research/international-student-monthly-summary-and-data-tables"
HA_URL  = "https://data.gov.au/data/dataset/student-visas"


def arrow_html(yoy):
    """Return HTML badge for YoY trend."""
    if yoy is None:
        return '<span class="trend-neutral">—</span>'
    if yoy > 5:
        cls = "trend-up"
        label = f"↑ +{yoy}%"
    elif yoy < -5:
        cls = "trend-down"
        label = f"↓ {yoy}%"
    else:
        cls = "trend-flat"
        label = f"→ {yoy:+.1f}%"
    return f'<span class="{cls}">{label}</span>'


def fmt_num(v):
    if v is None or v == 0:
        return '<span class="zero">—</span>'
    return f"{v:,}"


def build_block_html(out: dict) -> str:
    """Build a self-contained HTML card for one country."""
    country   = out["country"]
    aei_month = out["aei_month"]
    c         = out["commencements"]
    t         = c["total"]
    bs        = c["by_sector"]
    vg        = out.get("offshore_visa_grants", {})

    # Year columns (exclude yoy_pct)
    yrs = sorted(k for k in t if k != "yoy_pct")
    yr_headers = "".join(f'<th>{y}</th>' for y in yrs)

    def sector_row(label: str, data: dict, is_total=False) -> str:
        cls = " class=\"total-row\"" if is_total else ""
        cells = "".join(f"<td>{fmt_num(data.get(y))}</td>" for y in yrs)
        trend = arrow_html(data.get("yoy_pct"))
        return f"<tr{cls}><td class='sector-label'>{label}</td>{cells}<td>{trend}</td></tr>"

    sector_rows = ""
    for label in ["Higher Education", "VET", "ELICOS", "Other"]:
        sector_rows += sector_row(label, bs.get(label, {}))
    sector_rows += sector_row("Total", t, is_total=True)

    # Visa grants bar
    visa_section = ""
    if vg:
        v_yrs = sorted(k for k in vg if k != "yoy_pct")
        if v_yrs:
            max_grants = max((vg.get(y, 0) or 0) for y in v_yrs) or 1
            bars = ""
            for vy in v_yrs:
                val = vg.get(vy, 0) or 0
                pct = round(val / max_grants * 100)
                bars += f"""
                <div class="visa-bar-row">
                    <div class="visa-year">{vy}</div>
                    <div class="visa-bar-wrap">
                        <div class="visa-bar" style="width:{pct}%"></div>
                    </div>
                    <div class="visa-val">{val:,}</div>
                </div>"""
            yoy_badge = arrow_html(vg.get("yoy_pct")) if "yoy_pct" in vg else ""
            visa_section = f"""
            <div class="visa-section">
                <div class="section-label">Offshore Visa Grants {yoy_badge}</div>
                <div class="visa-bars">{bars}</div>
            </div>"""

    return f"""
    <div class="market-block" id="market-{country.lower().replace(' ', '-')}">
        <div class="market-header">
            <div class="market-title">Market Size</div>
            <div class="market-meta">{country} &nbsp;·&nbsp; AEI {aei_month}</div>
        </div>

        <div class="commencements-section">
            <div class="section-label">Student Commencements</div>
            <table class="comm-table">
                <thead>
                    <tr>
                        <th>Sector</th>
                        {yr_headers}
                        <th>Trend</th>
                    </tr>
                </thead>
                <tbody>
                    {sector_rows}
                </tbody>
            </table>
        </div>

        {visa_section}

        <div class="market-footer">
            <a href="{AEI_URL}" target="_blank" rel="noopener">
                Latest monthly figures → Australian Dept of Education
            </a>
            <span class="attribution">
                Data: <a href="{AEI_URL}" target="_blank">Australian Department of Education</a>
                &nbsp;|&nbsp;
                <a href="{HA_URL}" target="_blank">Department of Home Affairs</a>
            </span>
        </div>
    </div>"""


CSS = """
.market-block {
    background: #1a1a2e;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 24px;
    margin: 20px 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #e0e0e0;
    max-width: 700px;
}
.market-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid #2a2a4a;
}
.market-title {
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #888;
}
.market-meta {
    font-size: 13px;
    color: #888;
}
.section-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #666;
    margin-bottom: 10px;
    margin-top: 16px;
}
.comm-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}
.comm-table th {
    text-align: right;
    padding: 6px 8px;
    font-size: 12px;
    color: #666;
    font-weight: 600;
    border-bottom: 1px solid #2a2a4a;
}
.comm-table th:first-child { text-align: left; }
.comm-table td {
    padding: 7px 8px;
    text-align: right;
    border-bottom: 1px solid #1e1e3a;
    font-variant-numeric: tabular-nums;
}
.comm-table td.sector-label { text-align: left; color: #ccc; }
.comm-table tr.total-row td {
    font-weight: 700;
    color: #fff;
    border-top: 1px solid #3a3a5a;
    border-bottom: none;
}
.zero { color: #444; }
.trend-up   { color: #4caf50; font-weight: 600; font-size: 12px; }
.trend-down { color: #f44336; font-weight: 600; font-size: 12px; }
.trend-flat { color: #ff9800; font-weight: 600; font-size: 12px; }
.trend-neutral { color: #555; font-size: 12px; }
.visa-section { margin-top: 20px; }
.visa-bars { display: flex; flex-direction: column; gap: 6px; }
.visa-bar-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
}
.visa-year { width: 42px; color: #888; font-size: 12px; }
.visa-bar-wrap { flex: 1; background: #111128; border-radius: 3px; height: 8px; }
.visa-bar { background: #0f3460; border-radius: 3px; height: 8px; transition: width 0.3s; }
.visa-val { width: 60px; text-align: right; color: #aaa; font-size: 12px; font-variant-numeric: tabular-nums; }
.market-footer {
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px solid #2a2a4a;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.market-footer a {
    color: #5c8fcf;
    text-decoration: none;
    font-size: 13px;
}
.market-footer a:hover { text-decoration: underline; }
.attribution { font-size: 11px; color: #555; }
.attribution a { color: #4a7ab5; }
"""


def build_standalone_html(blocks_html: str, month_tag: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Market Intelligence Blocks — {month_tag}</title>
<style>
body {{ background: #0d0d1a; padding: 40px; }}
h1 {{ color: #e0e0e0; font-family: sans-serif; font-size: 18px; margin-bottom: 30px; }}
{CSS}
</style>
</head>
<body>
<h1>Market Size Blocks — {month_tag}</h1>
{blocks_html}
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to market_size JSON file")
    parser.add_argument("--country", help="Filter to single country")
    args = parser.parse_args()

    if args.file:
        json_path = Path(args.file)
    else:
        files = sorted(PROC_DIR.glob("market_size_*.json"), reverse=True)
        if not files:
            print("No market_size JSON found in data/processed/")
            print("Run process_market_data.py first.")
            return
        json_path = files[0]
        print(f"Using latest: {json_path.name}")

    with open(json_path) as f:
        all_countries = json.load(f)

    if args.country:
        all_countries = [c for c in all_countries if c["country"].lower() == args.country.lower()]
        if not all_countries:
            print(f"Country '{args.country}' not found in {json_path.name}")
            return

    blocks_html = ""
    for out in all_countries:
        blocks_html += build_block_html(out)
        print(f"  ✓ Built block for {out['country']}")

    month_tag = re.search(r"\d{4}-\d{2}", json_path.name)
    month_tag = month_tag.group(0) if month_tag else "latest"

    out_path = OUT_DIR / f"market_blocks_{month_tag}.html"
    with open(out_path, "w") as f:
        f.write(build_standalone_html(blocks_html, month_tag))
    print(f"\n✅ Preview HTML → {out_path}")

    # Also save snippets dict as JSON for embedding
    snippets = {out["country"]: build_block_html(out) for out in all_countries}
    snip_path = OUT_DIR / f"market_snippets_{month_tag}.json"
    with open(snip_path, "w") as f:
        json.dump(snippets, f, indent=2)
    print(f"✅ Snippets  → {snip_path}  (embed key = country name)")

    # Print embed CSS for reference
    print(f"\n── Embed CSS (add once to your HTML <style> block) ─────────────")
    print("See CSS variable in build_market_blocks.py or copy from the preview HTML.")


if __name__ == "__main__":
    main()
