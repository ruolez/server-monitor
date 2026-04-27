'use strict';
(() => {
  const { $, api, fmtDuration, fmtTime, escapeHtml } = window.SM;

  const outageList    = $('#outageList');
  const outageEmpty   = $('#outageEmpty');
  const checkRows     = $('#checkRows');
  const checksEmpty   = $('#checksEmpty');
  const serverFilter  = $('#serverFilter');

  async function loadServers() {
    const servers = await api('/api/servers');
    serverFilter.innerHTML =
      '<option value="">All servers</option>' +
      servers.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
  }

  async function loadOutages() {
    const id = serverFilter.value;
    const url = id ? `/api/history/outages?server_id=${id}` : '/api/history/outages';
    const rows = await api(url);
    if (rows.length === 0) {
      outageList.innerHTML = '';
      outageEmpty.hidden = false;
      return;
    }
    outageEmpty.hidden = true;
    outageList.innerHTML = rows.map(o => {
      const open = !o.ended_at;
      const dur = open
        ? `Ongoing · started ${fmtTime(o.started_at)}`
        : `Lasted ${fmtDuration(o.duration_seconds)} · ${fmtTime(o.started_at)} → ${fmtTime(o.ended_at)}`;
      return `
        <div class="timeline-item">
          <span class="timeline-bullet ${open ? 'is-open' : 'is-closed'}"></span>
          <div>
            <div class="timeline-title">${escapeHtml(o.server_name)}</div>
            <div class="timeline-meta">
              <span>${dur}</span>
              <span>Reminders: ${o.reminder_count || 0}</span>
              ${o.recovery_alert_sent_at ? '<span>Recovery sent</span>' : ''}
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  async function loadChecks() {
    const id = serverFilter.value;
    const url = id ? `/api/history/checks?server_id=${id}&limit=200` : '/api/history/checks?limit=200';
    const rows = await api(url);
    if (rows.length === 0) {
      checkRows.innerHTML = '';
      checksEmpty.hidden = false;
      return;
    }
    checksEmpty.hidden = true;
    checkRows.innerHTML = rows.map(c => `
      <tr>
        <td class="num">${fmtTime(c.checked_at)}</td>
        <td>${escapeHtml(c.server_name)}</td>
        <td>${c.status === 'up' ? '<span class="status-pill pill-up"><span class="dot"></span>UP</span>' : '<span class="status-pill pill-down"><span class="dot"></span>DOWN</span>'}</td>
        <td class="num">${c.latency_ms != null ? c.latency_ms + 'ms' : '—'}</td>
        <td>${escapeHtml(c.error_message || '')}</td>
      </tr>
    `).join('');
  }

  serverFilter.addEventListener('change', () => { loadOutages(); loadChecks(); });

  (async () => {
    await loadServers();
    await Promise.all([loadOutages(), loadChecks()]);
  })();
})();
