// studyeventz-ingest — POST /track endpoint backed by Cloudflare D1.
//
// Layers (each rejects on its own):
//   1. CORS — Origin must be studyeventz.com or www.studyeventz.com
//   2. Site key — ?k=... must match env.SITE_KEY (public token, friction only)
//   3. Per-IP rate limit — 100 req/min via env.RATE_LIMITER
//   4. Body size cap — 10 KB
//   5. Batch cap — at most 100 events per POST
//   6. Per-event validation — type must be in allowlist; type/ts/session_id required
//   7. Per-field length cap — anything overly long is truncated, not stored as-is
//
// IP addresses are never stored raw — they're hashed with SHA-256(ip + salt)
// truncated to 12 hex chars. The salt is held in env.IP_HASH_SALT (Cloudflare secret).

const ALLOWED_ORIGINS = new Set([
  "https://www.studyeventz.com",
  "https://studyeventz.com",
]);

const ALLOWED_TYPES = new Set([
  "event_impression",
  "event_register_click",
  "logo_click",
  "location_click",
  "calendar_click",
  "line_click",
]);

const MAX_BODY_BYTES = 10 * 1024;  // 10 KB
const MAX_BATCH = 100;

const FIELD_CAPS = {
  type:         50,
  ts:           50,
  session_id:   100,
  page:         200,
  country:      30,
  event_id:     50,
  event_name:   500,
  agent_name:   300,
  agent_id:     50,
  date:         20,
  clicked_url:  1000,
};

function corsHeaders(origin) {
  // Echo the requesting origin if allowed, else default to www (so the browser
  // sees something deterministic). Vary tells caches the response is origin-dependent.
  // Allow-Credentials: true is needed for fetch(keepalive) + sendBeacon
  // because those modes can make the browser treat the request as credentialed.
  // Safe here because the endpoint never reads cookies — see Worker code.
  const allowed = ALLOWED_ORIGINS.has(origin) ? origin : "https://www.studyeventz.com";
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Credentials": "true",
    "Vary": "Origin",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function json(status, body, origin) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(origin), "Content-Type": "application/json" },
  });
}

async function hashIp(ip, salt) {
  if (!ip || !salt) return null;
  const enc = new TextEncoder();
  const digest = await crypto.subtle.digest("SHA-256", enc.encode(`${ip}|${salt}`));
  const bytes = new Uint8Array(digest).slice(0, 6);  // 6 bytes = 12 hex chars
  return Array.from(bytes).map(b => b.toString(16).padStart(2, "0")).join("");
}

function cap(s, max) {
  if (s == null) return null;
  const str = String(s);
  return str.length > max ? str.slice(0, max) : str;
}

function validEvent(ev) {
  // Required: object shape, allowed type, non-empty ts and session_id
  if (!ev || typeof ev !== "object" || Array.isArray(ev)) return false;
  if (typeof ev.type !== "string" || !ALLOWED_TYPES.has(ev.type)) return false;
  if (typeof ev.ts !== "string" || ev.ts.length === 0 || ev.ts.length > FIELD_CAPS.ts) return false;
  if (typeof ev.session_id !== "string" || ev.session_id.length === 0) return false;
  return true;
}

// ─── /submit (event-submission form) ──────────────────────────────────────
// Smaller body cap than /track (single submission, not a batch).
const SUBMIT_MAX_BODY_BYTES = 5 * 1024;

const SUBMIT_FIELD_CAPS = {
  country:          30,
  organizer:        300,
  event_name:       500,
  event_date:       20,
  event_time:       50,
  location:         300,
  registration_url: 1000,
  submitter_name:   200,
  submitter_email:  300,
  notes:            2000,
};

const URL_RE   = /^https?:\/\/[^\s]+$/i;
const DATE_RE  = /^\d{4}-\d{2}-\d{2}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

async function handleSubmit(request, env, origin) {
  const contentLength = parseInt(request.headers.get("content-length") || "0", 10);
  if (contentLength > SUBMIT_MAX_BODY_BYTES) {
    return json(413, { error: "payload_too_large" }, origin);
  }
  let body;
  try {
    const text = await request.text();
    if (text.length > SUBMIT_MAX_BODY_BYTES) {
      return json(413, { error: "payload_too_large" }, origin);
    }
    body = JSON.parse(text);
  } catch {
    return json(400, { error: "invalid_json" }, origin);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return json(400, { error: "expected_object" }, origin);
  }

  // Per-field validation
  const errors = {};
  const get = (k) => (typeof body[k] === "string" ? body[k].trim() : "");

  const country          = get("country");
  const organizer        = get("organizer");
  const eventName        = get("event_name");
  const eventDate        = get("event_date");
  const eventTime        = get("event_time");
  const location         = get("location");
  const registrationUrl  = get("registration_url");
  const submitterName    = get("submitter_name");
  const submitterEmail   = get("submitter_email");
  const notes            = get("notes");

  if (!organizer)       errors.organizer = "required";
  if (!eventName)       errors.event_name = "required";
  if (!eventDate)       errors.event_date = "required";
  else if (!DATE_RE.test(eventDate)) errors.event_date = "must be YYYY-MM-DD";
  if (!registrationUrl) errors.registration_url = "required";
  else if (!URL_RE.test(registrationUrl)) errors.registration_url = "must start with http:// or https://";
  if (submitterEmail && !EMAIL_RE.test(submitterEmail)) errors.submitter_email = "invalid email";

  // Length caps (string already trimmed)
  for (const [k, max] of Object.entries(SUBMIT_FIELD_CAPS)) {
    const v = get(k);
    if (v && v.length > max) errors[k] = `too long (>${max} chars)`;
  }

  if (Object.keys(errors).length) {
    return json(400, { error: "validation_failed", fields: errors }, origin);
  }

  const ipHash    = await hashIp(request.headers.get("CF-Connecting-IP"), env.IP_HASH_SALT);
  const userAgent = cap(request.headers.get("User-Agent"), 500);
  const referrer  = cap(request.headers.get("Referer"), 500);

  try {
    const result = await env.DB.prepare(`
      INSERT INTO submissions (
        country, organizer, event_name, event_date, event_time, location,
        registration_url, submitter_name, submitter_email, notes,
        user_agent, referrer, ip_hash
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      country         ? cap(country,         SUBMIT_FIELD_CAPS.country)         : null,
      cap(organizer,        SUBMIT_FIELD_CAPS.organizer),
      cap(eventName,        SUBMIT_FIELD_CAPS.event_name),
      cap(eventDate,        SUBMIT_FIELD_CAPS.event_date),
      eventTime       ? cap(eventTime,       SUBMIT_FIELD_CAPS.event_time)       : null,
      location        ? cap(location,        SUBMIT_FIELD_CAPS.location)        : null,
      cap(registrationUrl,  SUBMIT_FIELD_CAPS.registration_url),
      submitterName   ? cap(submitterName,   SUBMIT_FIELD_CAPS.submitter_name)   : null,
      submitterEmail  ? cap(submitterEmail,  SUBMIT_FIELD_CAPS.submitter_email)  : null,
      notes           ? cap(notes,           SUBMIT_FIELD_CAPS.notes)           : null,
      userAgent,
      referrer,
      ipHash
    ).run();
    const newId = result?.meta?.last_row_id ?? null;
    return json(200, { ok: true, id: newId, status: "pending" }, origin);
  } catch (e) {
    console.error("submission insert failed:", String(e).slice(0, 200));
    return json(500, { error: "insert_failed" }, origin);
  }
}


export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const url = new URL(request.url);

    // 1a. Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // 1b. Routing — only POST to /i (ingest) or /s (submit).
    // Paths use single letters to avoid being blocked by adblocker pattern
    // matchers that target /track, /collect, /event etc.
    // /track and /submit kept as aliases so existing curls + earlier
    // documentation continue to work.
    const isTrack  = (url.pathname === "/i" || url.pathname === "/track")  && request.method === "POST";
    const isSubmit = (url.pathname === "/s" || url.pathname === "/submit") && request.method === "POST";
    if (!isTrack && !isSubmit) {
      return json(404, { error: "not_found" }, origin);
    }

    // 2. CORS allow-list (both endpoints)
    if (!ALLOWED_ORIGINS.has(origin)) {
      return json(403, { error: "origin_not_allowed" }, origin);
    }

    // 3. Site key
    if (url.searchParams.get("k") !== env.SITE_KEY) {
      return json(403, { error: "bad_site_key" }, origin);
    }

    // 4. Rate limit per IP (both endpoints share the same bucket)
    const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
    if (env.RATE_LIMITER && typeof env.RATE_LIMITER.limit === "function") {
      try {
        const { success } = await env.RATE_LIMITER.limit({ key: ip });
        if (!success) return json(429, { error: "rate_limited" }, origin);
      } catch (_) { /* limiter unavailable — fail open, not closed */ }
    }

    // Dispatch
    if (isSubmit) return handleSubmit(request, env, origin);

    // /track from here on
    const contentLength = parseInt(request.headers.get("content-length") || "0", 10);
    if (contentLength > MAX_BODY_BYTES) {
      return json(413, { error: "payload_too_large" }, origin);
    }

    let body;
    try {
      const text = await request.text();
      if (text.length > MAX_BODY_BYTES) {
        return json(413, { error: "payload_too_large" }, origin);
      }
      body = JSON.parse(text);
    } catch {
      return json(400, { error: "invalid_json" }, origin);
    }

    const events = Array.isArray(body) ? body : [body];
    if (events.length === 0 || events.length > MAX_BATCH) {
      return json(400, { error: "bad_batch_size" }, origin);
    }

    const ipHash = await hashIp(ip, env.IP_HASH_SALT);
    const userAgent = cap(request.headers.get("User-Agent"), 500);
    const referrer = cap(request.headers.get("Referer"), 500);

    let saved = 0;
    let skipped = 0;

    for (const ev of events) {
      if (!validEvent(ev)) { skipped++; continue; }
      const clickedUrl = ev.registration_url || ev.maps_url || ev.calendar_url || null;
      try {
        await env.DB.prepare(`
          INSERT INTO events (
            type, ts, session_id, page, country, event_id, event_name,
            agent_name, agent_id, event_date, clicked_url,
            user_agent, referrer, ip_hash
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `).bind(
          cap(ev.type,         FIELD_CAPS.type),
          cap(ev.ts,           FIELD_CAPS.ts),
          cap(ev.session_id,   FIELD_CAPS.session_id),
          cap(ev.page,         FIELD_CAPS.page),
          cap(ev.country,      FIELD_CAPS.country),
          cap(ev.event_id,     FIELD_CAPS.event_id),
          cap(ev.event_name,   FIELD_CAPS.event_name),
          cap(ev.agent_name,   FIELD_CAPS.agent_name),
          cap(ev.agent_id,     FIELD_CAPS.agent_id),
          cap(ev.date,         FIELD_CAPS.date),
          clickedUrl ? cap(clickedUrl, FIELD_CAPS.clicked_url) : null,
          userAgent,
          referrer,
          ipHash
        ).run();
        saved++;
      } catch (e) {
        console.error("insert failed:", String(e).slice(0, 200));
        skipped++;
      }
    }

    return json(200, { saved, skipped, received: events.length }, origin);
  },
};
