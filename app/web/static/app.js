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
const TOKEN_KEY = "janus.dashboardToken";
const $ = (id) => document.getElementById(id);

let inflight = false;
let scoreFloor = 75;
let commandsNeedToken = false;
let slotLine = "";

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

  if (session.execute_threshold) scoreFloor = session.execute_threshold;
  const instrument = session.equity_instrument === "options" ? "options" : "shares";
  const bits = [
    "Paper",
    instrument,
    `threshold ${Math.round(scoreFloor)}`,
  ];
  if (session.dry_run) bits.push("dry run");
  if (session.trading_enabled === false) bits.push("trading off");
  if (!session.crypto_enabled) bits.push("crypto off");
  const mode = $("modeLine");
  if (mode) mode.textContent = bits.join(" · ");

  const note = $("modeBanner");
  if (note) {
    const messages = [];
    if (session.dry_run) {
      messages.push("Dry run is on. The scan still ranks names, and nothing is filled.");
    }
    if (session.trading_enabled === false) {
      messages.push("Trading is switched off in configuration.");
    }
    if (session.regime === "IDLE") {
      messages.push("The equity session is closed. Open positions keep being managed.");
    }
    note.textContent = messages.join(" ");
    note.classList.toggle("hidden", messages.length === 0);
  }

  commandsNeedToken = !!session.commands_locked;
  const auth = $("authRow");
  if (auth) auth.classList.toggle("hidden", !commandsNeedToken);
  paintCommandLock();
}

function commandsReady() {
  return !commandsNeedToken || !!sessionStorage.getItem(TOKEN_KEY);
}

function paintCommandLock() {
  const ready = commandsReady();
  document.querySelectorAll("[data-action], [data-close]").forEach((btn) => {
    btn.disabled = !ready;
  });
}

function dollarsToStops(positions) {
  return (positions || []).reduce((sum, p) => {
    const mark = Number(p.mark_price ?? p.entry_price);
    const stop = Number(p.stop_price);
    const qty = Number(p.quantity) || 0;
    const mult = Number(p.multiplier) || 1;
    if (!Number.isFinite(mark) || !Number.isFinite(stop) || mark <= 0) return sum;
    return sum + Math.max(0, mark - stop) * qty * mult;
  }, 0);
}

/* ============================== portfolio ================================ */
function renderPortfolio(p, curve) {
  $("equity").textContent = money(p.equity);
  $("equity").className = "stat-value " + pnlClass(p.equity - p.starting_capital);
  const dollarPnl = p.total_pnl ?? (p.equity - p.starting_capital);
  $("equitySub").textContent =
    `${signed(dollarPnl)} · ${signed(p.return_pct, 2)}% from ${money(p.starting_capital, 0)} · off peak ${money(Math.abs(p.drawdown_pct), 1)}%`;

  slotLine = `${p.open_count} of ${p.max_open} slots`;
  if ($("openSub") && !$("openValue").dataset.live) {
    $("openSub").textContent = `${slotLine} · waiting for marks`;
  }

  if ($("cashAvail")) {
    $("cashAvail").textContent = money(p.cash);
  }
  if ($("deskExposure")) {
    const cashPct = p.equity > 0 ? (p.cash / p.equity) * 100 : 0;
    $("deskExposure").textContent = `${money(cashPct, 0)}% of the account is still cash`;
  }

  const dayPnl = p.day_pnl ?? p.realized_today;
  $("realizedToday").textContent = signed(dayPnl);
  $("realizedToday").className = "stat-value num " + pnlClass(dayPnl);
  $("lossLimitSub").textContent =
    `booked ${signed(p.realized_today)} · daily stop ${money(p.daily_loss_limit)}`;
  paintLossMeter(dayPnl, p.daily_loss_limit);

  $("unrealized").textContent = signed(p.unrealized_pnl);
  $("unrealized").className = "stat-value num " + pnlClass(p.unrealized_pnl);
  $("winRate").textContent =
    p.win_rate === null
      ? `${p.trades_opened} opened, none closed yet`
      : `${p.win_rate}% of ${p.trades_closed} closed were winners`;

  $("haltBanner").classList.toggle("hidden", !p.halted);
  $("haltReason").textContent = p.halt_reason || "";

  renderSpark(curve || [], p.starting_capital);
}

function paintLossMeter(dayPnl, limit) {
  const meter = $("limitMeter");
  const fill = $("limitMeterFill");
  if (!meter || !fill) return;
  const cap = Math.abs(Number(limit) || 0);
  if (cap <= 0 || dayPnl >= 0) {
    meter.hidden = true;
    return;
  }
  const used = Math.min(100, (Math.abs(dayPnl) / cap) * 100);
  meter.hidden = false;
  fill.style.width = `${used.toFixed(1)}%`;
  fill.classList.toggle("hot", used >= 80);
}

function renderSpark(points, start) {
  const svg = $("spark");
  if (points.length < 2) { svg.innerHTML = ""; return; }

  const vals = points.map((p) => p.equity);
  const scale = Number.isFinite(start) ? vals.concat([start]) : vals;
  const min = Math.min(...scale), max = Math.max(...scale);
  const span = max - min || 1;
  const yOf = (v) => (50 - ((v - min) / span) * 44).toFixed(1);
  const step = 300 / (points.length - 1);

  const d = vals
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${yOf(v)}`)
    .join(" ");

  const up = vals[vals.length - 1] >= (Number.isFinite(start) ? start : vals[0]);
  const base = Number.isFinite(start)
    ? `<line x1="0" x2="300" y1="${yOf(start)}" y2="${yOf(start)}" stroke="var(--text-faint)" stroke-width="1" stroke-dasharray="3 3" opacity="0.65"/>`
    : "";
  svg.innerHTML =
    base +
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
    if ($("openValue")) {
      $("openValue").textContent = money(0);
      $("openValue").dataset.live = "1";
      $("openValue").className = "stat-value dim";
    }
    if ($("openSub")) {
      $("openSub").textContent = slotLine
        ? `nothing at risk · ${slotLine}`
        : "nothing at risk";
    }
    paintCommandLock();
    return;
  }

  const atRisk = dollarsToStops(positions);
  if ($("openValue")) {
    $("openValue").textContent = money(atRisk);
    $("openValue").dataset.live = "1";
    $("openValue").className = "stat-value " + (atRisk > 0 ? "dim" : "");
  }
  if ($("openSub")) {
    $("openSub").textContent = slotLine
      ? `if every stop fills now · ${slotLine}`
      : "if every stop fills now";
  }

  host.innerHTML = positions.map((p) => {
    const tag = p.direction === "LONG_CALL" ? "call"
              : p.direction === "LONG_PUT" ? "put"
              : p.direction === "LONG_SHARE" ? "share" : "spot";
    const pct = Math.max(0, Math.min(100, (p.progress ?? 0) * 100));
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
          <button class="danger" data-close="${esc(p.position_id)}" ${commandsReady() ? "" : "disabled"}>Close now</button>
          <span class="faint" style="font-size:11px">opened ${shortTime(p.entry_ts)}</span>
        </div>
      </div>`;
  }).join("");
  paintCommandLock();
}

/* ============================== scan board =============================== */
function renderScan(scan) {
  const host = $("scanBoard");
  const results = scan?.results || [];

  const above = results.filter((r) => r.total_score !== null && r.total_score >= scoreFloor).length;
  $("scanMeta").textContent = scan?.scan_id
    ? `${scan.scan_id} · ${scan.market} · ${scan.duration_ms ?? "—"}ms · ${scan.executed} opened · ${above} at or above ${Math.round(scoreFloor)}`
    : "";

  if (!results.length) {
    host.innerHTML =
      `<div class="empty"><strong>No scan yet</strong>
       The board fills in after the engine's first pass over the universe.</div>`;
    return;
  }

  const head = `
    <div class="scan-head">
      <span>Name</span>
      <span class="scan-head-pillars"><span>Liq</span><span>Tech</span><span>Sent</span></span>
      <span>Score</span>
    </div>`;

  host.innerHTML = head + results.map((r) => {
    const col = (label, v) => {
      const n = Math.max(0, Math.min(100, v || 0));
      return `<div class="pillar-col"><div class="pillar" title="${label} ${Math.round(n)}"><span style="width:${n}%"></span></div><span class="pillar-num">${Math.round(n)}</span></div>`;
    };

    let why, cls = "";
    if (r.verdict === "EXECUTE") { why = `<b>Opened</b> — ${esc(r.reason)}`; cls = "exec"; }
    else if (r.verdict === "BLOCKED") why = `<b>${esc(r.blocked_by)}</b> — ${esc(r.reason)}`;
    else if (r.verdict === "ERROR") why = `<b>No data</b> — ${esc(r.reason)}`;
    else why = esc(r.reason);

    const score = r.total_score;
    const hot = score !== null && score >= scoreFloor;
    return `
      <div class="scan-row">
        <div class="scan-sym">${esc(r.symbol)}</div>
        <div>
          <div class="pillars" title="liquidity, technical, sentiment">
            ${col("Liq", r.liquidity)}${col("Tech", r.technical)}${col("Sent", r.sentiment)}
          </div>
          <div class="scan-why ${cls}">${why}</div>
        </div>
        <div class="scan-total ${score === null ? "" : hot ? "hot" : "cold"}">${score !== null ? score.toFixed(0) : "—"}</div>
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
function renderSentiment(s) {
  const el = $("newsBias");
  const sub = $("newsBiasSub");
  if (!el || !sub) return;
  // Equity desk only — MACRO news bias.
  const scopeKey = "macro";
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
    sub.textContent = `${row.note || scopeKey.toUpperCase()}${age}`;
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
function authHeaders() {
  const token = sessionStorage.getItem(TOKEN_KEY) || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Dashboard-Token"] = token;
  return headers;
}

async function send(path, body) {
  $("cmdResult").textContent = "Queueing…";
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) {
      $("authRow")?.classList.remove("hidden");
      $("cmdResult").textContent = "The desk token was rejected. Enter it above and try again.";
      return;
    }
    $("cmdResult").textContent = r.ok
      ? `Queued ${data.kind}. The engine picks it up on its next tick.`
      : `That did not go through: ${data.detail || r.status}`;
  } catch (err) {
    $("cmdResult").textContent = `Could not reach the dashboard API: ${err.message}`;
  }
  refresh();
}

$("saveToken")?.addEventListener("click", () => {
  const value = ($("dashToken").value || "").trim();
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
  $("dashToken").value = "";
  paintCommandLock();
  $("cmdResult").textContent = value
    ? "Token saved in this tab. It is not written to the book."
    : "Token cleared from this tab.";
});

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
  if (action === "halt") {
    if (confirm("Halt new trades until you resume?")) {
      send("/api/commands/halt", { reason: "halted from the dashboard" });
    }
  }
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
    const foot = $("footerNote");
    if (foot && d.session) {
      const instrument = d.session.equity_instrument === "options" ? "options" : "shares";
      foot.textContent =
        `Paper ${instrument}. Fills cross the spread and pay fees. Commands need the desk token when one is set.`;
    }
    renderPortfolio(d.portfolio, d.equity_curve);
    renderPositions(d.positions);
    renderScan(d.scan);
    renderTape(d.events);
    renderSystem(d.system);
    renderSentiment(d.sentiment);
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
