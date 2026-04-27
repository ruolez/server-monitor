from flask import Blueprint, jsonify, request

from .. import auth, db

bp = Blueprint("api_history", __name__, url_prefix="/api/history")


@bp.get("/outages")
@auth.login_required
def list_outages():
    server_id = request.args.get("server_id", type=int)
    limit = min(request.args.get("limit", 200, type=int), 500)
    with db.cursor() as cur:
        if server_id:
            cur.execute(
                """
                SELECT o.*, s.name AS server_name FROM outage_events o
                JOIN servers s ON s.id = o.server_id
                WHERE o.server_id = %s
                ORDER BY o.started_at DESC LIMIT %s
                """,
                (server_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT o.*, s.name AS server_name FROM outage_events o
                JOIN servers s ON s.id = o.server_id
                ORDER BY o.started_at DESC LIMIT %s
                """,
                (limit,),
            )
        return jsonify(cur.fetchall())


@bp.get("/checks")
@auth.login_required
def list_checks():
    server_id = request.args.get("server_id", type=int)
    limit = min(request.args.get("limit", 200, type=int), 1000)
    with db.cursor() as cur:
        if server_id:
            cur.execute(
                """
                SELECT cr.*, s.name AS server_name FROM check_results cr
                JOIN servers s ON s.id = cr.server_id
                WHERE cr.server_id = %s
                ORDER BY cr.checked_at DESC LIMIT %s
                """,
                (server_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT cr.*, s.name AS server_name FROM check_results cr
                JOIN servers s ON s.id = cr.server_id
                ORDER BY cr.checked_at DESC LIMIT %s
                """,
                (limit,),
            )
        return jsonify(cur.fetchall())
