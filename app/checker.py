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
                    last_latency_ms = %(lat)s,
                    consecutive_failures = 0,
                    consecutive_successes = consecutive_successes + 1,
                    consecutive_slow = CASE
                        WHEN latency_warn_ms IS NOT NULL AND %(lat)s >= latency_warn_ms
                        THEN consecutive_slow + 1 ELSE 0 END,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {"lat": result.latency_ms, "id": server["id"]},
            )
        else:
            # A down transition supersedes degradation — clear it silently.
            cur.execute(
                """
                UPDATE servers
                SET last_checked_at = now(),
                    last_latency_ms = NULL,
                    consecutive_failures = consecutive_failures + 1,
                    consecutive_successes = 0,
                    consecutive_slow = 0,
                    degraded_since = NULL,
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

    if (
        result.success
        and fresh["current_status"] == "down"
        and fresh["consecutive_successes"] >= fresh["recovery_threshold"]
    ):
        # Recovery: close open outage, mark UP, send recovery email.
        # recovery_threshold mirrors failure_threshold (default 1 = legacy
        # behavior); ended_at is stamped at confirmation, overstating the
        # outage by up to (recovery_threshold - 1) intervals — acceptable.
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
        # Flap-suppressed/maintenance outages skip the recovery email; while
        # flapping even pre-flap outages stay quiet (the [STABLE] summary
        # closes the loop instead).
        if outage and not outage["alerts_suppressed"] and not fresh["is_flapping"]:
            alerting.fire_recovery_alert(fresh, outage)
        transition = "recovered"

    elif result.success and fresh["current_status"] == "unknown":
        # Initial 'unknown' → 'up'. No alert, no recovery_threshold gate.
        # (Must NOT match 'down': a down server below recovery_threshold
        # stays down until enough consecutive successes accumulate.)
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
        # During a maintenance window (or while flapping) the outage is still
        # recorded (history and uptime stats stay truthful) but flagged so no
        # email fires; the maintenance-exit catch-up in the minute tick
        # un-suppresses it if the server is still down when the window ends.
        suppressed = fresh["is_flapping"] or alerting.in_maintenance(server["id"])
        with db.cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO outage_events (server_id, alerts_suppressed)
                VALUES (%s, %s) RETURNING id
                """,
                (server["id"], suppressed),
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

        if not fresh["is_flapping"] and _flap_started(fresh, outage_id):
            pass  # single [FLAPPING] email already sent; per-transition alerts paused
        elif not suppressed:
            alerting.fire_down_alert(fresh, outage_id, result.error)
        transition = "down"

    if result.success:
        _track_degradation(fresh, result.latency_ms)

    return {
        "status": status,
        "latency_ms": result.latency_ms,
        "error": result.error,
        "transition": transition,
    }


def _track_degradation(fresh: dict, latency_ms: int | None) -> None:
    """Latency degradation is orthogonal to up/down: a degraded server is UP
    but persistently slower than its latency_warn_ms threshold. `fresh` was
    fetched after the counter update, so consecutive_slow is current."""
    warn_ms = fresh["latency_warn_ms"]
    degraded_since = fresh["degraded_since"]

    if warn_ms is None:
        # Threshold removed while degraded — clear state silently.
        if degraded_since is not None:
            with db.cursor(commit=True) as cur:
                cur.execute(
                    "UPDATE servers SET degraded_since = NULL, consecutive_slow = 0 WHERE id = %s",
                    (fresh["id"],),
                )
        return

    if (
        degraded_since is None
        and fresh["consecutive_slow"] >= fresh["latency_warn_checks"]
    ):
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE servers SET degraded_since = now() WHERE id = %s",
                (fresh["id"],),
            )
        if not fresh["is_flapping"]:  # flapping latency data is noise
            alerting.fire_degraded_alert(fresh, latency_ms)
    elif degraded_since is not None and fresh["consecutive_slow"] == 0:
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE servers SET degraded_since = NULL WHERE id = %s",
                (fresh["id"],),
            )
        if not fresh["is_flapping"]:
            alerting.fire_degraded_clear(fresh, degraded_since, latency_ms)


def _flap_started(server: dict, outage_id: int) -> bool:
    """Evaluated at each confirmed down transition: if this server has had
    >= flap_threshold confirmed outages inside flap_window_minutes, enter
    flapping mode — suppress per-transition alerts and send one [FLAPPING]
    email. Derived from outage_events, so no rolling counters to maintain."""
    with db.cursor() as cur:
        cur.execute("SELECT flap_window_minutes, flap_threshold FROM app_settings WHERE id = 1")
        cfg = cur.fetchone()
        cur.execute(
            """
            SELECT count(*) AS n FROM outage_events
            WHERE server_id = %s
              AND started_at >= now() - (%s || ' minutes')::interval
            """,
            (server["id"], cfg["flap_window_minutes"]),
        )
        count = cur.fetchone()["n"]  # includes the row just inserted

    if count < cfg["flap_threshold"]:
        return False

    with db.cursor(commit=True) as cur:
        cur.execute(
            "UPDATE servers SET is_flapping = TRUE, flapping_since = now() WHERE id = %s",
            (server["id"],),
        )
        cur.execute(
            "UPDATE outage_events SET alerts_suppressed = TRUE WHERE id = %s",
            (outage_id,),
        )
    from . import outbox  # local import to avoid cycle at module load
    outbox.cancel_pending(server["id"], ("down", "recovery", "reminder"))
    alerting.fire_flapping_alert(server, count, cfg["flap_window_minutes"])
    logger.warning(
        "server %s is flapping (%d outages in %dm) — per-transition alerts paused",
        server["name"], count, cfg["flap_window_minutes"],
    )
    return True


def flap_clear_pass() -> int:
    """Minute tick: a flapping server whose latest outage is older than the
    flap window and that is currently up has stabilized — clear the flag and
    send one [STABLE] summary."""
    cleared = 0
    with db.cursor() as cur:
        cur.execute("SELECT flap_window_minutes FROM app_settings WHERE id = 1")
        window_min = cur.fetchone()["flap_window_minutes"]
        cur.execute(
            """
            SELECT s.* FROM servers s
            WHERE s.is_flapping
              AND s.current_status = 'up'
              AND NOT EXISTS (
                  SELECT 1 FROM outage_events o
                  WHERE o.server_id = s.id
                    AND o.started_at >= now() - (%s || ' minutes')::interval
              )
            """,
            (window_min,),
        )
        servers = cur.fetchall()

    for server in servers:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS outage_count,
                       COALESCE(SUM(duration_seconds), 0) AS downtime_seconds
                FROM outage_events
                WHERE server_id = %s AND started_at >= %s
                """,
                (server["id"], server["flapping_since"]),
            )
            stats = cur.fetchone()
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE servers SET is_flapping = FALSE, flapping_since = NULL WHERE id = %s",
                (server["id"],),
            )
        alerting.fire_flap_clear(
            server, server["flapping_since"], stats["outage_count"], stats["downtime_seconds"]
        )
        cleared += 1
    return cleared


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


def maintenance_exit_pass() -> int:
    """Un-suppress open outages whose maintenance window has ended and fire
    the deferred DOWN alert — otherwise a server that went down during a
    window and stayed down would never alert."""
    fired = 0
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT o.id AS outage_id, o.server_id
            FROM outage_events o
            JOIN servers s ON s.id = o.server_id
            WHERE o.ended_at IS NULL AND o.alerts_suppressed
              AND NOT s.is_flapping
            """
        )
        rows = cur.fetchall()

    for row in rows:
        if alerting.in_maintenance(row["server_id"]):
            continue
        with db.cursor(commit=True) as cur:
            cur.execute(
                "UPDATE outage_events SET alerts_suppressed = FALSE WHERE id = %s",
                (row["outage_id"],),
            )
            cur.execute("SELECT * FROM servers WHERE id = %s", (row["server_id"],))
            server = cur.fetchone()
        if server:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT error_message FROM check_results
                    WHERE server_id = %s AND status = 'down'
                    ORDER BY checked_at DESC LIMIT 1
                    """,
                    (row["server_id"],),
                )
                last = cur.fetchone()
            alerting.fire_down_alert(server, row["outage_id"], last["error_message"] if last else None)
            fired += 1
    return fired


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
              AND NOT o.alerts_suppressed
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
