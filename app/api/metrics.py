"""
Time-bucketed metrics for the server detail charts and uptime history.

Aggregation is done on the fly with date_bin over check_results — at this
deployment's scale (a 30d range at 60s checks is ~43k rows behind the
(server_id, checked_at DESC) index) a rollup table isn't warranted.
"""
from flask import Blueprint, jsonify, request

from .. import auth, db

bp = Blueprint("api_metrics", __name__, url_prefix="/api")

# range -> (span interval, bucket interval, bucket seconds)
RANGES = {
    "1h":  ("1 hour",   "1 minute",   60),
    "24h": ("24 hours", "5 minutes",  300),
    "7d":  ("7 days",   "30 minutes", 1800),
    "30d": ("30 days",  "2 hours",    7200),
}

UPTIME_DAYS = (30, 90)


@bp.get("/servers/<int:server_id>/metrics")
@auth.login_required
def server_metrics(server_id: int):
    rng = request.args.get("range", "24h")
    if rng not in RANGES:
        return jsonify(error=f"range must be one of {', '.join(RANGES)}"), 400
    span, bucket, bucket_seconds = RANGES[rng]

    with db.cursor() as cur:
        cur.execute("SELECT id, name FROM servers WHERE id = %s", (server_id,))
        server = cur.fetchone()
        if not server:
            return jsonify(error="not found"), 404
        cur.execute(
            """
            SELECT date_bin(%(bucket)s::interval, checked_at, TIMESTAMPTZ 'epoch') AS bucket,
                   count(*)                                                   AS checks,
                   count(*) FILTER (WHERE status = 'up')                      AS up_checks,
                   round(avg(latency_ms) FILTER (WHERE status = 'up'))::int   AS avg_ms,
                   min(latency_ms) FILTER (WHERE status = 'up')               AS min_ms,
                   max(latency_ms) FILTER (WHERE status = 'up')               AS max_ms
            FROM check_results
            WHERE server_id = %(id)s
              AND checked_at >= now() - %(span)s::interval
            GROUP BY bucket
            ORDER BY bucket
            """,
            {"id": server_id, "span": span, "bucket": bucket},
        )
        rows = cur.fetchall()

    points = [
        {
            "t": int(r["bucket"].timestamp()),
            "avg_ms": r["avg_ms"],
            "min_ms": r["min_ms"],
            "max_ms": r["max_ms"],
            "checks": r["checks"],
            "up_checks": r["up_checks"],
        }
        for r in rows
    ]
    total = sum(r["checks"] for r in rows)
    up = sum(r["up_checks"] for r in rows)
    lat = [r["avg_ms"] for r in rows if r["avg_ms"] is not None]
    return jsonify(
        range=rng,
        bucket_seconds=bucket_seconds,
        uptime_pct=round(100.0 * up / total, 2) if total else None,
        checks=total,
        avg_ms=round(sum(lat) / len(lat)) if lat else None,
        min_ms=min((r["min_ms"] for r in rows if r["min_ms"] is not None), default=None),
        max_ms=max((r["max_ms"] for r in rows if r["max_ms"] is not None), default=None),
        points=points,
    )


def uptime_payload(days: int, server_id: int | None = None,
                   enabled_only: bool = False) -> dict:
    """Daily uptime % per server, computed from check_results (ground truth:
    sub-threshold blips and scheduler-down gaps all show up here, unlike
    outage_events). Day boundaries follow the daily-report timezone."""
    with db.cursor() as cur:
        cur.execute("SELECT daily_report_timezone FROM app_settings WHERE id = 1")
        tz = cur.fetchone()["daily_report_timezone"] or "UTC"

        where_server = "AND server_id = %(server_id)s" if server_id else ""
        cur.execute(
            f"""
            SELECT server_id,
                   (checked_at AT TIME ZONE %(tz)s)::date AS day,
                   count(*)                               AS checks,
                   count(*) FILTER (WHERE status = 'up')  AS up_checks
            FROM check_results
            WHERE checked_at >= ((now() AT TIME ZONE %(tz)s)::date - %(days)s * INTERVAL '1 day')
                               AT TIME ZONE %(tz)s
              {where_server}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            {"tz": tz, "days": days - 1, "server_id": server_id},
        )
        rows = cur.fetchall()

        where_enabled = "WHERE enabled" if enabled_only else ""
        cur.execute(f"SELECT id, name FROM servers {where_enabled} ORDER BY name")
        servers = cur.fetchall()

    by_server: dict[int, dict[str, dict]] = {}
    for r in rows:
        by_server.setdefault(r["server_id"], {})[r["day"].isoformat()] = r

    out = []
    for s in servers:
        if server_id and s["id"] != server_id:
            continue
        days_map = by_server.get(s["id"], {})
        day_list = []
        total = up = 0
        for iso_day, r in sorted(days_map.items()):
            total += r["checks"]
            up += r["up_checks"]
            day_list.append(
                {
                    "day": iso_day,
                    "uptime_pct": round(100.0 * r["up_checks"] / r["checks"], 2),
                    "checks": r["checks"],
                }
            )
        out.append(
            {
                "server_id": s["id"],
                "name": s["name"],
                "uptime_pct": round(100.0 * up / total, 2) if total else None,
                "days": day_list,
            }
        )
    return {"days": days, "timezone": tz, "servers": out}


@bp.get("/uptime")
@auth.login_required
def uptime():
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 0
    if days not in UPTIME_DAYS:
        return jsonify(error=f"days must be one of {UPTIME_DAYS}"), 400
    server_id = request.args.get("server_id", type=int)
    return jsonify(uptime_payload(days, server_id))
