from flask import Blueprint, jsonify

from .. import auth, db

bp = Blueprint("api_status", __name__, url_prefix="/api/status")


@bp.get("")
@auth.login_required
def get_status():
    """
    Aggregated payload tuned for the dashboard's 5-second poll.

    Includes the last 96 check results per server (used for the 24h sparkline
    when servers run on the default 15-minute granularity, and a useful
    rolling window otherwise).
    """
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, hostname, check_type, tcp_port, enabled,
                   current_status, last_checked_at, last_latency_ms,
                   last_status_change_at, interval_seconds, failure_threshold,
                   consecutive_failures
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
        if servers:
            ids = tuple(s["id"] for s in servers)
            cur.execute(
                """
                SELECT * FROM (
                  SELECT server_id, status, latency_ms, checked_at,
                         row_number() OVER (PARTITION BY server_id ORDER BY checked_at DESC) AS rn
                  FROM check_results
                  WHERE server_id = ANY(%s)
                ) ranked
                WHERE rn <= 96
                ORDER BY server_id, checked_at ASC
                """,
                (list(ids),),
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

    for s in servers:
        s["sparkline"] = sparklines.get(s["id"], [])
        s["down_since"] = open_outages.get(s["id"])

    return jsonify(servers=servers, summary=summary)
