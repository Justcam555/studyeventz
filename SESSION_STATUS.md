# Agent Scraper — Session Status
**Last updated: 2026-04-14**

---

## What This Project Is

A pipeline that:
1. Scrapes education agent directories to build a SQLite database of agents (Thailand + Nepal)
2. Researches each agent's social media presence + Google Maps data
3. Enriches profiles with live TikTok, Instagram, YouTube, LinkedIn data via Apify
4. Generates two HTML reports published to GitHub Pages

---

## Key Directories

| Path | Purpose |
|---|---|
| `~/Desktop/Agent Scraper/` | All scraping/enrichment scripts + SQLite DB |
| `~/Desktop/Agent Scraper/data/agents.db` | Main database (9.6MB) |
| `~/Desktop/marketintelligencereports/` | GitHub-published HTML outputs |

---

## Database Schema

### `agents` table
One row per **agent × university** relationship.
- `id`, `university_id`, `canonical_name`, `company_name`, `country`, `city`, `email`, `phone`, `website`
- ~168 unique agents across Thailand (65) and Nepal (103)
- Each agent may appear 1–16+ times (once per university they represent)

### `universities` table
- `id`, `name`, `website`, `agent_page_url`, `scrape_status`

### `agent_social` table
One row per **agent × country** (UNIQUE constraint).
- Basic: `agent_id`, `canonical_name`, `country`, `website_url`, `website_active`
- Social links: `facebook_url`, `instagram_handle`, `instagram_url`, `linkedin_url`, `linkedin_followers`, `li_employee_count`
- TikTok: `tiktok_handle`, `tiktok_url`, `tiktok_followers`, `tiktok_video_count`, `tiktok_total_views`, `tiktok_top_video_views`, `tiktok_avg_views`, `tiktok_engagement_rate`, `tiktok_last_post`, `tiktok_videos` (JSON)
- Instagram: `instagram_followers`, `ig_post_count`, `ig_last_post`
- YouTube: `yt_channel_name`, `yt_channel_url`, `yt_subscribers`, `yt_total_views`, `yt_video_count`, `yt_top_video_title`, `yt_top_video_views`, `yt_videos` (JSON)
- Google: `google_rating`, `google_reviews`, `google_place_id`, `google_maps_url`
- Scores: `presence_score` (0–10), `platform_enriched_at`, `researched_at`

---

## Scripts

### `research_social.py`
**Purpose:** Phase 1 — website check, scrape social links from homepage, Google Places lookup, calculate presence score.
```bash
python3 research_social.py                    # Thailand + Nepal
python3 research_social.py --country Thailand
python3 research_social.py --refresh          # re-research all
```
**Status:** Complete for both countries.

### `enrich_agents.py`
**Purpose:** Phase 2 — live Apify scraping for TikTok, Instagram, YouTube, LinkedIn. Rebuilds HTML profile page.
```bash
python3 enrich_agents.py                      # Thailand + Nepal
python3 enrich_agents.py --country Thailand
python3 enrich_agents.py --discover-only      # only find TikTok handles from websites
python3 enrich_agents.py --report-only        # rebuild HTML without re-scraping
python3 enrich_agents.py --refresh            # re-scrape already-enriched agents
```
**Apify actors used:**
- TikTok: `clockworks/tiktok-scraper` — input: `{"profiles": ["@handle"], "maxItems": 10}`
- Instagram: `apify/instagram-scraper` — input: `{"searchType": "user", "searchQueries": ["handle"], "maxItems": 1}`
- YouTube: `streamers/youtube-scraper` — input: `{"searchQueries": ["name"], "maxResults": 10}`
- LinkedIn: `harvestapi/linkedin-company` — input: `{"companies": ["https://linkedin.com/company/slug"]}`

**Known limitations:**
- Instagram follower counts always return N/A (field empty in user-search mode — needs Graph API)
- TikTok keyword mode broken — only profiles mode works (3 companies have no TikTok)
- YouTube returns search results not channel videos — uses owned/3rd-party badge to distinguish

### `fix_linkedin.py`
**Purpose:** Backfill LinkedIn data for agents where it's missing (e.g. after a failed run).
```bash
python3 fix_linkedin.py                   # Thailand + Nepal
python3 fix_linkedin.py --country Nepal
```
**Status:** Run for Thailand ✓. Run for Nepal after enrichment completes.

### `research_social.py`
Already documented above.

---

## Current Enrichment Status (as of 2026-04-14 ~8:30pm)

| Country | Total | Enriched | TikTok handles | LinkedIn data |
|---|---|---|---|---|
| Thailand | 65 | **65 ✓ complete** | 18 | 10 |
| Nepal | 103 | **38 / 103 — IN PROGRESS** | 10 | 27 |

**Nepal enrichment is running in background: PID 40773**
```bash
# Check progress
python3 -c "import sqlite3; conn=sqlite3.connect('data/agents.db'); print(conn.execute(\"SELECT COUNT(*) FROM agent_social WHERE country='Nepal' AND platform_enriched_at IS NOT NULL\").fetchone()[0], '/103')"

# Check if still running
ps aux | grep enrich_agents | grep -v grep
```

---

## When Nepal Finishes — Next Steps

```bash
# 1. Backfill any failed LinkedIn calls for Nepal
python3 fix_linkedin.py --country Nepal

# 2. Rebuild final HTML with all 168 enriched profiles
cd ~/Desktop/Agent\ Scraper && python3 enrich_agents.py --report-only

# 3. Commit and push to GitHub
cd ~/Desktop/marketintelligencereports
git add agent-profile.html agent-network.html
git commit -m "Add fully enriched profiles for Thailand + Nepal"
git push
```

---

## Output Files

### `agent-profile.html` (427KB)
- Location: `~/Desktop/marketintelligencereports/agent-profile.html`
- 168 agents embedded as JSON, looked up via `?id=` URL param
- Sections per profile: KPI strip, TikTok video chart, Instagram, YouTube, LinkedIn, Google Maps, Facebook (coming soon placeholder), University Partners, Presence Score breakdown
- Visual style matches `one-education-report.html` (dark gradient cover, red accent cards)

### `agent-network.html` (809KB)
- Location: `~/Desktop/marketintelligencereports/agent-network.html`
- Multi-tab: Map, Directory, University Matrix, Agent Network
- Directory tab has "Profile" link column for Thailand/Nepal agents → links to `agent-profile.html?id=X`

---

## Key Data Findings — Thailand

### TikTok (top performers)
| Agent | Handle | Total Views | Top Video |
|---|---|---|---|
| Education For Life | @eflstudyaustralia | 1,112,139 | 937,000 |
| WIN Education | @win.educationthailand | 277,081 | 124,700 |
| CETA Thailand | @cetathailand | 255,851 | 251,500 (viral) |
| OEC Global Education | @oecglobaleducation | 214,471 | 110,400 |
| One Education Consulting | @oneeducationthailand | ~15,918 | ~11,500 |

### LinkedIn (top performers)
| Agent | Followers | Employees |
|---|---|---|
| Adventus Education | 37,765 | 296 (global brand) |
| Liu Cheng International Group | 15,584 | 145 |
| SOL Edu | 3,257 | 69 |
| One Education Consulting | 590 | 9 |

### Google Maps
- One Education: **4.8★, 72 reviews** — highest rating among all competitors

---

## Known Bugs / Fixed

### Bug: Thailand presence scores slightly low
- **Cause:** `get_agents()` SELECT omitted `facebook_url` from agent_social, so `calc_score()` treated all agents as having no Facebook. Thailand was already running when this was caught.
- **Effect:** Scores ~1.5 points lower than correct for agents with Facebook.
- **Status:** Fixed in code for Nepal run onward. Thailand scores in DB are slightly understated.
- **To fix Thailand:** Run `python3 enrich_agents.py --country Thailand --refresh` (re-scrapes everything, uses Apify credits) OR write a targeted SQL UPDATE.

### Bug: University partners showing only 1 instead of all
- **Cause:** `rebuild_profiles()` joined `agents` on `a.id = s.agent_id` — but `agent_social` stores one representative agent_id, while 16 university relationships live across 16 different `agents` rows.
- **Fix applied:** Changed join to `a.canonical_name = s.canonical_name AND LOWER(a.country) = LOWER(s.country)` so all university rows are included in `GROUP_CONCAT`.
- **Status:** Fixed ✓ — Hands On Education now shows all 16 partners.

---

## Planned Work (Tomorrow+)

### University logo cards
- Currently university partners display as a plain sorted text list on agent profiles
- Plan: replace with visual logo cards (logo image + name + country flag + link to agent page)
- Blocked on: user to provide university logo assets
- File to edit: `enrich_agents.py` → `_build_html()` function, university partners section (~line 1065)
- After editing: run `python3 enrich_agents.py --report-only` to rebuild

### Facebook data (Thursday)
- Facebook API access arriving Thursday
- "Facebook — coming soon" placeholder already in profile HTML
- Actor to use: `apify/facebook-pages-scraper` (public page metrics)
- **Do NOT use `curious_coder/facebook-ads-scraper`** — ignored result limits, cost $22.42 in one run

### Instagram follower counts
- Requires Instagram Graph API (Facebook Developer app with `instagram_basic` permission)
- Currently always returns N/A

### run_weekly.sh (intelligence-agent project)
- Still needs: 5 recipient email addresses + Gmail app password
- Will schedule as Monday 7am ICT cron

---

## Environment Variables Required

```bash
export APIFY_API_TOKEN='...'
export GOOGLE_PLACES_API_KEY='...'
```
