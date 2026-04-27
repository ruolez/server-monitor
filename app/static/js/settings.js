'use strict';
(() => {
  const { $, api, toast, showModal, hideModal } = window.SM;

  const smtpForm    = $('#smtpForm');
  const policyForm  = $('#policyForm');
  const passwordState = $('#smtpPasswordState');

  const testModal     = $('#testSmtpModal');
  const testForm      = $('#testSmtpForm');

  function fill(form, data, fields) {
    for (const f of fields) {
      const el = form.elements[f];
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = !!data[f];
      else                         el.value = data[f] ?? '';
    }
  }

  async function load() {
    const data = await api('/api/settings');
    fill(smtpForm, data, ['smtp_host','smtp_port','smtp_username','smtp_from_address','smtp_from_name','smtp_use_starttls']);
    fill(policyForm, data, ['reminder_interval_minutes','retention_days','default_check_interval_seconds']);
    passwordState.textContent = data.smtp_password_set ? '(stored — leave blank to keep)' : '(not yet set)';
  }

  smtpForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const payload = {
      smtp_host:         smtpForm.elements.smtp_host.value.trim(),
      smtp_port:         Number(smtpForm.elements.smtp_port.value),
      smtp_username:     smtpForm.elements.smtp_username.value.trim(),
      smtp_from_address: smtpForm.elements.smtp_from_address.value.trim(),
      smtp_from_name:    smtpForm.elements.smtp_from_name.value.trim(),
      smtp_use_starttls: smtpForm.elements.smtp_use_starttls.checked,
    };
    const pw = smtpForm.elements.smtp_password.value;
    if (pw) payload.smtp_password = pw;
    try {
      await api('/api/settings', { method: 'PUT', body: payload });
      smtpForm.elements.smtp_password.value = '';
      toast('SMTP settings saved', 'success');
      load();
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  });

  policyForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const payload = {
      reminder_interval_minutes:      Number(policyForm.elements.reminder_interval_minutes.value),
      retention_days:                 Number(policyForm.elements.retention_days.value),
      default_check_interval_seconds: Number(policyForm.elements.default_check_interval_seconds.value),
    };
    try {
      await api('/api/settings', { method: 'PUT', body: payload });
      toast('Policy saved', 'success');
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  });

  $('#testSmtpBtn').addEventListener('click', () => { testForm.reset(); showModal(testModal); });
  $('#testSmtpClose').addEventListener('click', () => hideModal(testModal));
  $('#testSmtpCancel').addEventListener('click', () => hideModal(testModal));
  testModal.addEventListener('click', (e) => { if (e.target === testModal) hideModal(testModal); });

  testForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const to = testForm.elements.to.value.trim();
    try {
      await api('/api/settings/test-smtp', { method: 'POST', body: { to } });
      toast('Test email sent — check your inbox', 'success', 5000);
      hideModal(testModal);
    } catch (err) {
      toast(err.message || 'Send failed', 'error', 6000);
    }
  });

  load();
})();
