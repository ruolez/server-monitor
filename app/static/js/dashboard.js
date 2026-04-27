'use strict';
(() => {
  const { $, api, toast, fmtRelative, fmtDuration, escapeHtml,
          statusPill, renderSparkline, updateSummaryChip } = window.SM;

  const cards = $('#cards');
  const empty = $('#empty');
  let inFlight = false;

  function targetText(s) {
    return s.check_type === 'tcp' ? `${s.hostname}:${s.tcp_port}` : s.hostname;
  }

  function downSince(s) {
    if (!s.down_since) return '';
    const seconds = Math.max(0, (Date.now() - new Date(s.down_since).getTime()) / 1000);
    return `<div class="down-banner">⚠ Down for <b>${fmtDuration(seconds)}</b></div>`;
  }

  function uptimePct(samples) {
    if (!samples.length) return null;
    const up = samples.filter(s => s.status === 'up').length;
    return Math.round((up / samples.length) * 1000) / 10;  // one decimal
  }

  function cardHtml(s) {
    const samples = s.sparkline || [];
    const pct = uptimePct(samples);
    const lastSample = samples[samples.length - 1];
    const oldest = samples[0];
    return `
      <article class="card server-card ${s.current_status === 'down' ? 'is-down' : ''}" data-id="${s.id}">
        <div class="server-head">
          <div>
            <div class="server-name">${escapeHtml(s.name)}</div>
            <div class="server-target">${escapeHtml(targetText(s))}</div>
          </div>
          ${statusPill(s.current_status)}
        </div>
        ${downSince(s)}
        <div>
          ${renderSparkline(samples)}
          <div class="sparkline-meta">
            <span>${oldest ? new Date(oldest.checked_at).toLocaleTimeString() : '—'}</span>
            <span>${pct != null ? pct + '% up' : 'no data'}</span>
            <span>${lastSample ? new Date(lastSample.checked_at).toLocaleTimeString() : '—'}</span>
          </div>
        </div>
        <div class="server-meta">
          <span>Latency <b class="num">${s.last_latency_ms != null ? s.last_latency_ms + 'ms' : '—'}</b></span>
          <span>Last check <b>${fmtRelative(s.last_checked_at)}</b></span>
          <span>Every <b>${s.interval_seconds}s</b></span>
          <span>${s.check_type.toUpperCase()}</span>
        </div>
      </article>
    `;
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const data = await api('/api/status');
      updateSummaryChip(data.summary);
      const servers = data.servers || [];
      if (servers.length === 0) {
        cards.innerHTML = '';
        empty.hidden = false;
      } else {
        empty.hidden = true;
        cards.innerHTML = servers.map(cardHtml).join('');
      }
    } catch (err) {
      console.error(err);
      if (err.status === 401) {
        window.location.href = '/login';
        return;
      }
      toast('Failed to load status', 'error');
    } finally {
      inFlight = false;
    }
  }

  cards.addEventListener('click', async (e) => {
    const card = e.target.closest('.server-card');
    if (!card) return;
    const id = card.dataset.id;
    try {
      await api(`/api/servers/${id}/check-now`, { method: 'POST' });
      toast('Check queued', 'success');
      refresh();
    } catch (err) {
      toast(err.message || 'Check failed', 'error');
    }
  });

  refresh();
  setInterval(refresh, 5000);
})();
