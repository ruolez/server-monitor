import logging
from datetime import datetime, timezone

from . import db, outbox

logger = logging.getLogger(__name__)


def _humanize_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60}m"


def in_maintenance(server_id: int) -> bool:
    """True when an active maintenance window covers this server (a window
    with server_id NULL covers all servers)."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM maintenance_windows
            WHERE (server_id IS NULL OR server_id = %s)
              AND starts_at <= now() AND ends_at > now()
            LIMIT 1
            """,
            (server_id,),
        )
        return cur.fetchone() is not None


def recipients_for(server_id: int) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT r.email FROM server_recipients sr
            JOIN recipients r ON r.id = sr.recipient_id
            WHERE sr.server_id = %s AND r.enabled
            """,
            (server_id,),
        )
        overrides = [r["email"] for r in cur.fetchall()]
        if overrides:
            return overrides
        cur.execute("SELECT email FROM recipients WHERE is_default AND enabled")
        return [r["email"] for r in cur.fetchall()]


def _send(server: dict, kind: str, subject: str, html: str, text: str) -> bool:
    """Enqueue an alert email; the scheduler's outbox drain delivers it with
    retry/backoff. The *_sent_at markers therefore mean 'queued at'. Returns
    False when there is nothing to enqueue (no recipients / maintenance).

    Central maintenance gate: every per-server kind is silenced during a
    window EXCEPT recovery, whose suppression follows the outage's own
    alerts_suppressed flag (an outage that alerted before the window must
    still close its loop with a RECOVERY email)."""
    if kind != "recovery" and in_maintenance(server["id"]):
        logger.info("suppressing %s alert for %s (maintenance window)", kind, server["name"])
        return False
    to = recipients_for(server["id"])
    if not to:
        logger.warning("no recipients for server %s — skipping alert", server["name"])
        return False
    outbox.enqueue(server["id"], kind, to, subject, html, text)
    return True


def _down_template(server: dict, started_at: datetime, error: str | None) -> tuple[str, str, str]:
    target = server["hostname"]
    if server["check_type"] == "tcp":
        target = f"{server['hostname']}:{server['tcp_port']}"
    subject = f"[DOWN] {server['name']}"
    text = (
        f"Server '{server['name']}' is DOWN.\n"
        f"Target: {target} ({server['check_type'].upper()})\n"
        f"Detected: {started_at.isoformat()}\n"
        f"Last error: {error or 'unknown'}\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#ef4444;margin:0 0 8px">Server is DOWN</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b></p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Target</td><td>{target}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Check</td><td>{server['check_type'].upper()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Detected</td><td>{started_at.isoformat()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Last error</td><td>{error or 'unknown'}</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def _recovery_template(server: dict, started_at: datetime, ended_at: datetime, duration: int) -> tuple[str, str, str]:
    subject = f"[UP] {server['name']} recovered"
    text = (
        f"Server '{server['name']}' is back UP.\n"
        f"Down at: {started_at.isoformat()}\n"
        f"Recovered: {ended_at.isoformat()}\n"
        f"Outage duration: {_humanize_seconds(duration)}\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#22c55e;margin:0 0 8px">Server recovered</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b></p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Down at</td><td>{started_at.isoformat()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Recovered</td><td>{ended_at.isoformat()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Duration</td><td>{_humanize_seconds(duration)}</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def _reminder_template(server: dict, started_at: datetime, now: datetime, count: int) -> tuple[str, str, str]:
    duration = int((now - started_at).total_seconds())
    subject = f"[STILL DOWN] {server['name']} ({_humanize_seconds(duration)})"
    text = (
        f"Server '{server['name']}' is still DOWN.\n"
        f"Down since: {started_at.isoformat()} ({_humanize_seconds(duration)} ago)\n"
        f"Reminder #{count}\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#f59e0b;margin:0 0 8px">Server is still DOWN</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b></p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Down since</td><td>{started_at.isoformat()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Duration</td><td>{_humanize_seconds(duration)}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Reminder</td><td>#{count}</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def _flapping_template(server: dict, count: int, window_min: int) -> tuple[str, str, str]:
    subject = f"[FLAPPING] {server['name']} ({count} outages in {window_min}m)"
    text = (
        f"Server '{server['name']}' is flapping: {count} outages in the last {window_min} minutes.\n"
        f"Per-transition DOWN/UP emails are paused until it stays stable for {window_min} minutes.\n"
        f"A summary will be sent when it stabilizes.\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#f59e0b;margin:0 0 8px">Server is flapping</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b> — {count} outages in the last {window_min} minutes.</p>
      <p style="margin:0;color:#7d8590">Per-transition DOWN/UP emails are paused until it stays stable
      for {window_min} minutes; a summary will follow when it stabilizes.</p>
    </div>
    """
    return subject, html, text


def _flap_clear_template(server: dict, since: datetime, outage_count: int,
                         downtime_seconds: int) -> tuple[str, str, str]:
    subject = f"[STABLE] {server['name']} stopped flapping"
    text = (
        f"Server '{server['name']}' has stabilized.\n"
        f"Flapping since: {since.isoformat()}\n"
        f"Outages during that period: {outage_count}\n"
        f"Total downtime: {_humanize_seconds(int(downtime_seconds))}\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#22c55e;margin:0 0 8px">Server stabilized</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b> stopped flapping.</p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Flapping since</td><td>{since.isoformat()}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Outages</td><td>{outage_count}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Total downtime</td><td>{_humanize_seconds(int(downtime_seconds))}</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def fire_flapping_alert(server: dict, count: int, window_min: int) -> None:
    subject, html, text = _flapping_template(server, count, window_min)
    _send(server, "flapping", subject, html, text)


def fire_flap_clear(server: dict, since: datetime, outage_count: int, downtime_seconds: int) -> None:
    subject, html, text = _flap_clear_template(server, since, outage_count, downtime_seconds)
    _send(server, "flap_clear", subject, html, text)


def _degraded_template(server: dict, latency_ms: int | None) -> tuple[str, str, str]:
    warn = server["latency_warn_ms"]
    subject = f"[DEGRADED] {server['name']} latency high"
    text = (
        f"Server '{server['name']}' is UP but responding slowly.\n"
        f"Latest latency: {latency_ms}ms (warning threshold: {warn}ms)\n"
        f"Threshold crossed on {server['latency_warn_checks']} consecutive check(s).\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#f59e0b;margin:0 0 8px">Latency degraded</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b> is UP but responding slowly.</p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Latest latency</td><td>{latency_ms} ms</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Warning threshold</td><td>{warn} ms</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Consecutive slow checks</td><td>{server['latency_warn_checks']}</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def _degraded_clear_template(server: dict, since: datetime, latency_ms: int | None) -> tuple[str, str, str]:
    duration = int((datetime.now(timezone.utc) - since).total_seconds())
    subject = f"[OK] {server['name']} latency back to normal"
    text = (
        f"Server '{server['name']}' latency is back under {server['latency_warn_ms']}ms.\n"
        f"Degraded since: {since.isoformat()} ({_humanize_seconds(duration)})\n"
        f"Latest latency: {latency_ms}ms\n"
    )
    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="color:#22c55e;margin:0 0 8px">Latency back to normal</h2>
      <p style="margin:0 0 8px"><b>{server['name']}</b></p>
      <table style="border-collapse:collapse;font-size:14px">
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Degraded for</td><td>{_humanize_seconds(duration)}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Latest latency</td><td>{latency_ms} ms</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Threshold</td><td>{server['latency_warn_ms']} ms</td></tr>
      </table>
    </div>
    """
    return subject, html, text


def fire_degraded_alert(server: dict, latency_ms: int | None) -> None:
    outbox.cancel_pending(server["id"], ("degraded_clear",))
    subject, html, text = _degraded_template(server, latency_ms)
    _send(server, "degraded", subject, html, text)


def fire_degraded_clear(server: dict, since: datetime, latency_ms: int | None) -> None:
    outbox.cancel_pending(server["id"], ("degraded",))
    subject, html, text = _degraded_clear_template(server, since, latency_ms)
    _send(server, "degraded_clear", subject, html, text)


def fire_down_alert(server: dict, outage_id: int, error: str | None) -> None:
    # A fresh DOWN supersedes any not-yet-delivered RECOVERY (and any pending
    # degradation chatter — the failure branch already cleared that state).
    outbox.cancel_pending(server["id"], ("recovery", "degraded", "degraded_clear"))
    now = datetime.now(timezone.utc)
    subject, html, text = _down_template(server, now, error)
    if _send(server, "down", subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE outage_events SET down_alert_sent_at = now() WHERE id = %s",
                (outage_id,),
            )


def fire_recovery_alert(server: dict, outage: dict) -> None:
    # Never deliver a stale DOWN/reminder after the server already recovered.
    outbox.cancel_pending(server["id"], ("down", "reminder"))
    started = outage["started_at"]
    ended = outage["ended_at"] or datetime.now(timezone.utc)
    duration = outage["duration_seconds"] or int((ended - started).total_seconds())
    subject, html, text = _recovery_template(server, started, ended, duration)
    if _send(server, "recovery", subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE outage_events SET recovery_alert_sent_at = now() WHERE id = %s",
                (outage["id"],),
            )


def fire_reminder(server: dict, outage: dict) -> None:
    now = datetime.now(timezone.utc)
    count = (outage["reminder_count"] or 0) + 1
    subject, html, text = _reminder_template(server, outage["started_at"], now, count)
    if _send(server, "reminder", subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE outage_events
                SET last_reminder_sent_at = now(), reminder_count = %s
                WHERE id = %s
                """,
                (count, outage["id"]),
            )
