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
  "page_view",
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


// ─── GET /dash (private analytics dashboard) ───────────────────────────────
// A self-contained HTML view of the ingested analytics, gated by env.DASH_KEY
// (a Cloudflare SECRET — distinct from the public SITE_KEY). This is a browser
// navigation, not a CORS fetch, so it bypasses the Origin allow-list and is
// protected purely by the secret in ?k=. Fails closed if DASH_KEY is unset.

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Constant-time-ish string compare to avoid leaking the key via timing.
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function bar(value, max, label, sub) {
  const pct = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return `<div class="row">
    <div class="row-label">${escapeHtml(label)}${sub ? `<span class="row-sub">${escapeHtml(sub)}</span>` : ""}</div>
    <div class="row-track"><div class="row-fill" style="width:${pct}%"></div></div>
    <div class="row-val">${value}</div>
  </div>`;
}

async function handleDash(request, env, url) {
  if (!env.DASH_KEY) {
    return new Response("Dashboard disabled: DASH_KEY secret is not set.", {
      status: 503, headers: { "Content-Type": "text/plain" },
    });
  }
  if (!safeEqual(url.searchParams.get("k") || "", env.DASH_KEY)) {
    return new Response("Forbidden", { status: 403, headers: { "Content-Type": "text/plain" } });
  }

  let days = parseInt(url.searchParams.get("days") || "30", 10);
  if (!Number.isFinite(days) || days < 1) days = 30;
  if (days > 365) days = 365;
  const window = `-${days} days`;

  const q = async (sql, ...binds) => {
    const r = await env.DB.prepare(sql).bind(...binds).all();
    return r.results || [];
  };

  let summary, byType, byDay, topEvents, topAgents, byCountry, subs, geo, devices, referrers;
  try {
    [summary] = await q(
      `SELECT
         SUM(type='page_view') AS pageviews,
         SUM(type='event_impression') AS impressions,
         SUM(type='event_register_click') AS reg_clicks,
         SUM(type LIKE '%click' AND type<>'event_register_click') AS other_clicks,
         COUNT(*) AS total,
         COUNT(DISTINCT session_id) AS sessions,
         COUNT(DISTINCT ip_hash) AS visitors
       FROM events WHERE received_at >= datetime('now', ?)`, window);
    // Visitors by country (server-side geo-IP; visits = distinct sessions).
    geo = await q(
      `SELECT COALESCE(NULLIF(geo_country,''),'??') cc,
         COUNT(DISTINCT session_id) visits, COUNT(DISTINCT ip_hash) visitors
       FROM events WHERE received_at >= datetime('now', ?)
       GROUP BY cc ORDER BY visits DESC, visitors DESC LIMIT 40`, window);
    // Device split (coarse, from user-agent).
    devices = await q(
      `SELECT CASE WHEN user_agent LIKE '%Mobi%' OR user_agent LIKE '%Android%'
                    OR user_agent LIKE '%iPhone%' OR user_agent LIKE '%iPad%'
                   THEN 'Mobile' ELSE 'Desktop' END dev,
         COUNT(DISTINCT session_id) visits
       FROM events WHERE received_at >= datetime('now', ?)
       GROUP BY dev ORDER BY visits DESC`, window);
    // Referrers (full URL; host extraction + internal-traffic filtering in JS).
    referrers = await q(
      `SELECT COALESCE(NULLIF(referrer,''),'(direct)') ref,
         COUNT(DISTINCT session_id) visits
       FROM events WHERE received_at >= datetime('now', ?)
       GROUP BY ref ORDER BY visits DESC LIMIT 60`, window);
    byType = await q(
      `SELECT type, COUNT(*) n FROM events
       WHERE received_at >= datetime('now', ?) GROUP BY type ORDER BY n DESC`, window);
    byDay = await q(
      `SELECT substr(received_at,1,10) day,
         SUM(type='event_impression') imps,
         SUM(type LIKE '%click') clicks
       FROM events WHERE received_at >= datetime('now', ?)
       GROUP BY day ORDER BY day`, window);
    // Group top events by NAME (not raw event_id) so duplicate ids for the
    // same logical event don't fragment the counts.
    topEvents = await q(
      `SELECT event_name, MAX(agent_name) agent_name,
         SUM(type='event_impression') imps,
         SUM(type LIKE '%click') clicks
       FROM events WHERE received_at >= datetime('now', ?)
         AND event_name IS NOT NULL AND event_name<>''
       GROUP BY event_name ORDER BY imps DESC, clicks DESC LIMIT 20`, window);
    topAgents = await q(
      `SELECT agent_name,
         SUM(type='event_impression') imps,
         SUM(type LIKE '%click') clicks
       FROM events WHERE received_at >= datetime('now', ?)
         AND agent_name IS NOT NULL AND agent_name<>''
       GROUP BY agent_name ORDER BY imps DESC LIMIT 20`, window);
    byCountry = await q(
      `SELECT COALESCE(NULLIF(country,''),'(unknown)') country, COUNT(*) n
       FROM events WHERE received_at >= datetime('now', ?)
       GROUP BY country ORDER BY n DESC`, window);
    [subs] = await q(
      `SELECT COUNT(*) total, SUM(status='pending') pending FROM submissions`);
  } catch (e) {
    return new Response("Query failed: " + escapeHtml(String(e).slice(0, 300)), {
      status: 500, headers: { "Content-Type": "text/plain" },
    });
  }

  const s = summary || {};
  const imps = s.impressions || 0;
  const regClicks = s.reg_clicks || 0;
  const ctr = imps > 0 ? ((regClicks / imps) * 100).toFixed(1) : "0.0";

  const k = encodeURIComponent(url.searchParams.get("k") || "");
  const dayChip = (d, lbl) =>
    `<a class="chip ${days === d ? "active" : ""}" href="?k=${k}&days=${d}">${lbl}</a>`;

  const maxDay = Math.max(1, ...byDay.map(d => (d.imps || 0)));
  const dayBars = byDay.length
    ? byDay.map(d => {
        const h = Math.max(3, Math.round(((d.imps || 0) / maxDay) * 100));
        return `<div class="spark-col" title="${escapeHtml(d.day)}: ${d.imps || 0} impressions, ${d.clicks || 0} clicks">
          <div class="spark-bar" style="height:${h}%"></div>
          <div class="spark-x">${escapeHtml(d.day.slice(5))}</div>
        </div>`;
      }).join("")
    : `<div class="muted">No activity in this window.</div>`;

  const maxEvent = Math.max(1, ...topEvents.map(e => e.imps || 0));
  const eventRows = topEvents.length
    ? topEvents.map(e => bar(e.imps || 0, maxEvent, e.event_name, e.agent_name)).join("")
    : `<div class="muted">No events tracked yet.</div>`;

  const maxAgent = Math.max(1, ...topAgents.map(a => a.imps || 0));
  const agentRows = topAgents.length
    ? topAgents.map(a => bar(a.imps || 0, maxAgent, a.agent_name,
        (a.clicks || 0) + " click" + ((a.clicks || 0) === 1 ? "" : "s"))).join("")
    : `<div class="muted">No agents tracked yet.</div>`;

  const typeRows = byType.map(t => `<tr><td>${escapeHtml(t.type)}</td><td class="num">${t.n}</td></tr>`).join("");
  const countryRows = byCountry.map(c => `<tr><td>${escapeHtml(c.country)}</td><td class="num">${c.n}</td></tr>`).join("");

  // ISO-3166 alpha-2 → flag emoji (regional indicators).
  const ccFlag = (cc) => /^[A-Z]{2}$/.test(cc)
    ? String.fromCodePoint(...[...cc].map(c => 0x1F1E6 + c.charCodeAt(0) - 65)) : "";
  const maxGeo = Math.max(1, ...geo.map(g => g.visits || 0));
  const geoRows = geo.length
    ? geo.map(g => bar(g.visits || 0, maxGeo, `${ccFlag(g.cc)} ${g.cc}`.trim(),
        (g.visitors || 0) + " visitor" + ((g.visitors || 0) === 1 ? "" : "s"))).join("")
    : `<div class="muted">No visitor geography yet.</div>`;

  const maxDev = Math.max(1, ...devices.map(d => d.visits || 0));
  const deviceRows = devices.length
    ? devices.map(d => bar(d.visits || 0, maxDev, d.dev, "")).join("")
    : `<div class="muted">—</div>`;

  // Referrers: reduce full URLs to host, drop our own internal navigation.
  const refHost = (u) => {
    if (u === "(direct)") return "(direct)";
    const h = u.replace(/^https?:\/\//i, "").split("/")[0].replace(/^www\./i, "");
    return h || "(direct)";
  };
  const refMap = new Map();
  for (const r of referrers) {
    const h = refHost(r.ref);
    if (h.includes("studyeventz")) continue;      // internal navigation
    refMap.set(h, (refMap.get(h) || 0) + (r.visits || 0));
  }
  const refList = [...refMap.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15);
  const maxRef = Math.max(1, ...refList.map(r => r[1]));
  const refRows = refList.length
    ? refList.map(([h, v]) => bar(v, maxRef, h, "")).join("")
    : `<div class="muted">No external referrers yet.</div>`;

  const html = `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>StudyEventz Analytics</title>
<style>
  :root { --bg:#0f1419; --card:#1a212b; --line:#2a3340; --text:#e6edf3;
          --muted:#8b98a8; --teal:#2dd4bf; --accent:#38bdf8; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         padding:1.2rem; max-width:1000px; margin:0 auto; }
  h1 { font-size:1.3rem; margin:0 0 .2rem; }
  .sub { color:var(--muted); font-size:.85rem; margin-bottom:1rem; }
  .chips { display:flex; gap:.5rem; margin-bottom:1.2rem; flex-wrap:wrap; }
  .chip { padding:.35rem .8rem; border-radius:20px; background:var(--card);
          color:var(--muted); text-decoration:none; border:1px solid var(--line);
          font-size:.85rem; }
  .chip.active { background:var(--teal); color:#06251f; border-color:var(--teal); font-weight:600; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
           gap:.8rem; margin-bottom:1.4rem; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:.9rem 1rem; }
  .stat .n { font-size:1.7rem; font-weight:700; }
  .stat .l { color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.05em; }
  .stat .n.accent { color:var(--teal); }
  section { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:1rem 1.1rem; margin-bottom:1.2rem; }
  section h2 { font-size:.95rem; margin:0 0 .9rem; }
  .row { display:flex; align-items:center; gap:.7rem; margin:.45rem 0; }
  .row-label { flex:0 0 42%; font-size:.85rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-sub { color:var(--muted); font-size:.75rem; margin-left:.4rem; }
  .row-track { flex:1; background:#0d1117; border-radius:6px; height:14px; overflow:hidden; }
  .row-fill { height:100%; background:linear-gradient(90deg,var(--accent),var(--teal)); border-radius:6px; }
  .row-val { flex:0 0 38px; text-align:right; font-variant-numeric:tabular-nums; font-size:.85rem; }
  .spark { display:flex; align-items:flex-end; gap:3px; height:120px; overflow-x:auto; padding-bottom:1.2rem; }
  .spark-col { flex:1; min-width:14px; display:flex; flex-direction:column; align-items:center;
               justify-content:flex-end; height:100%; position:relative; }
  .spark-bar { width:70%; background:linear-gradient(180deg,var(--teal),var(--accent)); border-radius:3px 3px 0 0; }
  .spark-x { position:absolute; bottom:-1.1rem; font-size:.6rem; color:var(--muted);
             transform:rotate(-45deg); transform-origin:center; white-space:nowrap; }
  table { width:100%; border-collapse:collapse; font-size:.85rem; }
  td { padding:.3rem .2rem; border-bottom:1px solid var(--line); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; color:var(--teal); }
  .muted { color:var(--muted); font-size:.85rem; padding:.5rem 0; }
  .two { display:grid; grid-template-columns:1fr 1fr; gap:1.2rem; }
  @media(max-width:640px){ .two{ grid-template-columns:1fr; } .row-label{ flex-basis:50%; } }
  footer { color:var(--muted); font-size:.75rem; text-align:center; margin-top:1rem; }
</style></head><body>
  <h1>StudyEventz Analytics</h1>
  <div class="sub">Last ${days} day${days === 1 ? "" : "s"} · live from D1</div>
  <div class="chips">
    ${dayChip(7, "7d")}${dayChip(30, "30d")}${dayChip(90, "90d")}${dayChip(365, "1y")}
  </div>
  <div class="cards">
    <div class="stat"><div class="n accent">${s.pageviews || 0}</div><div class="l">Pageviews</div></div>
    <div class="stat"><div class="n">${s.sessions || 0}</div><div class="l">Visits</div></div>
    <div class="stat"><div class="n">${s.visitors || 0}</div><div class="l">Visitors</div></div>
    <div class="stat"><div class="n">${imps}</div><div class="l">Impressions</div></div>
    <div class="stat"><div class="n">${regClicks}</div><div class="l">Register clicks</div></div>
    <div class="stat"><div class="n">${ctr}%</div><div class="l">Click rate</div></div>
  </div>

  <section>
    <h2>Visitors by country</h2>
    ${geoRows}
  </section>

  <div class="two">
    <section>
      <h2>Where they come from</h2>
      ${refRows}
    </section>
    <section>
      <h2>Device</h2>
      ${deviceRows}
    </section>
  </div>

  <section>
    <h2>Activity by day (impressions)</h2>
    <div class="spark">${dayBars}</div>
  </section>

  <section>
    <h2>Top events — by impressions</h2>
    ${eventRows}
  </section>

  <section>
    <h2>Top agents — by impressions</h2>
    ${agentRows}
  </section>

  <div class="two">
    <section>
      <h2>Interactions by type</h2>
      <table>${typeRows || '<tr><td class="muted">none</td></tr>'}</table>
    </section>
    <section>
      <h2>By market &amp; submissions</h2>
      <table>${countryRows || '<tr><td class="muted">none</td></tr>'}</table>
      <table style="margin-top:.8rem">
        <tr><td>Submissions (all time)</td><td class="num">${(subs && subs.total) || 0}</td></tr>
        <tr><td>Pending review</td><td class="num">${(subs && subs.pending) || 0}</td></tr>
      </table>
    </section>
  </div>

  <footer>StudyEventz · private dashboard · noindex</footer>
</body></html>`;

  return new Response(html, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow",
    },
  });
}


export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const url = new URL(request.url);

    // 1a. Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // Private analytics dashboard (GET, secret-gated, no CORS — browser nav).
    if (url.pathname === "/dash" && request.method === "GET") {
      return handleDash(request, env, url);
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
    // Visitor country, derived server-side from Cloudflare's edge geo-IP
    // (ISO-3166 alpha-2, e.g. "TH", "GB"). Never the raw IP — see hashIp().
    const geoCountry = (request.cf && request.cf.country) || null;

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
            user_agent, referrer, ip_hash, geo_country
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
          ipHash,
          geoCountry
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
