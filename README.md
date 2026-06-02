# 🎓 Australian University Agent Scraper & Database

A complete system to scrape, store, query, and report on publicly listed education agents from Australian university websites.

---

## Setup

```bash
cd agent_scraper
pip install -r requirements.txt
```

---

## 1. Scraping (`scrape.py`)

### First run — scrape all universities
```bash
python scrape.py
```

### Check status without scraping
```bash
python scrape.py --list
```

### Scrape a specific university (partial name match)
```bash
python scrape.py --uni "Monash"
python scrape.py --uni "Melbourne"
python scrape.py --uni "Queensland"
```

### Re-scrape everything (refresh existing data)
```bash
python scrape.py --refresh
```

### Just load the university list into the DB (no scraping)
```bash
python scrape.py --load-only
```

**How the scraper works:**
The scraper tries multiple strategies per page, in order:
1. **HTML tables** — most structured pages
2. **Repeated card/list elements** — modern styled pages
3. **Definition lists** — some older university sites
4. **Email-anchored text blocks** — pages with inline contact info
5. **Country/region headings + entries** — common "agents by country" layout
6. **Raw text fallback** — saves page preview for manual review

---

## 2. Querying (`query.py`)

### Show all agents
```bash
python query.py agents
```

### Filter by country
```bash
python query.py agents --country "China"
python query.py agents --country "India"
python query.py agents --country "Indonesia"
```

### Filter by university
```bash
python query.py agents --university "Monash"
```

### Find agents with email addresses
```bash
python query.py agents --has-email
```

### Search by email domain
```bash
python query.py agents --email "@gmail.com"
python query.py agents --email "education"
```

### Full-text search (searches name, email, raw text)
```bash
python query.py search "IDP"
python query.py search "education group"
python query.py search "Beijing"
```

### Statistics
```bash
python query.py stats                    # By country (default)
python query.py stats --by university    # By university
python query.py stats --by country_university  # Cross-tab
```

### Coverage overview
```bash
python query.py coverage
```

### Export to Excel
```bash
python query.py agents --country "Vietnam" --export vietnam_agents.xlsx
python query.py stats --by country --export country_stats.xlsx
python query.py coverage --export coverage.xlsx
```

---

## 3. Social Media Reports (`social_report.py`)

### Full report (HTML + Excel)
```bash
python social_report.py
```
Outputs to `./reports/` directory.

### Country-specific report
```bash
python social_report.py --country "China"
python social_report.py --country "India"
```

### University-specific report
```bash
python social_report.py --university "Monash"
```

### Format options
```bash
python social_report.py --format html      # HTML only
python social_report.py --format excel     # Excel only
python social_report.py --format both      # Both (default)
```

### Custom output directory
```bash
python social_report.py --output ./my_reports/
```

**Report contents:**
- KPI summary cards (total agents, countries, email coverage %)
- Agent distribution by country (with bar chart in Excel)
- Agent count by university
- Multi-university agents (agencies authorised by 2+ universities — top outreach targets)
- 5 ready-to-use social media posts (LinkedIn, Twitter/X, Instagram, Facebook)
- One-click copy buttons in HTML version

---

## File Structure

```
agent_scraper/
├── scrape.py          — Main scraper
├── query.py           — Database query CLI
├── social_report.py   — Report generator
├── requirements.txt   — Python dependencies
├── data/
│   ├── australian_university_agent_pages.xlsx  — Source URL list
│   └── agents.db      — SQLite database (created on first run)
└── reports/           — Generated reports (created automatically)
```

---

## Database Schema

**`universities`** — 42 Australian universities with their agent page URLs  
**`agents`** — Individual agent records (company, contact, country, email, phone, website)  
**`scrape_log`** — History of all scrape attempts  

---

## Notes on Coverage

Some university pages use JavaScript-rendered finders (e.g. Macquarie, some QLD universities). 
These won't be scraped by this tool as they require a browser. Pages like these will show 
`scrape_status = 'raw_text_fallback'` — you'll need to visit those pages manually and 
copy/paste agent lists, or use a Selenium-based scraper for those specific URLs.

Pages confirmed to have static HTML agent lists (best scrape results expected):
- Southern Cross University
- University of New England  
- University of Wollongong
- University of Sydney (static HTML table)
- Deakin University
- La Trobe University
- University of Tasmania
