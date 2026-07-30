/* ==========================================================================
   Janus Desk dashboard.

   Vanilla JS, no build step, no framework. That is a deliberate choice: you
   can open this file in any editor, change a number, and reload. No npm, no
   bundler, nothing to break between you and the page.

   It polls one endpoint (/api/overview) on an interval. No SSE, no
   websockets - post-mortem #11 in v1 was long-lived streams starving the web
   worker and taking the health check down with it. If live updates ever
   matter more than they do now, add them on a separate service.
   ========================================================================== */

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);

let inflight = false;

/* ------------------------------------------------------------ formatting */
const money = (v, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });

const signed = (v, dp = 2) =>
  v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + money(v, dp);

const pnlClass = (v) => (v > 0 ? "gain" : v < 0 ? "loss" : "dim");

function clockET(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York",
  });
}

function shortTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function countdown(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `in ${h}h ${m}m` : `in ${m}m`;
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ================================ ribbon ================================= */
function renderRibbon(session) {
  const ribbon = $("ribbon");
  const playhead = $("playhead");

  [...ribbon.querySelectorAll(".seg")].forEach((n) => n.remove());

  (session.ribbon || []).forEach((seg) => {
    const el = document.createElement("div");
    el.className = `seg seg--${seg.regime}`;
    el.style.left = `${seg.start * 100}%`;
    el.style.width = `${(seg.end - seg.start) * 100}%`;
    if (seg.end - seg.start > 0.10) el.textContent = seg.label;
    el.title = seg.label;
    ribbon.insertBefore(el, playhead);
  });

  playhead.style.left = `${(session.day_fraction || 0) * 100}%`;

  document.documentElement.dataset.regime = session.regime;
  $("shiftBadge").textContent = session.regime;
  $("sessionLabel").textContent = session.label;
  $("handoffAt").textContent = clockET(session.next_handoff_et) + " ET";
  $("handoffIn").textContent = countdown(session.seconds_to_handoff);
}

/* ============================== portfolio ================================ */
function renderPortfolio(p, curve) {
  $("equity").textContent = money(p.equity);
  $("equity").className = "stat-value " + pnlClass(p.equity - p.starting_capital);
  $("equitySub").textContent =
    `${signed(p.return_pct, 2)}% from ${money(p.starting_capital, 0)} · drawdown ${money(p.drawdown_pct, 1)}%`;

  $("openValue").textContent = money(p.open_value);
  $("openSub").textContent =
    `${p.open_count} of ${p.max_open} slots · ${money(p.cash)} cash`;

  $("realizedToday").textContent = signed(p.realized_today);
  $("realizedToday").className = "stat-value num " + pnlClass(p.realized_today);
  $("lossLimitSub").textContent = `daily stop at ${money(p.daily_loss_limit)}`;

  $("unrealized").textContent = signed(p.unrealized_pnl);
  $("unrealized").className = "stat-value num " + pnlClass(p.unrealized_pnl);
  $("winRate").textContent =
    p.win_rate === null
      ? `${p.trades_opened} opened, none closed yet`
      : `${p.win_rate}% of ${p.trades_closed} closed were winners`;

  $("haltBanner").classList.toggle("hidden", !p.halted);
  $("haltReason").textContent = p.halt_reason || "";

  renderSpark(curve || []);
}

function renderSpark(points) {
  const svg = $("spark");
  if (points.length < 2) { svg.innerHTML = ""; return; }

  const vals = points.map((p) => p.equity);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const step = 300 / (points.length - 1);

  const d = vals
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(50 - ((v - min) / span) * 44).toFixed(1)}`)
    .join(" ");

  const up = vals[vals.length - 1] >= vals[0];
  svg.innerHTML =
    `<path d="${d}" fill="none" stroke="var(--${up ? "gain" : "loss"})" ` +
    `stroke-width="1.5" stroke-linejoin="round" opacity="0.9"/>`;
}

/* ============================== positions ================================ */
function renderPositions(positions) {
  const host = $("positions");

  if (!positions.length) {
    host.innerHTML =
      `<div class="empty"><strong>Nothing open</strong>
       Positions appear here the moment the desk fills one.</div>`;
    return;
  }

  host.innerHTML = positions.map((p) => {
    const tag = p.direction === "LONG_CALL" ? "call"
              : p.direction === "LONG_PUT" ? "put"
              : p.direction === "LONG_SHARE" ? "share" : "spot";
    const pct = (p.progress ?? 0) * 100;
    const stale = p.mark_ts && (Date.now() - new Date(p.mark_ts)) > 15 * 60 * 1000;
    const rMult = (p.scalp && p.r_multiple !== null && p.r_multiple !== undefined)
      ? `<span class="tag">R ${signed(p.r_multiple, 2)}</span>` : "";

    return `
      <div class="pos">
        <div class="pos-top">
          <span class="pos-sym">${esc(p.underlying)}</span>
          <span class="tag tag--${tag}">${esc(p.direction.replace("LONG_", ""))}</span>
          ${p.scalp ? `<span class="tag tag--scalp">scalp</span>` : ""}
          ${p.entry_score ? `<span class="tag">score ${p.entry_score.toFixed(0)}</span>` : ""}
          ${rMult}
          ${stale ? `<span class="tag" style="color:var(--warn)">stale price</span>` : ""}
          <span class="pos-pnl ${pnlClass(p.unrealized_pnl)}">
            ${signed(p.unrealized_pnl)}
            <span class="faint" style="font-size:12px">${signed(p.pnl_pct, 1)}%</span>
          </span>
        </div>

        <div class="pos-contract">${esc(p.instrument)} · ${p.quantity}
          @ ${money(p.entry_price, 4)} → ${money(p.mark_price, 4)}</div>

        <div class="exit-bar">
          <div class="exit-track">
            <div class="exit-marker" style="left:${pct.toFixed(1)}%"></div>
          </div>
          <div class="exit-legend">
            <span>stop ${money(p.stop_price, 4)}</span>
            <span>${p.time_stop_ts ? "time stop " + shortTime(p.time_stop_ts) : ""}</span>
            <span>target ${money(p.target_price, 4)}</span>
          </div>
        </div>

        <div class="pos-actions">
          <button class="danger" data-close="${esc(p.position_id)}">Close now</button>
          <span class="faint" style="font-size:11px">opened ${shortTime(p.entry_ts)}</span>
        </div>
      </div>`;
  }).join("");
}

/* ============================== scan board =============================== */
function renderScan(scan) {
  const host = $("scanBoard");
  const results = scan?.results || [];

  $("scanMeta").textContent = scan?.scan_id
    ? `${scan.scan_id} · ${scan.market} · ${scan.duration_ms ?? "—"}ms · ${scan.executed} opened`
    : "";

  if (!results.length) {
    host.innerHTML =
      `<div class="empty"><strong>No scan yet</strong>
       The board fills in after the engine's first pass over the universe.</div>`;
    return;
  }

  host.innerHTML = results.map((r) => {
    const bar = (v) =>
      `<div class="pillar" style="flex:1"><span style="width:${Math.max(0, Math.min(100, v || 0))}%"></span></div>`;

    let why, cls = "";
    if (r.verdict === "EXECUTE") { why = `<b>Opened</b> — ${esc(r.reason)}`; cls = "exec"; }
    else if (r.verdict === "BLOCKED") why = `<b>${esc(r.blocked_by)}</b> — ${esc(r.reason)}`;
    else if (r.verdict === "ERROR") why = `<b>No data</b> — ${esc(r.reason)}`;
    else why = esc(r.reason);

    return `
      <div class="scan-row">
        <div class="scan-sym">${esc(r.symbol)}</div>
        <div>
          <div class="pillars" title="liquidity / technical / sentiment">
            ${bar(r.liquidity)}${bar(r.technical)}${bar(r.sentiment)}
          </div>
          <div class="scan-why ${cls}">${why}</div>
        </div>
        <div class="scan-total">${r.total_score !== null ? r.total_score.toFixed(0) : "—"}</div>
      </div>`;
  }).join("");
}

/* ================================= tape ================================== */
function renderTape(events) {
  const host = $("tape");
  if (!events.length) {
    host.innerHTML = `<div class="empty">Waiting for the first event.</div>`;
    return;
  }
  host.innerHTML = events.map((e) => `
    <div class="tape-line ${e.level}">
      <span class="tape-ts">${shortTime(e.ts)}</span>
      <span class="tape-msg">${esc(e.message)}</span>
    </div>`).join("");
}

/* =============================== system ================================== */
function renderSystem(system) {
  const hb = (system.heartbeats || []).find((h) => h.component === "engine");
  const age = hb ? (Date.now() - new Date(hb.ts)) / 1000 : null;
  const alive = age !== null && age < 120;

  $("dotEngine").className = "dot " + (alive ? "live" : "dead");
  $("engineLabel").textContent = alive
    ? `engine ${Math.round(age)}s ago`
    : "engine not beating";

  const open = (system.breakers || []).filter((b) => b.open);
  $("dotData").className = "dot " + (open.length ? "warn" : "live");
  $("dataLabel").textContent = open.length
    ? `${open.map((b) => b.name).join(", ")} circuit open`
    : "data feeds healthy";
}

/* ============================== sentiment ================================ */
function renderSentiment(s, session) {
  const el = $("newsBias");
  const sub = $("newsBiasSub");
  if (!el || !sub) return;
  // Follow the live shift: CRYPTO desk reads crypto scope, else MACRO.
  const regime = session?.regime || "EQUITY";
  const scopeKey = regime === "CRYPTO" ? "crypto" : "macro";
  const row = (s && s[scopeKey]) || s || {};
  const fresh = !!row.fresh && !row.stale;
  const b = Number(row.bias) || 0;
  el.textContent = (b >= 0 ? "+" : "") + b.toFixed(2);
  el.className = "stat-value num " + (fresh ? pnlClass(b) : "dim");
  const age = row.age_seconds != null ? ` · ${Math.round(row.age_seconds / 60)}m ago` : "";
  if (!row.fresh) {
    sub.textContent = `no ${scopeKey.toUpperCase()} read yet`;
  } else if (row.stale) {
    sub.textContent = `${scopeKey.toUpperCase()} stale${age}`;
  } else {
    sub.textContent = (row.note ? esc(row.note) : scopeKey.toUpperCase()) + age;
  }
}

function renderBudget(budget) {
  const label = $("dataLabel");
  const dot = $("dotData");
  if (!budget || !budget.alpaca) return;
  const a = budget.alpaca;
  const used = a.used ?? 0;
  const limit = a.limit ?? 100;
  if (label) {
    const prev = label.textContent || "";
    if (!prev.includes("budget")) {
      // leave circuit-breaker text if present; append budget when healthy
    }
    if (dot && !dot.classList.contains("warn") && !dot.classList.contains("dead")) {
      label.textContent = `budget ${used}/${limit}/min`;
    }
  }
  // Budget pill if present
  const pill = $("budgetPill");
  if (pill) {
    pill.textContent = `${used}/${limit} req/min`;
    pill.classList.toggle("warn", used > limit * 0.8);
  }
}

/* ============================== commands ================================= */
async function send(path, body) {
  $("cmdResult").textContent = "Queueing…";
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    $("cmdResult").textContent = r.ok
      ? `Queued ${data.kind}. The engine picks it up on its next tick.`
      : `That did not go through: ${data.detail || r.status}`;
  } catch (err) {
    $("cmdResult").textContent = `Could not reach the dashboard API: ${err.message}`;
  }
  refresh();
}

document.addEventListener("click", (ev) => {
  const closeBtn = ev.target.closest("[data-close]");
  if (closeBtn) {
    send("/api/commands/close", { position_id: closeBtn.dataset.close });
    return;
  }
  const action = ev.target.closest("[data-action]")?.dataset.action;
  if (!action) return;

  if (action === "scan") send("/api/commands/scan");
  if (action === "resume") send("/api/commands/resume");
  if (action === "halt") send("/api/commands/halt", { reason: "halted from the dashboard" });
  if (action === "flatten") {
    if (confirm("Close every open position at the current mark?")) {
      send("/api/commands/flatten");
    }
  }
});

/* =============================== polling ================================= */
async function refresh() {
  if (inflight) return;
  inflight = true;
  try {
    const r = await fetch("/api/overview", { cache: "no-store" });
    if (!r.ok) throw new Error(`API returned ${r.status}`);
    const d = await r.json();

    renderRibbon(d.session);
    renderPortfolio(d.portfolio, d.equity_curve);
    renderPositions(d.positions);
    renderScan(d.scan);
    renderTape(d.events);
    renderSystem(d.system);
    renderSentiment(d.sentiment, d.session);
    renderBudget(d.request_budget || d.system?.request_budget);

    $("updated").textContent = `updated ${shortTime(new Date().toISOString())}`;
    $("updatedPill").querySelector(".dot").className = "dot live";
    $("footerClock").textContent = clockET(d.session.now_et) + " ET";
  } catch (err) {
    $("updated").textContent = "dashboard offline";
    $("updatedPill").querySelector(".dot").className = "dot dead";
    console.error(err);
  } finally {
    inflight = false;
  }
}

refresh();
setInterval(refresh, POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
