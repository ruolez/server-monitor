"""
Self-monitoring of the scheduler container from the web container.

The scheduler writes scheduler_heartbeat_at to app_settings every 10s.
The web container reads it on /health (which Docker's healthcheck hits
every 30s, so staleness is noticed even with no browser open) and on the
dashboard poll. When the heartbeat goes stale the web container sends the
"[MONITOR] scheduler down" email directly via mailer — never through the
email outbox, because the outbox is drained by the scheduler, which is
the very thing that is dead.
"""
import logging
import os
import threading
from datetime import datetime, timezone

from . import db, mailer

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = int(os.environ.get("SCHEDULER_STALE_SECONDS", "90"))
REALERT_MINUTES = 30


def heartbeat_status() -> dict:
    with db.cursor() as cur:
        cur.execute("SELECT scheduler_heartbeat_at FROM app_settings WHERE id = 1")
        row = cur.fetchone()
    beat = row["scheduler_heartbeat_at"] if row else None
    if beat is None:
        # Fresh install: the scheduler has never started, don't cry wolf.
        return {"heartbeat_at": None, "age_seconds": None, "stale": False}
    age = int((datetime.now(timezone.utc) - beat).total_seconds())
    return {
        "heartbeat_at": beat.isoformat(),
        "age_seconds": age,
        "stale": age > STALE_AFTER_SECONDS,
    }


def _default_recipients() -> list[str]:
    with db.cursor() as cur:
        cur.execute("SELECT email FROM recipients WHERE is_default AND enabled")
        return [r["email"] for r in cur.fetchall()]


def _send_async(subject: str, html: str, text: str) -> None:
    def _run() -> None:
        to = _default_recipients()
        if not to:
            logger.error("scheduler alert not emailed: no default recipients configured")
            return
        try:
            mailer.send_email(to, subject, html, text)
            logger.warning("scheduler self-monitor email sent: %s", subject)
        except mailer.MailerError as exc:
            logger.error("scheduler self-monitor email failed: %s", exc)

    threading.Thread(target=_run, daemon=True, name="selfmon-mail").start()


def maybe_alert(status: dict) -> None:
    """Send at most one stale-alert per REALERT_MINUTES, plus one recovery
    email when the heartbeat returns. Race-safe across gunicorn workers via
    atomic UPDATE ... RETURNING claims. Must never raise into /health."""
    try:
        if status["stale"]:
            with db.cursor(commit=True) as cur:
                cur.execute(
                    """
                    UPDATE app_settings SET scheduler_alert_sent_at = now()
                    WHERE id = 1
                      AND (scheduler_alert_sent_at IS NULL
                           OR scheduler_alert_sent_at < now() - make_interval(mins => %s))
                    RETURNING id
                    """,
                    (REALERT_MINUTES,),
                )
                claimed = cur.fetchone() is not None
            if claimed:
                age_min = (status["age_seconds"] or 0) // 60
                subject = "[MONITOR] Scheduler is not running"
                text = (
                    "The Server Monitor scheduler container has stopped sending heartbeats.\n"
                    f"Last heartbeat: {status['heartbeat_at']} ({age_min}m ago)\n"
                    "Checks and alerts are NOT running until it recovers.\n"
                    "Remediation: docker compose -f /opt/server-monitor/docker-compose.yml restart scheduler\n"
                )
                html = f"""
                <div style="font-family:Inter,system-ui,sans-serif">
                  <h2 style="color:#ef4444;margin:0 0 8px">Scheduler is not running</h2>
                  <p style="margin:0 0 8px">The monitoring scheduler has stopped sending heartbeats.
                  <b>Checks and alerts are not running</b> until it recovers.</p>
                  <table style="border-collapse:collapse;font-size:14px">
                    <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Last heartbeat</td><td>{status['heartbeat_at']}</td></tr>
                    <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Stale for</td><td>{age_min}m</td></tr>
                    <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Remediation</td>
                        <td><code>docker compose -f /opt/server-monitor/docker-compose.yml restart scheduler</code></td></tr>
                  </table>
                </div>
                """
                _send_async(subject, html, text)
        else:
            with db.cursor(commit=True) as cur:
                cur.execute(
                    """
                    UPDATE app_settings SET scheduler_alert_sent_at = NULL
                    WHERE id = 1 AND scheduler_alert_sent_at IS NOT NULL
                    RETURNING id
                    """
                )
                claimed = cur.fetchone() is not None
            if claimed and status["heartbeat_at"] is not None:
                subject = "[MONITOR] Scheduler recovered"
                text = (
                    "The Server Monitor scheduler is sending heartbeats again.\n"
                    f"Latest heartbeat: {status['heartbeat_at']}\n"
                )
                html = f"""
                <div style="font-family:Inter,system-ui,sans-serif">
                  <h2 style="color:#22c55e;margin:0 0 8px">Scheduler recovered</h2>
                  <p style="margin:0 0 8px">Heartbeats have resumed; checks and alerts are running again.</p>
                  <table style="border-collapse:collapse;font-size:14px">
                    <tr><td style="padding:4px 12px 4px 0;color:#7d8590">Latest heartbeat</td><td>{status['heartbeat_at']}</td></tr>
                  </table>
                </div>
                """
                _send_async(subject, html, text)
    except Exception:  # noqa: BLE001
        logger.exception("scheduler self-monitor alert pass failed")
