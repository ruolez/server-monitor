'use strict';
(() => {
  const { $, api, toast, fmtRelative, fmtDuration, escapeHtml,
          statusPill, renderSparkline, updateSummaryChip,
          showModal, hideModal, renderUptimeBars } = window.SM;

  const cards = $('#cards');
  const empty = $('#empty');
  let inFlight = false;
  let tick = 0;
  let lastServers = [];           // latest /api/status servers payload
  const sparkCache = new Map();   // server id -> sparkline samples
  const statusSig = new Map();    // server id -> status signature

  function targetText(s) {
    return s.check_type === 'tcp' ? `${s.hostname}:${s.tcp_port}` : s.hostname;
  }

  function updateSchedulerWarning(sched) {
    const banner = $('#schedulerWarning');
    if (!banner) return;
    if (sched && sched.stale) {
      $('#schedulerWarningAge').textContent = fmtDuration(sched.age_seconds);
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
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

  function pillGroup(s) {
    const pills = [statusPill(s.current_status)];
    if (s.in_maintenance) {
      pills.push('<span class="status-pill pill-maint"><span class="dot"></span>MAINT</span>');
    }
    if (s.is_flapping) {
      pills.push('<span class="status-pill pill-warnstate"><span class="dot"></span>FLAPPING</span>');
    }
    if (s.current_status === 'up' && s.degraded_since) {
      const tip = `Slow since ${new Date(s.degraded_since).toLocaleString()}`;
      pills.push(`<span class="status-pill pill-warnstate" title="${escapeHtml(tip)}"><span class="dot"></span>DEGRADED</span>`);
    }
    return `<span class="pill-group">${pills.join('')}</span>`;
  }

  function cardHtml(s) {
    const samples = s.sparkline || sparkCache.get(s.id) || [];
    const pct = uptimePct(samples);
    const lastSample = samples[samples.length - 1];
    const oldest = samples[0];
    return `
      <article class="card server-card ${s.current_status === 'down' && !s.in_maintenance ? 'is-down' : ''}" data-id="${s.id}">
        <div class="server-head">
          <div>
            <div class="server-name">${escapeHtml(s.name)}</div>
            <div class="server-target">${escapeHtml(targetText(s))}</div>
          </div>
          ${pillGroup(s)}
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

  // Light poll (no sparklines) every tick; full poll every 10th tick or
  // immediately when any server's status changes between light polls.
  async function refresh(forceFull = false) {
    if (inFlight) return;
    inFlight = true;
    let needFull = false;
    try {
      const full = forceFull || tick % 10 === 0;
      tick++;
      const data = await api('/api/status' + (full ? '' : '?sparklines=0'));
      updateSummaryChip(data.summary);
      updateSchedulerWarning(data.scheduler);
      const servers = data.servers || [];
      lastServers = servers;
      for (const s of servers) {
        if (s.sparkline) sparkCache.set(s.id, s.sparkline);
        const sig = `${s.current_status}|${s.last_status_change_at || ''}`;
        if (!full && statusSig.has(s.id) && statusSig.get(s.id) !== sig) needFull = true;
        statusSig.set(s.id, sig);
      }
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
    if (needFull) refresh(true);
  }

  // ---- server detail modal (latency chart + uptime history) ----
  const detailModal = $('#detailModal');
  const chartEl = $('#latencyChart');
  let detailId = null;
  let detailRange = '24h';
  let detailTimer = null;
  let chart = null;

  function destroyChart() {
    if (chart) { chart.destroy(); chart = null; }
    chartEl.innerHTML = '';
  }

  // The API returns sparse buckets; insert nulls for missing ones so uPlot
  // renders honest gaps instead of interpolating across them.
  function densify(points, bucketSeconds) {
    if (!points.length) return [[], [], [], []];
    const ts = [], avg = [], max = [], min = [];
    const byT = new Map(points.map(p => [p.t, p]));
    const first = points[0].t, last = points[points.length - 1].t;
    for (let t = first; t <= last; t += bucketSeconds) {
      const p = byT.get(t);
      ts.push(t);
      avg.push(p ? p.avg_ms : null);
      max.push(p ? p.max_ms : null);
      min.push(p ? p.min_ms : null);
    }
    return [ts, avg, max, min];
  }

  function bucketStripHtml(points) {
    if (!points.length) return '';
    const segs = points.map(p => {
      const ratio = p.checks ? p.up_checks / p.checks : null;
      const cls = ratio == null ? 's-empty' : ratio >= 1 ? 's-up' : 's-down';
      const when = new Date(p.t * 1000).toLocaleString();
      const tip = ratio == null ? `${when} · no data`
        : `${when} · ${Math.round(ratio * 100)}% up${p.avg_ms != null ? ` · ${p.avg_ms}ms avg` : ''}`;
      return `<span class="sparkline-seg ${cls}" title="${escapeHtml(tip)}"></span>`;
    });
    return `<div class="sparkline">${segs.join('')}</div>`;
  }

  function statStripHtml(m) {
    const stat = (label, v) => `<span>${label} <b class="num">${v}</b></span>`;
    return [
      stat('Uptime', m.uptime_pct != null ? m.uptime_pct + '%' : '—'),
      stat('Avg', m.avg_ms != null ? m.avg_ms + 'ms' : '—'),
      stat('Min', m.min_ms != null ? m.min_ms + 'ms' : '—'),
      stat('Max', m.max_ms != null ? m.max_ms + 'ms' : '—'),
      stat('Checks', m.checks ?? 0),
    ].join('');
  }

  function buildChart(data) {
    destroyChart();
    const axisStyle = { stroke: '#8b95a7', grid: { stroke: '#1a212d' }, ticks: { stroke: '#1a212d' } };
    chart = new uPlot({
      width: Math.max(320, chartEl.clientWidth || 760),
      height: 220,
      series: [
        {},
        { label: 'avg', stroke: '#3b82f6', width: 2, spanGaps: false },
        { label: 'max', stroke: 'rgba(59,130,246,.28)', width: 1, spanGaps: false },
        { label: 'min', stroke: 'rgba(59,130,246,.28)', width: 1, spanGaps: false },
      ],
      bands: [{ series: [2, 3], fill: 'rgba(59,130,246,.10)' }],
      axes: [
        axisStyle,
        { ...axisStyle, size: 56, values: (u, vals) => vals.map(v => v + 'ms') },
      ],
      legend: { show: false },
    }, data, chartEl);
  }

  async function loadMetrics(range) {
    const m = await api(`/api/servers/${detailId}/metrics?range=${range}`);
    $('#detailStats').innerHTML = statStripHtml(m);
    if (m.points.length) {
      buildChart(densify(m.points, m.bucket_seconds));
    } else {
      destroyChart();
      chartEl.innerHTML = '<p class="hint" style="padding:30px 0;text-align:center">No check data in this range yet.</p>';
    }
    $('#bucketStrip').innerHTML = bucketStripHtml(m.points);
  }

  async function loadDetailUptime() {
    const u = await api(`/api/uptime?days=90&server_id=${detailId}`);
    const row = (u.servers || [])[0];
    $('#detailUptime').innerHTML = row
      ? `<div class="hint" style="margin:0 0 6px">${row.uptime_pct != null ? row.uptime_pct + '% over available data' : 'no data yet'}</div>`
        + renderUptimeBars(row.days, 90)
      : '<p class="hint">No data.</p>';
  }

  async function openDetail(id) {
    detailId = id;
    const cardData = lastServers.find(x => String(x.id) === String(id));
    if (cardData) {
      $('#detailTitle').textContent = cardData.name;
      $('#detailTarget').textContent = targetText(cardData);
      $('#detailPill').innerHTML = pillGroup(cardData);
    }
    showModal(detailModal);  // chart must be sized AFTER unhide ([hidden] => 0x0)
    try {
      await Promise.all([loadMetrics(detailRange), loadDetailUptime()]);
    } catch (err) {
      toast(err.message || 'Failed to load metrics', 'error');
    }
    clearInterval(detailTimer);
    detailTimer = setInterval(() => { loadMetrics(detailRange).catch(() => {}); }, 30000);
  }

  function closeDetail() {
    clearInterval(detailTimer);
    detailTimer = null;
    detailId = null;
    destroyChart();
    hideModal(detailModal);
  }

  $('#rangeTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-range]');
    if (!btn || !detailId) return;
    detailRange = btn.dataset.range;
    for (const b of $('#rangeTabs').querySelectorAll('button')) b.classList.toggle('is-active', b === btn);
    loadMetrics(detailRange).catch(err => toast(err.message || 'Failed to load metrics', 'error'));
  });

  $('#detailCheckNow').addEventListener('click', async () => {
    if (!detailId) return;
    const btn = $('#detailCheckNow');
    btn.disabled = true;
    try {
      const r = await api(`/api/servers/${detailId}/check-now`, { method: 'POST' });
      toast(`Checked: ${r.status}${r.latency_ms != null ? ` · ${r.latency_ms}ms` : ''}`, r.status === 'up' ? 'success' : 'error');
      refresh(true);
      loadMetrics(detailRange).catch(() => {});
    } catch (err) {
      toast(err.message || 'Check failed', 'error');
    } finally {
      btn.disabled = false;
    }
  });

  $('#detailClose').addEventListener('click', closeDetail);
  detailModal.addEventListener('click', (e) => { if (e.target === detailModal) closeDetail(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && detailId) closeDetail(); });

  cards.addEventListener('click', (e) => {
    const card = e.target.closest('.server-card');
    if (!card) return;
    openDetail(card.dataset.id);
  });

  refresh();
  setInterval(() => refresh(), 3000);
})();
