import logging
from datetime import datetime, timezone

from . import db, mailer

logger = logging.getLogger(__name__)


def _humanize_seconds(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60}m"


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


def _send(server: dict, subject: str, html: str, text: str) -> bool:
    to = recipients_for(server["id"])
    if not to:
        logger.warning("no recipients for server %s — skipping alert", server["name"])
        return False
    try:
        mailer.send_email(to, subject, html, text)
        return True
    except mailer.MailerError as exc:
        logger.error("alert email failed for server %s: %s", server["name"], exc)
        return False


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


def fire_down_alert(server: dict, outage_id: int, error: str | None) -> None:
    now = datetime.now(timezone.utc)
    subject, html, text = _down_template(server, now, error)
    if _send(server, subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE outage_events SET down_alert_sent_at = now() WHERE id = %s",
                (outage_id,),
            )


def fire_recovery_alert(server: dict, outage: dict) -> None:
    started = outage["started_at"]
    ended = outage["ended_at"] or datetime.now(timezone.utc)
    duration = outage["duration_seconds"] or int((ended - started).total_seconds())
    subject, html, text = _recovery_template(server, started, ended, duration)
    if _send(server, subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE outage_events SET recovery_alert_sent_at = now() WHERE id = %s",
                (outage["id"],),
            )


def fire_reminder(server: dict, outage: dict) -> None:
    now = datetime.now(timezone.utc)
    count = (outage["reminder_count"] or 0) + 1
    subject, html, text = _reminder_template(server, outage["started_at"], now, count)
    if _send(server, subject, html, text):
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE outage_events
                SET last_reminder_sent_at = now(), reminder_count = %s
                WHERE id = %s
                """,
                (count, outage["id"]),
            )
