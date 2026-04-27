'use strict';
(() => {
  const { $, $$, api, toast, fmtRelative, escapeHtml, statusPill, showModal, hideModal } = window.SM;

  const rowsEl   = $('#serverRows');
  const emptyEl  = $('#serversEmpty');
  const modal    = $('#serverModal');
  const form     = $('#serverForm');
  const titleEl  = $('#serverModalTitle');
  const deleteBtn = $('#serverDeleteBtn');
  const checkTypeSelect = $('#checkTypeSelect');
  const tcpPortField    = $('#tcpPortField');
  const recipientBoxes  = $('#recipientCheckboxes');

  let allRecipients = [];

  function targetText(s) {
    return s.check_type === 'tcp' ? `${s.hostname}:${s.tcp_port}` : s.hostname;
  }

  function rowHtml(s) {
    return `
      <tr data-id="${s.id}">
        <td>${statusPill(s.current_status)}</td>
        <td><b>${escapeHtml(s.name)}</b></td>
        <td><span class="num">${escapeHtml(targetText(s))}</span></td>
        <td>${s.check_type.toUpperCase()}</td>
        <td class="num">${s.interval_seconds}s</td>
        <td>${fmtRelative(s.last_checked_at)}</td>
        <td class="num">${s.last_latency_ms != null ? s.last_latency_ms + 'ms' : '—'}</td>
        <td>${s.override_count > 0 ? `${s.override_count} override(s)` : 'default'}</td>
        <td class="row-actions">
          <button class="btn-icon" data-act="check" title="Check now">↻</button>
          <button class="btn-icon" data-act="edit" title="Edit">✎</button>
        </td>
      </tr>
    `;
  }

  async function loadAll() {
    const [servers, recipients] = await Promise.all([
      api('/api/servers'),
      api('/api/recipients'),
    ]);
    allRecipients = recipients;
    if (servers.length === 0) {
      rowsEl.innerHTML = '';
      emptyEl.hidden = false;
    } else {
      emptyEl.hidden = true;
      rowsEl.innerHTML = servers.map(rowHtml).join('');
    }
  }

  function setCheckTypeUi(value) {
    tcpPortField.hidden = value !== 'tcp';
    const portInput = form.elements.tcp_port;
    portInput.required = value === 'tcp';
  }

  function renderRecipientChecks(selectedIds = []) {
    if (allRecipients.length === 0) {
      recipientBoxes.innerHTML = `<div class="muted">No recipients yet — add one on the Recipients page.</div>`;
      return;
    }
    recipientBoxes.innerHTML = allRecipients.map(r => `
      <label class="checkbox">
        <input type="checkbox" name="recipient" value="${r.id}" ${selectedIds.includes(r.id) ? 'checked' : ''}>
        <span>${escapeHtml(r.email)}${r.is_default ? ' <em class="hint-inline">(default)</em>' : ''}</span>
      </label>
    `).join('');
  }

  function openCreateModal() {
    form.reset();
    form.elements.id.value = '';
    form.elements.enabled.checked = true;
    form.elements.interval_seconds.value = 60;
    form.elements.timeout_seconds.value = 5;
    form.elements.failure_threshold.value = 3;
    setCheckTypeUi('icmp');
    titleEl.textContent = 'New server';
    deleteBtn.hidden = true;
    renderRecipientChecks([]);
    showModal(modal);
  }

  async function openEditModal(id) {
    const s = await api(`/api/servers/${id}`);
    form.elements.id.value = s.id;
    form.elements.name.value = s.name;
    form.elements.hostname.value = s.hostname;
    form.elements.check_type.value = s.check_type;
    form.elements.tcp_port.value = s.tcp_port ?? '';
    form.elements.interval_seconds.value = s.interval_seconds;
    form.elements.timeout_seconds.value = s.timeout_seconds;
    form.elements.failure_threshold.value = s.failure_threshold;
    form.elements.enabled.checked = !!s.enabled;
    setCheckTypeUi(s.check_type);
    titleEl.textContent = `Edit · ${s.name}`;
    deleteBtn.hidden = false;
    renderRecipientChecks(s.override_recipient_ids || []);
    showModal(modal);
  }

  async function submitForm(ev) {
    ev.preventDefault();
    const id = form.elements.id.value;
    const payload = {
      name: form.elements.name.value.trim(),
      hostname: form.elements.hostname.value.trim(),
      check_type: form.elements.check_type.value,
      tcp_port: form.elements.tcp_port.value ? Number(form.elements.tcp_port.value) : null,
      interval_seconds: Number(form.elements.interval_seconds.value),
      timeout_seconds: Number(form.elements.timeout_seconds.value),
      failure_threshold: Number(form.elements.failure_threshold.value),
      enabled: form.elements.enabled.checked,
      override_recipient_ids: $$('input[name="recipient"]:checked', recipientBoxes).map(i => Number(i.value)),
    };
    try {
      if (id) {
        await api(`/api/servers/${id}`, { method: 'PUT', body: payload });
        toast('Server updated', 'success');
      } else {
        await api('/api/servers', { method: 'POST', body: payload });
        toast('Server created', 'success');
      }
      hideModal(modal);
      loadAll();
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  }

  async function deleteServer() {
    const id = form.elements.id.value;
    if (!id) return;
    if (!confirm('Delete this server? Outage history and check results will also be removed.')) return;
    try {
      await api(`/api/servers/${id}`, { method: 'DELETE' });
      toast('Server deleted', 'success');
      hideModal(modal);
      loadAll();
    } catch (err) {
      toast(err.message || 'Delete failed', 'error');
    }
  }

  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.closest('tr').dataset.id;
    if (btn.dataset.act === 'edit') {
      openEditModal(id);
    } else if (btn.dataset.act === 'check') {
      try {
        const r = await api(`/api/servers/${id}/check-now`, { method: 'POST' });
        toast(`Checked: ${r.status}${r.latency_ms != null ? ` · ${r.latency_ms}ms` : ''}`, r.status === 'up' ? 'success' : 'error');
        loadAll();
      } catch (err) {
        toast(err.message || 'Check failed', 'error');
      }
    }
  });

  $('#newServerBtn').addEventListener('click', openCreateModal);
  $('#serverModalClose').addEventListener('click', () => hideModal(modal));
  $('#serverCancelBtn').addEventListener('click', () => hideModal(modal));
  deleteBtn.addEventListener('click', deleteServer);
  checkTypeSelect.addEventListener('change', (e) => setCheckTypeUi(e.target.value));
  form.addEventListener('submit', submitForm);
  modal.addEventListener('click', (e) => { if (e.target === modal) hideModal(modal); });

  loadAll();
})();
