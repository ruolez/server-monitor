'use strict';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...opts,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText || 'request failed');
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(msg, kind = 'info', ms = 3000) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.className = 'toast' + (kind === 'error' ? ' is-error' : kind === 'success' ? ' is-success' : '');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

function fmtRelative(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 5)    return 'just now';
  if (diff < 60)   return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (s < 86400) return `${h}h ${m}m`;
  const d = Math.floor(s / 86400);
  return `${d}d ${h % 24}h`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function statusPill(status) {
  const cls = status === 'up' ? 'pill-up' : status === 'down' ? 'pill-down' : 'pill-unknown';
  const label = (status || 'unknown').toUpperCase();
  return `<span class="status-pill ${cls}"><span class="dot"></span>${label}</span>`;
}

function renderSparkline(samples, expectedSegments = 96) {
  const segs = [];
  // Pad with empty placeholders on the left so newest is on the right.
  const pad = Math.max(0, expectedSegments - samples.length);
  for (let i = 0; i < pad; i++) segs.push(`<span class="sparkline-seg s-empty"></span>`);
  for (const s of samples) {
    const cls = s.status === 'up' ? 's-up' : 's-down';
    const tip = `${s.status.toUpperCase()} · ${new Date(s.checked_at).toLocaleString()}` +
                (s.latency_ms != null ? ` · ${s.latency_ms}ms` : '');
    segs.push(`<span class="sparkline-seg ${cls}" title="${escapeHtml(tip)}"></span>`);
  }
  return `<div class="sparkline">${segs.join('')}</div>`;
}

function updateSummaryChip(summary) {
  const el = document.getElementById('globalSummary');
  if (!el || !summary) return;
  el.querySelector('[data-k="up"]').textContent      = summary.up_count      ?? 0;
  el.querySelector('[data-k="down"]').textContent    = summary.down_count    ?? 0;
  el.querySelector('[data-k="unknown"]').textContent = summary.unknown_count ?? 0;
  el.hidden = false;
}

function showModal(el)  { el.hidden = false;  document.body.style.overflow = 'hidden'; }
function hideModal(el)  { el.hidden = true;   document.body.style.overflow = ''; }

// Status-page style daily uptime bars: one span per calendar day, oldest on
// the left. `days` is [{day: 'YYYY-MM-DD', uptime_pct, checks}] (sparse —
// missing days render gray), `totalDays` pads the strip to a fixed width.
function renderUptimeBars(days, totalDays) {
  const byDay = new Map((days || []).map(d => [d.day, d]));
  const spans = [];
  const today = new Date();
  for (let i = totalDays - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const row = byDay.get(iso);
    let cls = 'u-none';
    let tip = `${iso} · no data`;
    if (row) {
      cls = row.uptime_pct >= 99.9 ? 'u-full' : row.uptime_pct >= 97 ? 'u-high' : 'u-low';
      tip = `${iso} · ${row.uptime_pct}% · ${row.checks} checks`;
    }
    spans.push(`<span class="ubar ${cls}" title="${escapeHtml(tip)}"></span>`);
  }
  return `<div class="ubars">${spans.join('')}</div>`;
}

window.SM = { $, $$, api, toast, fmtRelative, fmtDuration, fmtTime, escapeHtml,
              statusPill, renderSparkline, updateSummaryChip, showModal, hideModal,
              renderUptimeBars };
