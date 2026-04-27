"""
Per-server check execution + state machine.

Used by the scheduler container's tick loop and by the web container's
"check now" endpoint.
"""
import logging

from . import alerting, db
from .checks import CheckResult
from .checks.icmp import check_icmp
from .checks.tcp import check_tcp

logger = logging.getLogger(__name__)


def run_check(server: dict) -> CheckResult:
    if server["check_type"] == "icmp":
        return check_icmp(server["hostname"], server["timeout_seconds"])
    if server["check_type"] == "tcp":
        port = server["tcp_port"] or 0
        return check_tcp(server["hostname"], port, server["timeout_seconds"])
    return CheckResult(success=False, error=f"unknown check_type {server['check_type']}")


def process_server(server: dict) -> dict:
    """
    Execute a check, persist the result, transition state, and fire alerts.

    Returns a dict describing what happened (used by the manual 'check now' API).
    """
    result = run_check(server)
    status = "up" if result.success else "down"

    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO check_results (server_id, status, latency_ms, error_message)
            VALUES (%s, %s, %s, %s)
            """,
            (server["id"], status, result.latency_ms, result.error),
        )

        if result.success:
            cur.execute(
                """
                UPDATE servers
                SET last_checked_at = now(),
                    last_latency_ms = %s,
                    consecutive_failures = 0,
                    updated_at = now()
                WHERE id = %s
                """,
                (result.latency_ms, server["id"]),
            )
        else:
            cur.execute(
                """
                UPDATE servers
                SET last_checked_at = now(),
                    last_latency_ms = NULL,
                    consecutive_failures = consecutive_failures + 1,
                    updated_at = now()
                WHERE id = %s
                """,
                (server["id"],),
            )

        cur.execute(
            "SELECT * FROM servers WHERE id = %s",
            (server["id"],),
        )
        fresh = cur.fetchone()

    transition = None

    if result.success and fresh["current_status"] == "down":
        # Recovery: close open outage, mark UP, send recovery email.
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE outage_events
                SET ended_at = now(),
                    duration_seconds = EXTRACT(EPOCH FROM (now() - started_at))::int
                WHERE server_id = %s AND ended_at IS NULL
                RETURNING *
                """,
                (server["id"],),
            )
            outage = cur.fetchone()
            cur.execute(
                """
                UPDATE servers
                SET current_status = 'up', last_status_change_at = now()
                WHERE id = %s
                """,
                (server["id"],),
            )
        if outage:
            alerting.fire_recovery_alert(fresh, outage)
        transition = "recovered"

    elif result.success and fresh["current_status"] != "up":
        # Initial 'unknown' → 'up'. No alert.
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE servers
                SET current_status = 'up', last_status_change_at = now()
                WHERE id = %s
                """,
                (server["id"],),
            )
        transition = "up"

    elif (
        not result.success
        and fresh["current_status"] != "down"
        and fresh["consecutive_failures"] >= fresh["failure_threshold"]
    ):
        # Down transition: open outage, mark DOWN, send down email.
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO outage_events (server_id) VALUES (%s) RETURNING id
                """,
                (server["id"],),
            )
            outage_id = cur.fetchone()["id"]
            cur.execute(
                """
                UPDATE servers
                SET current_status = 'down', last_status_change_at = now()
                WHERE id = %s
                """,
                (server["id"],),
            )
        alerting.fire_down_alert(fresh, outage_id, result.error)
        transition = "down"

    return {
        "status": status,
        "latency_ms": result.latency_ms,
        "error": result.error,
        "transition": transition,
    }


def fetch_due_servers() -> list[dict]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM servers
            WHERE enabled
              AND (last_checked_at IS NULL
                   OR last_checked_at + (interval_seconds || ' seconds')::interval <= now())
            ORDER BY COALESCE(last_checked_at, '1970-01-01'::timestamptz) ASC
            LIMIT 50
            """
        )
        return cur.fetchall()


def reminder_pass() -> int:
    """Send reminders for ongoing outages whose reminder interval has elapsed."""
    sent = 0
    with db.cursor() as cur:
        cur.execute("SELECT reminder_interval_minutes FROM app_settings WHERE id = 1")
        interval_min = cur.fetchone()["reminder_interval_minutes"]
        cur.execute(
            """
            SELECT o.*, s.* , o.id AS id, s.id AS server_id
            FROM outage_events o
            JOIN servers s ON s.id = o.server_id
            WHERE o.ended_at IS NULL
              AND o.down_alert_sent_at IS NOT NULL
              AND (
                   o.last_reminder_sent_at IS NULL
                   AND o.down_alert_sent_at + (%s || ' minutes')::interval <= now()
                OR o.last_reminder_sent_at IS NOT NULL
                   AND o.last_reminder_sent_at + (%s || ' minutes')::interval <= now()
              )
            """,
            (interval_min, interval_min),
        )
        rows = cur.fetchall()

    for row in rows:
        # Re-fetch separately to keep server vs outage dicts clean.
        with db.cursor() as cur:
            cur.execute("SELECT * FROM servers WHERE id = %s", (row["server_id"],))
            server = cur.fetchone()
            cur.execute("SELECT * FROM outage_events WHERE id = %s", (row["id"],))
            outage = cur.fetchone()
        if server and outage:
            alerting.fire_reminder(server, outage)
            sent += 1
    return sent
