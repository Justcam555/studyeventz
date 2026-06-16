-- D1 schema for studyeventz analytics ingest.
-- One row per tracked frontend event.
-- Apply with:
--   wrangler d1 execute studyeventz_analytics --file schema.sql --remote

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,           -- event_impression / event_register_click / logo_click / location_click / calendar_click / line_click
    ts           TEXT NOT NULL,           -- client-side ISO timestamp from the browser
    session_id   TEXT NOT NULL,           -- per-tab UUID, set by sessionStorage on first track()
    page         TEXT,                    -- e.g. /thailand/events.html
    country      TEXT,                    -- 'thailand', 'vietnam', etc. — frontend market the event came from
    event_id     TEXT,                    -- DB id of the event in agents.db.events (string for safety)
    event_name   TEXT,
    agent_name   TEXT,
    agent_id     TEXT,                    -- if populated by future frontend changes
    event_date   TEXT,                    -- the event's date, not the analytics timestamp
    clicked_url  TEXT,                    -- registration_url / maps_url / calendar_url depending on type
    user_agent   TEXT,
    referrer     TEXT,
    ip_hash      TEXT,                    -- SHA-256(ip + salt) truncated to 12 hex chars; NOT raw IP
    geo_country  TEXT,                    -- visitor country (ISO-3166 alpha-2) from Cloudflare edge geo-IP
    received_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
-- Existing deployments: add the newer columns with
--   ALTER TABLE events ADD COLUMN country TEXT;
--   ALTER TABLE events ADD COLUMN geo_country TEXT;

CREATE INDEX IF NOT EXISTS idx_events_type        ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_event_id    ON events(event_id);
CREATE INDEX IF NOT EXISTS idx_events_agent_name  ON events(agent_name);
CREATE INDEX IF NOT EXISTS idx_events_session_id  ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_event_date  ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_country     ON events(country);


-- Event submissions from agents/organizers via the public form.
-- Nothing here is auto-published — review with submissions_report.py and
-- approve manually via SQL or approve_submission.py before it lands in
-- agents.db events table.

CREATE TABLE IF NOT EXISTS submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    country           TEXT,                 -- 'thailand', 'vietnam' — market the form was on
    organizer         TEXT NOT NULL,        -- agent / company / university name
    event_name        TEXT NOT NULL,
    event_date        TEXT NOT NULL,        -- ISO YYYY-MM-DD
    event_time        TEXT,                 -- free text e.g. "14:00 - 16:00"
    location          TEXT,                 -- city / venue / "Online"
    registration_url  TEXT NOT NULL,        -- landing page URL
    submitter_name    TEXT,
    submitter_email   TEXT,
    notes             TEXT,                 -- extra context from the submitter
    user_agent        TEXT,
    referrer          TEXT,
    ip_hash           TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    received_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    reviewed_at       DATETIME,
    reviewer_notes    TEXT
);

CREATE INDEX IF NOT EXISTS idx_submissions_status      ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_received_at ON submissions(received_at);
CREATE INDEX IF NOT EXISTS idx_submissions_event_date  ON submissions(event_date);
CREATE INDEX IF NOT EXISTS idx_submissions_country     ON submissions(country);
