'use strict';
(() => {
  const { $, api, toast, showModal, hideModal, escapeHtml, fmtTime } = window.SM;

  const smtpForm    = $('#smtpForm');
  const policyForm  = $('#policyForm');
  const reportForm  = $('#reportForm');
  const passwordState = $('#smtpPasswordState');
  const reportLastSent = $('#reportLastSent');

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
    fill(policyForm, data, ['reminder_interval_minutes','retention_days','default_check_interval_seconds','flap_window_minutes','flap_threshold']);
    fill(reportForm, data, ['daily_report_enabled','daily_report_time','daily_report_timezone','public_status_enabled']);
    passwordState.textContent = data.smtp_password_set ? '(stored — leave blank to keep)' : '(not yet set)';
    reportLastSent.hidden = !data.daily_report_last_sent_on;
    if (data.daily_report_last_sent_on) {
      reportLastSent.textContent = `Last scheduled report sent: ${data.daily_report_last_sent_on}`;
    }
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
      flap_window_minutes:            Number(policyForm.elements.flap_window_minutes.value),
      flap_threshold:                 Number(policyForm.elements.flap_threshold.value),
    };
    try {
      await api('/api/settings', { method: 'PUT', body: payload });
      toast('Policy saved', 'success');
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  });

  reportForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const payload = {
      daily_report_enabled:  reportForm.elements.daily_report_enabled.checked,
      daily_report_time:     reportForm.elements.daily_report_time.value,
      daily_report_timezone: reportForm.elements.daily_report_timezone.value,
      public_status_enabled: reportForm.elements.public_status_enabled.checked,
    };
    try {
      await api('/api/settings', { method: 'PUT', body: payload });
      toast('Report settings saved', 'success');
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  });

  $('#sendReportBtn').addEventListener('click', async () => {
    const btn = $('#sendReportBtn');
    btn.disabled = true;
    try {
      await api('/api/settings/send-daily-report', { method: 'POST' });
      toast('Daily report sent — check your inbox', 'success', 5000);
    } catch (err) {
      toast(err.message || 'Send failed', 'error', 6000);
    } finally {
      btn.disabled = false;
    }
  });

  const passwordForm = $('#passwordForm');
  passwordForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const current = passwordForm.elements.current_password.value;
    const next    = passwordForm.elements.new_password.value;
    const confirm = passwordForm.elements.confirm_password.value;
    if (next !== confirm) {
      toast('New passwords do not match', 'error');
      return;
    }
    try {
      await api('/api/auth/change-password', {
        method: 'POST',
        body: { current_password: current, new_password: next },
      });
      passwordForm.reset();
      toast('Password changed', 'success');
    } catch (err) {
      toast(err.message || 'Password change failed', 'error');
    }
  });

  // ---- maintenance windows ----
  const maintList = $('#maintList');
  const maintForm = $('#maintForm');

  async function loadMaintServers() {
    const servers = await api('/api/servers');
    const sel = maintForm.elements.server_id;
    for (const s of servers) {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name;
      sel.appendChild(opt);
    }
  }

  async function loadMaintWindows() {
    const windows = await api('/api/maintenance');
    if (!windows.length) {
      maintList.innerHTML = '<p class="hint">No maintenance windows scheduled.</p>';
      return;
    }
    maintList.innerHTML = windows.map(w => `
      <div class="maint-item ${w.active ? 'is-active' : ''}">
        <div>
          <b>${escapeHtml(w.server_name || 'All servers')}</b>
          ${w.active ? '<span class="status-pill pill-maint"><span class="dot"></span>ACTIVE</span>' : ''}
          <div class="hint" style="margin:2px 0 0">
            ${fmtTime(w.starts_at)} → ${fmtTime(w.ends_at)}${w.note ? ' · ' + escapeHtml(w.note) : ''}
          </div>
        </div>
        <button type="button" class="btn-icon" data-del="${w.id}" title="Delete window">×</button>
      </div>
    `).join('');
  }

  maintList.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-del]');
    if (!btn) return;
    try {
      await api(`/api/maintenance/${btn.dataset.del}`, { method: 'DELETE' });
      toast('Maintenance window deleted', 'success');
      loadMaintWindows();
    } catch (err) {
      toast(err.message || 'Delete failed', 'error');
    }
  });

  maintForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const starts = maintForm.elements.starts_at.value;
    const ends   = maintForm.elements.ends_at.value;
    if (!starts || !ends) return;
    const payload = {
      server_id: maintForm.elements.server_id.value || null,
      starts_at: new Date(starts).toISOString(),
      ends_at:   new Date(ends).toISOString(),
      note:      maintForm.elements.note.value.trim(),
    };
    try {
      await api('/api/maintenance', { method: 'POST', body: payload });
      maintForm.reset();
      toast('Maintenance window added', 'success');
      loadMaintWindows();
    } catch (err) {
      toast(err.message || 'Save failed', 'error');
    }
  });

  const testSend   = $('#testSmtpSend');
  const testError  = $('#testSmtpError');
  const testStatus = $('#testSmtpStatus');

  function resetTestModal() {
    testForm.reset();
    testError.hidden = true;
    testStatus.hidden = true;
    testSend.disabled = false;
    testSend.textContent = 'Send';
  }

  $('#testSmtpBtn').addEventListener('click', () => { resetTestModal(); showModal(testModal); });
  $('#testSmtpClose').addEventListener('click', () => hideModal(testModal));
  $('#testSmtpCancel').addEventListener('click', () => hideModal(testModal));
  testModal.addEventListener('click', (e) => { if (e.target === testModal) hideModal(testModal); });

  testForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const to = testForm.elements.to.value.trim();
    // Immediate feedback: the send is synchronous and can block up to ~20s
    // while connecting to SMTP, so disable the button and show progress.
    testError.hidden = true;
    testStatus.hidden = false;
    testSend.disabled = true;
    testSend.textContent = 'Sending…';
    try {
      await api('/api/settings/test-smtp', { method: 'POST', body: { to } });
      toast('Test email sent — check your inbox', 'success', 5000);
      hideModal(testModal);
    } catch (err) {
      // Surface the real SMTP error inline (and keep it visible) so the
      // failure reason — wrong port, STARTTLS mismatch, auth rejected — is
      // readable instead of flashing past in a toast.
      testError.textContent = err.message || 'Send failed';
      testError.hidden = false;
    } finally {
      testStatus.hidden = true;
      testSend.disabled = false;
      testSend.textContent = 'Send';
    }
  });

  load();
  loadMaintServers().catch(() => {});
  loadMaintWindows().catch(() => {});
})();
