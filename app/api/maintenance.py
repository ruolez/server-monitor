from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import auth, db

bp = Blueprint("api_maintenance", __name__, url_prefix="/api/maintenance")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@bp.get("")
@auth.login_required
def list_windows():
    """Upcoming, active, and recently-ended (last 24h) windows."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT m.id, m.server_id, m.starts_at, m.ends_at, m.note, m.created_at,
                   s.name AS server_name,
                   (m.starts_at <= now() AND m.ends_at > now()) AS active
            FROM maintenance_windows m
            LEFT JOIN servers s ON s.id = m.server_id
            WHERE m.ends_at > now() - interval '1 day'
            ORDER BY m.starts_at
            """
        )
        rows = cur.fetchall()
    return jsonify(rows)


@bp.post("")
@auth.login_required
def create_window():
    data = request.get_json(silent=True) or {}
    starts_at = _parse_ts(data.get("starts_at"))
    ends_at = _parse_ts(data.get("ends_at"))
    if not starts_at or not ends_at:
        return jsonify(error="starts_at and ends_at must be ISO-8601 timestamps"), 400
    if ends_at <= starts_at:
        return jsonify(error="ends_at must be after starts_at"), 400

    server_id = data.get("server_id")
    if server_id not in (None, ""):
        try:
            server_id = int(server_id)
        except (TypeError, ValueError):
            return jsonify(error="server_id must be an integer or null"), 400
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM servers WHERE id = %s", (server_id,))
            if not cur.fetchone():
                return jsonify(error="server not found"), 404
    else:
        server_id = None

    note = (data.get("note") or "").strip()[:255] or None

    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO maintenance_windows (server_id, starts_at, ends_at, note)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (server_id, starts_at, ends_at, note),
        )
        window_id = cur.fetchone()["id"]
    return jsonify(id=window_id), 201


@bp.delete("/<int:window_id>")
@auth.login_required
def delete_window(window_id: int):
    with db.cursor(commit=True) as cur:
        cur.execute("DELETE FROM maintenance_windows WHERE id = %s", (window_id,))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(deleted=window_id)
