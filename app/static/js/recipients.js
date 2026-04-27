'use strict';
(() => {
  const { $, api, toast, escapeHtml, showModal, hideModal } = window.SM;

  const rowsEl  = $('#recipientRows');
  const emptyEl = $('#recipientsEmpty');
  const modal   = $('#recipientModal');
  const form    = $('#recipientForm');
  const titleEl = $('#recipientModalTitle');
  const deleteBtn = $('#recipientDeleteBtn');

  function rowHtml(r) {
    return `
      <tr data-id="${r.id}">
        <td><b>${escapeHtml(r.email)}</b></td>
        <td>${escapeHtml(r.name || '')}</td>
        <td>${r.is_default ? '<span class="status-pill pill-up"><span class="dot"></span>DEFAULT</span>' : '—'}</td>
        <td>${r.enabled ? 'Yes' : 'No'}</td>
        <td class="row-actions"><button class="btn-icon" data-act="edit" title="Edit">✎</button></td>
      </tr>
    `;
  }

  async function load() {
    const rows = await api('/api/recipients');
    if (rows.length === 0) {
      rowsEl.innerHTML = '';
      emptyEl.hidden = false;
    } else {
      emptyEl.hidden = true;
      rowsEl.innerHTML = rows.map(rowHtml).join('');
    }
  }

  function openCreate() {
    form.reset();
    form.elements.id.value = '';
    form.elements.enabled.checked = true;
    titleEl.textContent = 'New recipient';
    deleteBtn.hidden = true;
    showModal(modal);
  }

  async function openEdit(id) {
    const rows = await api('/api/recipients');
    const r = rows.find(x => String(x.id) === String(id));
    if (!r) return;
    form.elements.id.value = r.id;
    form.elements.email.value = r.email;
    form.elements.name.value = r.name || '';
    form.elements.is_default.checked = !!r.is_default;
    form.elements.enabled.checked = !!r.enabled;
    titleEl.textContent = `Edit · ${r.email}`;
    deleteBtn.hidden = false;
    showModal(modal);
  }

  async function submit(ev) {
    ev.preventDefault();
    const id = form.elements.id.value;
    const payload = {
      email: form.elements.email.value.trim().toLowerCase(),
      name: form.elements.name.value.trim() || null,
      is_default: form.elements.is_default.checked,
      enabled: form.elements.enabled.checked,
    };
    try {
      if (id) {
        await api(`/api/recipients/${id}`, { method: 'PUT', body: payload });
        toast('Recipient updated', 'success');
      } else {
        await api('/api/recipients', { method: 'POST', body: payload });
        toast('Recipient created', 'success');
      }
      hideModal(modal);
      load();
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  }

  async function del() {
    const id = form.elements.id.value;
    if (!id) return;
    if (!confirm('Delete this recipient? They will be removed from any per-server overrides.')) return;
    try {
      await api(`/api/recipients/${id}`, { method: 'DELETE' });
      toast('Recipient deleted', 'success');
      hideModal(modal);
      load();
    } catch (err) {
      toast(err.message || 'Delete failed', 'error');
    }
  }

  rowsEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act="edit"]');
    if (!btn) return;
    openEdit(btn.closest('tr').dataset.id);
  });

  $('#newRecipientBtn').addEventListener('click', openCreate);
  $('#recipientModalClose').addEventListener('click', () => hideModal(modal));
  $('#recipientCancelBtn').addEventListener('click', () => hideModal(modal));
  deleteBtn.addEventListener('click', del);
  form.addEventListener('submit', submit);
  modal.addEventListener('click', (e) => { if (e.target === modal) hideModal(modal); });

  load();
})();
