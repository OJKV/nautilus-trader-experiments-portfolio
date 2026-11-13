// NTX Monitor -- vanilla JS, no build step, no network beyond this origin.
// R22: when the API says a snapshot is stale, the page says STALE loudly and
// greys out every snapshot-derived number. It never shows them as live.
"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);
let currentBot = null;
try { currentBot = localStorage.getItem("ntx.bot"); } catch (e) { /* private mode */ }

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function num(v, d = 2) {
  return typeof v === "number" && isFinite(v)
    ? v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })
    : "–";
}
function usd(v) { return typeof v === "number" ? (v < 0 ? "-$" : "$") + num(Math.abs(v)) : "–"; }
function signed(v) {
  if (typeof v !== "number") return "–";
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
  return `<span class="${cls}">${v > 0 ? "+" : ""}${num(v)}</span>`;
}
// "$ (+x.xx%)" -- the dollar figure with the percentage of the sizing basis.
// The percentage is what makes two bots comparable: a a sample amount live account and a
// $1,000-sized demo one produce very different dollar numbers for identical
// performance. Shown only when the API supplied one; a dash beats a wrong number
// when someone is being shown the dashboard.
function signedWithPct(v, pct) {
  const base = signed(v);
  if (typeof pct !== "number" || !isFinite(pct)) return base;
  const cls = pct > 0 ? "pos" : pct < 0 ? "neg" : "";
  const sign = pct > 0 ? "+" : "";
  return `${base} <span class="${cls} kpi-sub">(${sign}${pct.toFixed(2)}%)</span>`;
}
function px(v) {
  if (typeof v !== "number") return "–";
  const a = Math.abs(v);
  return num(v, a >= 1000 ? 2 : a >= 1 ? 4 : 6);
}
function ago(s) {
  if (typeof s !== "number") return "never";
  const a = Math.abs(s);
  if (a < 90) return `${Math.round(s)}s`;
  if (a < 5400) return `${Math.round(s / 60)}m`;
  if (a < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}
function short(ts) { return ts ? String(ts).replace("T", " ").slice(0, 19) : ""; }
function rows(tbody, html, cols, emptyText) {
  tbody.innerHTML = html || `<tr><td class="empty" colspan="${cols}">${esc(emptyText)}</td></tr>`;
}

async function api(path, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== ""));
  const r = await fetch(`/api/${path}${q.toString() ? "?" + q : ""}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------ renderers

function renderBots(list) {
  const sel = $("bot");
  const ids = list.map((b) => b.id);
  if (!ids.includes(currentBot)) {
    const running = list.find((b) => b.has_snapshot && !b.stale);
    currentBot = (running || list.find((b) => b.has_snapshot) || list[0] || {}).id || null;
  }
  const html = list.map((b) =>
    `<option value="${esc(b.id)}"${b.id === currentBot ? " selected" : ""}>` +
    `${esc(b.id)} · ${esc(b.badge)}${b.has_snapshot ? " · " + ago(b.age_s) : ""}</option>`).join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
}

function renderStatus(status) {
  const stale = status.stale === true;
  const badge = $("badge");
  badge.textContent = status.badge;
  badge.className = "badge badge-" + String(status.badge).replace(/\s+/g, "-");
  document.body.classList.toggle("is-stale", stale);
  $("stale-banner").hidden = !stale;
  if (stale) {
    $("stale-banner").textContent = status.snapshot_ts
      ? `STALE: last snapshot ${ago(status.age_s)} ago (${short(status.snapshot_ts)} UTC). ` +
        "The node may be down or frozen; the numbers below are last-known, not live."
      : "STALE: no readable state snapshot for this bot. Nothing below is live.";
  }
  $("age").textContent = status.snapshot_ts ? `snapshot ${ago(status.age_s)} ago` : "no snapshot";
  document.title = `${status.badge} · ${status.bot} · NTX Monitor`;

  // Two numbers that both look like "equity": what the account holds, and what
  // trades are sized from. On the demo soak they differ on purpose, so each has
  // its own tile and the API's gloss is printed as a sentence under them. The
  // account balance is filled by renderBalance -- it can come from the venue.
  const sizing = (status.equity || {}).sizing || {};
  $("k-sizing").textContent = usd(sizing.usd);
  $("k-sizing-note").textContent = sizing.is_override
    ? "a setting (NTX_EQUITY_OVERRIDE_USD), not money"
    : "the account's own balance";
  $("equity-gloss").textContent = (status.equity || {}).note || "";
  $("k-unreal").innerHTML = signedWithPct(status.unrealized_usd, status.unrealized_pct);
  const un = status.positions_unprotected;
  $("k-unprot").innerHTML = typeof un === "number"
    ? `<span class="${un > 0 ? "neg" : "pos"}">${un}</span>` : "–";
  $("k-trading").textContent = status.trading_enabled == null ? "–"
    : `${status.trading_enabled ? "enabled" : "DISABLED"} · ${status.venue || ""}`;

  const insts = status.instruments || [];
  rows($("instruments").tBodies[0], insts.map((i) =>
    `<tr class="${i.warm ? "" : "row-warn"}"><td>${esc(i.id)}</td>` +
    `<td class="n">${px(i.last_close)}</td><td class="n">${ago(i.live_bar_age_s)}</td>` +
    `<td class="n">${esc(i.bars_seen)}</td><td>${i.warm ? "yes" : "warming"}</td></tr>`).join(""),
    5, "no instruments reported");
}

function renderBalance(b) {
  const val = $("k-account");
  const have = typeof b.usd === "number";
  val.textContent = have ? usd(b.usd) : "unavailable";
  val.classList.toggle("unavail", !have);
  const who = b.account_label ? `${b.account_label} · ` : "";
  // R22: say where the figure came from and when it was read, never imply "now".
  const where = b.source === "venue"
    ? `read from the ${b.env} account at the venue ${short(b.as_of)} UTC`
    : `from the node snapshot ${short(b.as_of)} UTC`;
  $("k-account-note").textContent = b.error ? b.error : `${who}${b.ccy || "USDT"} · ${where}`;
}

function renderPositions(p) {
  const html = (p.positions || []).map((x) => {
    const bad = x.protected !== true;
    return `<tr class="${bad ? "row-fail" : ""}"><td>${esc(x.instrument_id)}</td>` +
      `<td>${esc(x.side)}</td><td class="n">${num(x.qty, 4)}</td>` +
      `<td class="n">${px(x.entry_price)}</td><td class="n">${px(x.mark_price)}</td>` +
      `<td class="n">${px(x.stop_price)}</td><td class="n">${px(x.trailing_stop)}</td>` +
      `<td class="n">${esc(x.bars_held ?? "–")}</td><td class="n">${signed(x.unrealized_usd)}</td>` +
      `<td>${bad ? "UNPROTECTED" : "stop live"}</td></tr>`;
  }).join("");
  rows($("positions").tBodies[0], html, 10, p.stale ? "no fresh snapshot" : "flat");
  const jn = (p.journal_open || []).length;
  $("pos-note").textContent = `Source: node snapshot${p.stale ? " (STALE)" : ""}. ` +
    `Journal believes ${jn} open. The venue is the truth (R27).`;
}

function renderStats(s) {
  const label = { today: "Today", "7d": "Last 7 days", "30d": "Last 30 days", "365d": "Last 365 days", all: "All time" };
  rows($("periods").tBodies[0], (s.periods || []).map((r) =>
    `<tr><td>${esc(label[r.period] || r.period)}</td><td class="n">${r.closed}</td>` +
    `<td class="n">${r.win_rate == null ? "–" : (r.win_rate * 100).toFixed(0) + "%"}</td>` +
    `<td class="n">${r.closed ? signedWithPct(r.net_pnl_usd, r.net_pnl_pct) : "–"}</td>` +
    `<td class="n">${r.closed ? num(r.fees_usd) : "–"}</td></tr>`).join(""), 5, "no closed trades");
  $("k-net").innerHTML = s.closed ? signedWithPct(s.net_pnl_usd, s.net_pnl_pct) : "–";
  const wr = s.win_rate == null ? "–" : (s.win_rate * 100).toFixed(0) + "%";
  const pf = s.profit_factor == null ? (s.wins ? "∞" : "–") : num(s.profit_factor);
  $("k-trades").textContent = `${s.closed} · ${wr} · ${pf}`;
  const reasons = Object.entries(s.exit_reasons || {}).map(([k, v]) => `${k} ${v}`).join(", ");
  const inst = Object.entries(s.pnl_by_instrument || {})
    .map(([k, v]) => `${k.split("-")[0]} ${v.net_usd >= 0 ? "+" : ""}${num(v.net_usd)} (${v.count})`)
    .join(", ");
  $("stats-extra").textContent = s.closed
    ? `Fees $${num(s.fees_usd)} · funding $${num(s.funding_usd)} · open ${s.open} · ` +
      `protect gap avg ${num(s.avg_protect_gap_ms, 0)} ms, max ${s.max_protect_gap_ms ?? "–"} ms · ` +
      `exits: ${reasons} · by instrument: ${inst}`
    : `No closed trades yet · open (journal) ${s.open}`;
}

function renderTrades(t) {
  rows($("trades").tBodies[0], (t.trades || []).map((x) =>
    `<tr><td>${esc(short(x.closed_at))}</td><td>${esc(x.instrument_id)}</td>` +
    `<td>${esc(x.side)}</td><td class="n">${px(x.entry_price)}</td>` +
    `<td class="n">${px(x.exit_price)}</td><td class="n">${signed(x.pnl_net_usd)}</td>` +
    `<td>${esc(x.exit_reason)}</td><td class="n">${esc(x.bars_held ?? "–")}</td>` +
    `<td class="n">${esc(x.protect_gap_ms ?? "–")}</td></tr>`).join(""), 9, "no closed trades");
}

function renderHealth(h) {
  const pill = $("h-overall");
  pill.textContent = h.overall;
  pill.className = "pill st-" + h.overall;
  $("health").innerHTML = h.checks.map((c) =>
    `<li><span class="dot st-${esc(c.status)}" title="${esc(c.status)}"></span>` +
    `<span class="hname">${esc(c.name)}</span><span class="hdetail">${esc(c.detail)}</span></li>`
  ).join("");
}

function renderEvents(e) {
  const html = (e.events || []).map((x) => {
    const sev = String(x.severity || "");
    const cls = sev === "critical" || sev === "error" || x.kind === "data_stall" ? "row-fail"
      : sev === "warning" ? "row-warn" : "";
    const detail = typeof x.payload === "object" ? JSON.stringify(x.payload) : x.payload;
    return `<tr class="${cls}"><td>${esc(short(x.ts))}</td><td>${esc(x.kind)}</td>` +
      `<td>${esc(sev)}</td><td class="mono">${esc(String(detail).slice(0, 160))}</td></tr>`;
  }).join("");
  rows($("events").tBodies[0], html, 4, "no events");
}

function renderEquity(e) {
  const pts = (e.points || []).filter((p) => typeof p.equity_usd === "number");
  const box = $("equity");
  $("eq-meta").textContent = pts.length
    ? `${pts.length} pts · HWM ${usd(e.hwm)} · max DD ${num(e.max_drawdown_pct)}%` : "";
  if (pts.length < 2) { box.innerHTML = `<p class="empty">not enough equity points yet</p>`; return; }
  const W = 600, H = 180, L = 52, R = 8, T = 8, B = 20;
  const t = pts.map((p) => Date.parse(p.ts));
  const y = pts.map((p) => p.equity_usd);
  const t0 = Math.min(...t), t1 = Math.max(...t);
  let y0 = Math.min(...y), y1 = Math.max(...y);
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const sx = (v) => L + ((v - t0) / (t1 - t0 || 1)) * (W - L - R);
  const sy = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(t[i]).toFixed(1)},${sy(y[i]).toFixed(1)}`).join("");
  box.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="equity curve">` +
    `<line class="axis" x1="${L}" y1="${T}" x2="${L}" y2="${H - B}"/>` +
    `<line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"/>` +
    `<text class="lbl" x="${L - 4}" y="${T + 8}" text-anchor="end">${esc(num(y1))}</text>` +
    `<text class="lbl" x="${L - 4}" y="${H - B}" text-anchor="end">${esc(num(y0))}</text>` +
    `<text class="lbl" x="${L}" y="${H - 4}">${esc(short(pts[0].ts).slice(0, 16))}</text>` +
    `<text class="lbl" x="${W - R}" y="${H - 4}" text-anchor="end">` +
    `${esc(short(pts[pts.length - 1].ts).slice(0, 16))}</text>` +
    `<path class="ln" d="${d}" vector-effect="non-scaling-stroke"/></svg>`;
}

function renderLogs(l) {
  const pre = $("logs");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.innerHTML = (l.lines || []).map((x) =>
    `<span class="lv-${esc(x.level)}">${esc(short(x.timestamp))} ${esc(x.level).padEnd(8)} ` +
    `${esc(x.component || "")} ${esc(x.message)}</span>`).join("\n") ||
    `<span class="empty">no log lines</span>`;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderBacktests(b) {
  rows($("backtests").tBodies[0], (b.runs || []).slice(0, 30).map((r) => {
    const m = r.metrics || {};
    return `<tr><td>${esc(short(r.created_utc).slice(0, 16))}</td><td>${esc(r.label)}</td>` +
      `<td>${esc(r.timeframe ?? "")}</td><td>${esc(r.start ?? "")} → ${esc(r.end ?? "")}</td>` +
      `<td class="n">${esc(m.trades ?? "–")}</td><td class="n">${num(m.sharpe)}</td>` +
      `<td class="n">${num(m.profit_factor)}</td><td class="n">${num(m.max_drawdown_pct, 1)}</td>` +
      `<td class="mono">${esc(r.source_hash ?? "")}</td></tr>`;
  }).join(""), 9, "no recorded runs");
}

// ------------------------------------------------------------------ loop

let backtestsLoadedAt = 0;

async function refresh() {
  const errs = [];
  const safe = (p, fn) => p.then(fn).catch((e) => errs.push(e.message));
  try {
    renderBots((await api("bots")).bots);
  } catch (e) {
    errs.push(e.message);
  }
  if (currentBot) {
    const bot = currentBot;
    await Promise.all([
      safe(api("status", { bot }), renderStatus),
      safe(api("balance", { bot }), renderBalance),
      safe(api("positions", { bot }), renderPositions),
      safe(api("stats", { bot }), renderStats),
      safe(api("trades", { bot, limit: 25 }), renderTrades),
      safe(api("health", { bot }), renderHealth),
      safe(api("events", { bot, limit: 25 }), renderEvents),
      safe(api("equity", { bot, max_points: 600 }), renderEquity),
    ]);
  } else {
    renderStatus({ badge: "NO SNAPSHOT", stale: true, bot: "none" });
  }
  await safe(api("logs", { level: $("loglevel").value, n: 300 }), renderLogs);
  if (Date.now() - backtestsLoadedAt > 60000) {
    await safe(api("backtests", { limit: 30 }), renderBacktests);
    backtestsLoadedAt = Date.now();
  }
  const eb = $("error-banner");
  eb.hidden = errs.length === 0;
  eb.textContent = errs.length ? `Monitor could not load: ${errs.join("; ")}` : "";
  if (errs.length) document.body.classList.add("is-stale");
  $("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

$("bot").addEventListener("change", (ev) => {
  currentBot = ev.target.value;
  try { localStorage.setItem("ntx.bot", currentBot); } catch (e) { /* ignore */ }
  refresh();
});
$("loglevel").addEventListener("change", refresh);

refresh();
setInterval(refresh, POLL_MS);
