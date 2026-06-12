'use strict';
(() => {
  const { $, api, fmtDuration, escapeHtml, statusPill, renderUptimeBars } = window.SM;

  const rowsEl = $('#publicRows');
  const emptyEl = $('#publicEmpty');
  let uptimeByServer = new Map();

  function rowHtml(s) {
    const down = s.current_status === 'down' && s.down_since
      ? `<div class="down-banner">⚠ Down for <b>${fmtDuration((Date.now() - new Date(s.down_since).getTime()) / 1000)}</b></div>`
      : '';
    const u = uptimeByServer.get(s.id);
    return `
      <div class="public-row">
        <div class="public-row-head">
          <b>${escapeHtml(s.name)}</b>
          <span class="public-row-right">
            ${u && u.uptime_pct != null ? `<span class="num public-pct">${u.uptime_pct}%</span>` : ''}
            ${statusPill(s.current_status)}
          </span>
        </div>
        ${down}
        ${u ? renderUptimeBars(u.days, 30) : ''}
      </div>
    `;
  }

  async function refreshStatus() {
    try {
      const servers = await api('/api/public/status');
      if (!servers.length) {
        rowsEl.innerHTML = '';
        emptyEl.hidden = false;
        return;
      }
      emptyEl.hidden = true;
      rowsEl.innerHTML = servers.map(rowHtml).join('');
    } catch (_) { /* page disabled or transient error — keep last render */ }
  }

  async function refreshUptime() {
    try {
      const data = await api('/api/public/uptime?days=30');
      uptimeByServer = new Map((data.servers || []).map(s => [s.server_id, s]));
    } catch (_) { /* ignore */ }
  }

  (async () => {
    await refreshUptime();
    await refreshStatus();
  })();
  setInterval(refreshStatus, 10000);
  // The daily aggregate is the expensive query; refresh it rarely.
  setInterval(async () => { await refreshUptime(); refreshStatus(); }, 600000);
})();
