# Claude Code Instructions — AU University Agent Scraper

When working in this project, you are helping maintain and run a scraper that
builds a database of education agents listed publicly by Australian universities.

## Quick start

```bash
bash run_all.sh
```

That single command does everything: installs deps, scrapes all 42 universities,
builds the SQLite database, exports Excel files, and generates social media reports.

## Project files

| File | Purpose |
|------|---------|
| `scrape.py` | Scrapes university agent pages → `data/agents.db` |
| `query.py` | CLI to query/export the database |
| `social_report.py` | Generates HTML + Excel social media reports |
| `run_all.sh` | Full pipeline in one command |
| `data/australian_university_agent_pages.xlsx` | Source list of 42 universities + URLs |
| `data/agents.db` | SQLite database (created on first run) |
| `reports/` | All generated reports land here |

## Common tasks

### Re-scrape everything fresh
```bash
python scrape.py --refresh
```

### Scrape one university
```bash
python scrape.py --uni "Monash"
```

### Query agents
```bash
python query.py agents --country "China"
python query.py agents --university "Melbourne" --has-email
python query.py search "IDP Education"
python query.py stats --by country
python query.py coverage
```

### Export to Excel
```bash
python query.py agents --export reports/agents.xlsx
python query.py agents --country "Vietnam" --export reports/vietnam.xlsx
```

### Generate social media report
```bash
python social_report.py                          # Full report
python social_report.py --country "India"        # Country-specific
python social_report.py --university "Monash"    # Uni-specific
```

## Handling JS-rendered pages

Some university pages (e.g. Macquarie, some QLD universities) load their agent
finders via JavaScript and won't scrape with simple HTTP requests. The scraper
will log these as `raw_text_fallback`. To handle them:

1. Check `python query.py coverage` — universities with 0 agents after scraping
2. For those, consider using Playwright/Selenium for those specific URLs
3. Or manually export their agent lists and import via:
   ```python
   import sqlite3, pandas as pd
   # Load manual CSV → insert into agents table
   ```

## Anthropic API model

Always use `claude-sonnet-4-6` for all Anthropic API calls in this project unless
the user explicitly instructs otherwise. Cost control is a priority — do not
upgrade to Opus without being asked.

## Database schema (SQLite)

```sql
universities (id, name, website, agent_page_url, status_note, last_scraped, scrape_status)
agents       (id, university_id, company_name, contact_name, country, region, city,
              email, phone, website, address, raw_text, source_url, scraped_at)
scrape_log   (id, university_id, scraped_at, status, agents_found, method, notes)
```
