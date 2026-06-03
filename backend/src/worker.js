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
  const allowed = ALLOWED_ORIGINS.has(origin) ? origin : "https://www.studyeventz.com";
  return {
    "Access-Control-Allow-Origin": allowed,
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

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const url = new URL(request.url);

    // 1a. Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // Routing
    if (url.pathname !== "/track" || request.method !== "POST") {
      return json(404, { error: "not_found" }, origin);
    }

    // 1b. CORS allow-list
    if (!ALLOWED_ORIGINS.has(origin)) {
      return json(403, { error: "origin_not_allowed" }, origin);
    }

    // 2. Site key (read from ?k= so sendBeacon and fetch both work)
    if (url.searchParams.get("k") !== env.SITE_KEY) {
      return json(403, { error: "bad_site_key" }, origin);
    }

    // 3. Rate limit per IP
    const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
    if (env.RATE_LIMITER && typeof env.RATE_LIMITER.limit === "function") {
      try {
        const { success } = await env.RATE_LIMITER.limit({ key: ip });
        if (!success) return json(429, { error: "rate_limited" }, origin);
      } catch (_) { /* limiter unavailable — fail open, not closed */ }
    }

    // 4. Body size cap (relies on Content-Length when present)
    const contentLength = parseInt(request.headers.get("content-length") || "0", 10);
    if (contentLength > MAX_BODY_BYTES) {
      return json(413, { error: "payload_too_large" }, origin);
    }

    // Parse JSON
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

    // 5. Batch size cap
    const events = Array.isArray(body) ? body : [body];
    if (events.length === 0 || events.length > MAX_BATCH) {
      return json(400, { error: "bad_batch_size" }, origin);
    }

    // Common per-request metadata
    const ipHash = await hashIp(ip, env.IP_HASH_SALT);
    const userAgent = cap(request.headers.get("User-Agent"), 500);
    const referrer = cap(request.headers.get("Referer"), 500);

    let saved = 0;
    let skipped = 0;

    for (const ev of events) {
      // 6. Per-event validation
      if (!validEvent(ev)) { skipped++; continue; }

      const clickedUrl = ev.registration_url || ev.maps_url || ev.calendar_url || null;

      try {
        await env.DB.prepare(`
          INSERT INTO events (
            type, ts, session_id, page, event_id, event_name,
            agent_name, agent_id, event_date, clicked_url,
            user_agent, referrer, ip_hash
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `).bind(
          cap(ev.type,         FIELD_CAPS.type),
          cap(ev.ts,           FIELD_CAPS.ts),
          cap(ev.session_id,   FIELD_CAPS.session_id),
          cap(ev.page,         FIELD_CAPS.page),
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
        // Don't let one bad row fail the request — log and move on
        console.error("insert failed:", String(e).slice(0, 200));
        skipped++;
      }
    }

    return json(200, { saved, skipped, received: events.length }, origin);
  },
};
