"""
Daily health report: per-server 24h summary emailed to default recipients.

Triggered by a minute-tick scheduler job (daily_report_pass) so the send
time/timezone can change at runtime without rescheduling, and a manual
"send now" API endpoint (send_daily_report).
"""
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, mailer
from .alerting import _humanize_seconds

logger = logging.getLogger(__name__)

STATUS_COLORS = {"up": "#22c55e", "down": "#ef4444", "unknown": "#7d8590"}


def _default_recipients() -> list[str]:
    with db.cursor() as cur:
        cur.execute("SELECT email FROM recipients WHERE is_default AND enabled")
        return [r["email"] for r in cur.fetchall()]


def _collect_stats() -> list[dict]:
    """Per enabled server: current status plus 24h uptime %, latency, and outage totals."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.hostname, s.check_type, s.tcp_port, s.current_status,
                   COALESCE(cr.total_checks, 0)     AS total_checks,
                   COALESCE(cr.up_checks, 0)        AS up_checks,
                   cr.avg_latency_ms,
                   COALESCE(oe.outage_count, 0)     AS outage_count,
                   COALESCE(oe.downtime_seconds, 0) AS downtime_seconds
            FROM servers s
            LEFT JOIN (
                SELECT server_id,
                       COUNT(*)                                            AS total_checks,
                       COUNT(*) FILTER (WHERE status = 'up')               AS up_checks,
                       ROUND(AVG(latency_ms) FILTER (WHERE status = 'up')) AS avg_latency_ms
                FROM check_results
                WHERE checked_at >= now() - interval '24 hours'
                GROUP BY server_id
            ) cr ON cr.server_id = s.id
            LEFT JOIN (
                SELECT server_id,
                       COUNT(*) AS outage_count,
                       SUM(EXTRACT(EPOCH FROM
                           COALESCE(ended_at, now())
                           - GREATEST(started_at, now() - interval '24 hours')
                       ))::bigint AS downtime_seconds
                FROM outage_events
                WHERE COALESCE(ended_at, now()) >= now() - interval '24 hours'
                GROUP BY server_id
            ) oe ON oe.server_id = s.id
            WHERE s.enabled
            ORDER BY s.name
            """
        )
        return cur.fetchall()


def _target(row: dict) -> str:
    if row["check_type"] == "tcp":
        return f"{row['hostname']}:{row['tcp_port']}"
    return row["hostname"]


def _uptime_pct(row: dict) -> str:
    if not row["total_checks"]:
        return "—"
    return f"{100.0 * row['up_checks'] / row['total_checks']:.2f}%"


def _report_template(stats: list[dict], generated_at: datetime) -> tuple[str, str, str]:
    up = sum(1 for r in stats if r["current_status"] == "up")
    down = sum(1 for r in stats if r["current_status"] == "down")
    unknown = len(stats) - up - down
    subject = f"[REPORT] Server Monitor daily — {up} up, {down} down"

    lines = [
        f"Daily health report — {generated_at.strftime('%Y-%m-%d %H:%M %Z')}",
        f"{up} up, {down} down, {unknown} unknown ({len(stats)} monitored). Stats cover the last 24 hours.",
        "",
    ]
    for r in stats:
        latency = f"{r['avg_latency_ms']}ms avg" if r["avg_latency_ms"] is not None else "no latency data"
        downtime = (
            f"{r['outage_count']} outage(s), {_humanize_seconds(int(r['downtime_seconds']))} down"
            if r["outage_count"]
            else "no outages"
        )
        lines.append(
            f"- {r['name']} ({_target(r)}, {r['check_type'].upper()}): "
            f"{r['current_status'].upper()}, uptime {_uptime_pct(r)}, {latency}, {downtime}"
        )
    text = "\n".join(lines) + "\n"

    rows_html = ""
    for r in stats:
        status = r["current_status"]
        color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
        latency = f"{r['avg_latency_ms']} ms" if r["avg_latency_ms"] is not None else "—"
        downtime = _humanize_seconds(int(r["downtime_seconds"])) if r["outage_count"] else "—"
        rows_html += f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb"><b>{html.escape(r['name'])}</b><br>
              <span style="color:#7d8590;font-size:12px">{html.escape(_target(r))} · {r['check_type'].upper()}</span></td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;color:{color};font-weight:600">{status.upper()}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{_uptime_pct(r)}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{latency}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{r['outage_count'] or '—'}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb">{downtime}</td>
        </tr>"""

    empty_html = ""
    if not stats:
        empty_html = '<p style="color:#7d8590">No enabled servers are being monitored.</p>'

    html_body = f"""
    <div style="font-family:Inter,system-ui,sans-serif">
      <h2 style="margin:0 0 4px">Daily health report</h2>
      <p style="margin:0 0 4px;color:#7d8590">{generated_at.strftime('%Y-%m-%d %H:%M %Z')} · stats cover the last 24 hours</p>
      <p style="margin:0 0 12px;font-size:15px">
        <span style="color:{STATUS_COLORS['up']};font-weight:600">{up} up</span> ·
        <span style="color:{STATUS_COLORS['down']};font-weight:600">{down} down</span> ·
        <span style="color:{STATUS_COLORS['unknown']};font-weight:600">{unknown} unknown</span>
      </p>
      {empty_html}
      <table style="border-collapse:collapse;font-size:14px">
        <tr>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Server</th>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Status</th>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Uptime 24h</th>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Avg latency</th>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Outages</th>
          <th style="padding:6px 12px;text-align:left;color:#7d8590;border-bottom:2px solid #d0d7de">Downtime</th>
        </tr>
        {rows_html}
      </table>
    </div>
    """
    return subject, html_body, text


def _report_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.error("invalid daily_report_timezone %r — falling back to UTC", name)
        return ZoneInfo("UTC")


def send_daily_report() -> None:
    """Build and send the report immediately. Raises mailer.MailerError on failure."""
    to = _default_recipients()
    if not to:
        raise mailer.MailerError("No default recipients configured. Add one in Recipients.")
    with db.cursor() as cur:
        cur.execute("SELECT daily_report_timezone FROM app_settings WHERE id = 1")
        tz = _report_timezone(cur.fetchone()["daily_report_timezone"])
    stats = _collect_stats()
    subject, html_body, text = _report_template(stats, datetime.now(timezone.utc).astimezone(tz))
    mailer.send_email(to, subject, html_body, text)
    logger.info("daily report sent to %d recipient(s): %d server(s)", len(to), len(stats))


def daily_report_pass() -> bool:
    """Minute-tick gate: send once per local day at/after the configured time."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT daily_report_enabled, daily_report_time,
                   daily_report_timezone, daily_report_last_sent_on
            FROM app_settings WHERE id = 1
            """
        )
        row = cur.fetchone()
    if not row or not row["daily_report_enabled"]:
        return False

    now_local = datetime.now(timezone.utc).astimezone(_report_timezone(row["daily_report_timezone"]))
    if now_local.time() < row["daily_report_time"]:
        return False
    if row["daily_report_last_sent_on"] == now_local.date():
        return False

    try:
        send_daily_report()
    except mailer.MailerError as exc:
        logger.error("daily report send failed (will retry next minute): %s", exc)
        return False

    # Mark only after a successful send, same pattern as alerting.fire_*.
    with db.cursor(commit=True) as cur:
        cur.execute(
            "UPDATE app_settings SET daily_report_last_sent_on = %s WHERE id = 1",
            (now_local.date(),),
        )
    return True
