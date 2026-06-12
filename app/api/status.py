from flask import Blueprint, jsonify, request

from .. import auth, db, selfmon

bp = Blueprint("api_status", __name__, url_prefix="/api/status")


@bp.get("")
@auth.login_required
def get_status():
    """
    Aggregated payload tuned for the dashboard poll.

    The dashboard polls a light variant (?sparklines=0) every few seconds and
    only fetches the full payload — the last 96 check results per server —
    every ~30s or when a server's status changes.
    """
    include_sparklines = request.args.get("sparklines", "1") != "0"
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, hostname, check_type, tcp_port, enabled,
                   current_status, last_checked_at, last_latency_ms,
                   last_status_change_at, interval_seconds, failure_threshold,
                   consecutive_failures, is_flapping, degraded_since,
                   latency_warn_ms
            FROM servers ORDER BY name
            """
        )
        servers = cur.fetchall()

        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE current_status = 'up')      AS up_count,
              COUNT(*) FILTER (WHERE current_status = 'down')    AS down_count,
              COUNT(*) FILTER (WHERE current_status = 'unknown') AS unknown_count,
              COUNT(*) AS total
            FROM servers WHERE enabled
            """
        )
        summary = cur.fetchone()

        sparklines: dict[int, list] = {}
        if servers and include_sparklines:
            ids = [s["id"] for s in servers]
            # LATERAL keeps this a 96-row index scan per server instead of
            # ranking every check_results row for the polled servers.
            cur.execute(
                """
                SELECT s.id AS server_id, cr.status, cr.latency_ms, cr.checked_at
                FROM servers s
                CROSS JOIN LATERAL (
                    SELECT status, latency_ms, checked_at
                    FROM check_results
                    WHERE server_id = s.id
                    ORDER BY checked_at DESC
                    LIMIT 96
                ) cr
                WHERE s.id = ANY(%s)
                ORDER BY s.id, cr.checked_at ASC
                """,
                (ids,),
            )
            for row in cur.fetchall():
                sparklines.setdefault(row["server_id"], []).append(
                    {
                        "status": row["status"],
                        "latency_ms": row["latency_ms"],
                        "checked_at": row["checked_at"].isoformat(),
                    }
                )

        cur.execute(
            """
            SELECT server_id, started_at FROM outage_events WHERE ended_at IS NULL
            """
        )
        open_outages = {r["server_id"]: r["started_at"].isoformat() for r in cur.fetchall()}

        cur.execute(
            """
            SELECT server_id FROM maintenance_windows
            WHERE starts_at <= now() AND ends_at > now()
            """
        )
        maint_rows = cur.fetchall()
        maint_all = any(r["server_id"] is None for r in maint_rows)
        maint_ids = {r["server_id"] for r in maint_rows if r["server_id"] is not None}

    for s in servers:
        if include_sparklines:
            s["sparkline"] = sparklines.get(s["id"], [])
        s["down_since"] = open_outages.get(s["id"])
        s["in_maintenance"] = maint_all or s["id"] in maint_ids

    # Report-only here; the alerting side of self-monitoring lives in /health.
    return jsonify(
        servers=servers,
        summary=summary,
        sparklines_included=include_sparklines,
        scheduler=selfmon.heartbeat_status(),
    )
