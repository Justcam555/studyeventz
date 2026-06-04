# Backend ingest — Cloudflare Worker + D1

Captures the front-end analytics events that `events.html` was already queuing locally, and stores them centrally in a managed SQLite database (D1) hosted at the Cloudflare edge.

The static site itself stays on GitHub Pages — this Worker is a small, separate endpoint on a `*.workers.dev` URL (or later your own `api.studyeventz.com` route).

## What gets stored

One row per tracked event. Columns: `id`, `type`, `ts`, `session_id`, `page`, `event_id`, `event_name`, `agent_name`, `agent_id`, `event_date`, `clicked_url`, `user_agent`, `referrer`, `ip_hash`, `received_at`.

**Raw IP is never stored** — it's SHA-256-hashed with a server-only salt (12 hex chars persisted). Same IP → same hash, so within-day deduping is possible without identifying anyone.

## Safeguards built in

| Layer | Where |
|---|---|
| CORS allow-list (only studyeventz.com + www) | `worker.js` → `ALLOWED_ORIGINS` |
| Public site key (`?k=...`) | `worker.js` → checks `env.SITE_KEY`. Set in `wrangler.toml` `[vars]`. Not real auth — friction only |
| Per-IP rate limit (100 req / 60 s) | `worker.js` → `env.RATE_LIMITER.limit({ key: ip })`. Configured in `wrangler.toml`. |
| Body size cap (10 KB) | `worker.js` → `MAX_BODY_BYTES` (checked twice: `Content-Length` header + post-read length) |
| Batch cap (100 events / POST) | `worker.js` → `MAX_BATCH` |
| Event type allow-list | `worker.js` → `ALLOWED_TYPES` |
| Required fields enforced (`type`, `ts`, `session_id`) | `worker.js` → `validEvent()` |
| Per-field length cap | `worker.js` → `FIELD_CAPS` table |
| IP hashing with secret salt | `worker.js` → `hashIp()`. Salt is `env.IP_HASH_SALT` (Cloudflare secret) |

## One-time setup

```bash
# 1. Install wrangler (Cloudflare's CLI)
brew install cloudflare-wrangler         # or: npm i -g wrangler

# 2. Authenticate (browser opens for OAuth)
wrangler login

cd backend

# 3. Create the D1 database
wrangler d1 create studyeventz_analytics
# → copy the printed database_id, paste it into wrangler.toml under
#   [[d1_databases]] → database_id (replace "REPLACE_WITH_OUTPUT_OF_D1_CREATE")

# 4. Apply the schema
wrangler d1 execute studyeventz_analytics --file schema.sql --remote

# 5. Set the IP-hash salt (keeps IP hashes unguessable). Use any random string:
openssl rand -hex 32 | wrangler secret put IP_HASH_SALT

# 6. Deploy the Worker
wrangler deploy

# Output prints the deployed URL. Copy it — looks like:
#   https://studyeventz-ingest.<your-cf-handle>.workers.dev
```

## Wire it to the front-end

1. Open `/Users/cameronagents/projects/intelligence/university-platform/build_events_page.py`
2. Find the `INGEST_URL = ""` line near the top
3. Set it to the Worker URL plus `/track`, e.g.
   ```python
   INGEST_URL = "https://studyeventz-ingest.<your-cf-handle>.workers.dev/track"
   ```
4. From the repo root:
   ```bash
   python3 build_events_page.py
   git add events.html build_events_page.py
   git commit -m "Wire frontend analytics to backend ingest"
   git push
   ```

GitHub Pages redeploys within ~30 s and the front-end will start POSTing events.

## Smoke test the endpoint

After deploy but before wiring the front-end:

```bash
WORKER_URL="https://studyeventz-ingest.<your-cf-handle>.workers.dev"

# Should return 200 with {"saved":1,...}
curl -i -X POST "$WORKER_URL/track?k=studyeventz-public-2026" \
  -H "Content-Type: application/json" \
  -H "Origin: https://www.studyeventz.com" \
  -d '{
    "type": "event_impression",
    "ts": "2026-06-03T12:00:00Z",
    "session_id": "smoke-test",
    "page": "/events.html",
    "event_id": "999",
    "event_name": "Smoke Test Event",
    "agent_name": "studyeventz QA",
    "date": "2026-06-03"
  }'

# Confirm it landed in D1
wrangler d1 execute studyeventz_analytics --remote \
  --command "SELECT id, type, event_name, agent_name, ip_hash, received_at FROM events WHERE session_id='smoke-test'"
```

Negative smoke tests — these should each fail with the expected status:

```bash
# Bad site key → 403
curl -sw "%{http_code}\n" -o /dev/null -X POST "$WORKER_URL/track?k=wrong" \
  -H "Content-Type: application/json" -H "Origin: https://www.studyeventz.com" \
  -d '{"type":"event_impression","ts":"2026-06-03T12:00:00Z","session_id":"x"}'

# Wrong Origin → 403
curl -sw "%{http_code}\n" -o /dev/null -X POST "$WORKER_URL/track?k=studyeventz-public-2026" \
  -H "Content-Type: application/json" -H "Origin: https://evil.example.com" \
  -d '{"type":"event_impression","ts":"2026-06-03T12:00:00Z","session_id":"x"}'

# Unknown event type → returns 200 with {"saved":0,"skipped":1}
curl -s -X POST "$WORKER_URL/track?k=studyeventz-public-2026" \
  -H "Content-Type: application/json" -H "Origin: https://www.studyeventz.com" \
  -d '{"type":"hack","ts":"2026-06-03T12:00:00Z","session_id":"x"}'

# Missing session_id → returns 200 with {"saved":0,"skipped":1}
curl -s -X POST "$WORKER_URL/track?k=studyeventz-public-2026" \
  -H "Content-Type: application/json" -H "Origin: https://www.studyeventz.com" \
  -d '{"type":"event_impression","ts":"2026-06-03T12:00:00Z"}'

# Clean up the smoke-test row when done
wrangler d1 execute studyeventz_analytics --remote \
  --command "DELETE FROM events WHERE session_id='smoke-test'"
```

## Event submissions

The same worker also exposes **POST /submit** for organizers to submit events via `submit.html`.

- Same CORS allow-list, site key, rate limit and IP-hashing as `/track`
- Stricter validation: `organizer`, `event_name`, `event_date` (YYYY-MM-DD), `registration_url` (http/https) are all required; optional fields are length-capped
- Writes to a separate `submissions` table with `status='pending'`
- **Nothing publishes automatically.** Review with `submissions_report.py`, approve, then manually add to `agents.db` so it lands in the public listing on the next build

Smoke test:

```bash
curl -i -X POST "$WORKER_URL/submit?k=studyeventz-public-2026" \
  -H "Content-Type: application/json" \
  -H "Origin: https://www.studyeventz.com" \
  -d '{
    "organizer": "Test Agent",
    "event_name": "Test Submission",
    "event_date": "2026-12-01",
    "event_time": "10:00 - 12:00",
    "location": "Bangkok",
    "registration_url": "https://example.com/event"
  }'

# Confirm row landed (status=pending)
wrangler d1 execute studyeventz_analytics --remote \
  --command "SELECT id, organizer, event_name, status FROM submissions ORDER BY id DESC LIMIT 5"

# Clean up
wrangler d1 execute studyeventz_analytics --remote \
  --command "DELETE FROM submissions WHERE organizer='Test Agent'"
```

Review pending locally:

```bash
python3 submissions_report.py                       # show pending
python3 submissions_report.py --approve 42 --note "manually added to agents.db"
python3 submissions_report.py --reject 43 --note "duplicate of #41"
```

Approval flow (for now, manual):

1. `python3 submissions_report.py` → review pending rows
2. For each approved submission, INSERT a row into `data/agents.db` events table (or the agent rows it depends on). Future improvement: `approve_submission.py` to automate.
3. `python3 submissions_report.py --approve <id>` to mark it as handled
4. Next Sunday build → public `events.json` updates

## Updating the analytics report

The report runs **locally** — it queries D1 via `wrangler` and prints to your terminal. Nothing is sent off-machine.

```bash
python3 analytics_report.py               # last 30 days
python3 analytics_report.py --days 7      # last week
```

## Local development

To run the Worker on your machine for development:

```bash
cd backend
wrangler dev --local                      # http://localhost:8787/track
```

Set `INGEST_URL = "http://localhost:8787/track"` temporarily in `build_events_page.py` to test against your local Worker.

## Rotating the salt

If the salt is compromised, rotate:

```bash
openssl rand -hex 32 | wrangler secret put IP_HASH_SALT
wrangler deploy
```

Note: existing rows keep their old `ip_hash` values. New events use the new salt — they won't link to old rows for the same IP. Acceptable for v1.

## Costs

Cloudflare Workers free tier covers:
- 100,000 requests/day → well above expected traffic
- 5,000,000 D1 reads/day, 100,000 D1 writes/day
- 5 GB D1 storage

You can run this for free indefinitely at studyeventz's traffic levels.
