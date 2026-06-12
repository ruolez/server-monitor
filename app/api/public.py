"""
Unauthenticated read-only status endpoints for the LAN status page.

Gated by app_settings.public_status_enabled (404 when off). Deliberately
minimal payloads: server names and colors only — no hostnames, ports, or
error messages.
"""
from flask import Blueprint, abort, jsonify, request

from .. import db
from .metrics import uptime_payload

bp = Blueprint("api_public", __name__, url_prefix="/api/public")


def require_public_enabled() -> None:
    with db.cursor() as cur:
        cur.execute("SELECT public_status_enabled FROM app_settings WHERE id = 1")
        row = cur.fetchone()
    if not row or not row["public_status_enabled"]:
        abort(404)


@bp.get("/status")
def public_status():
    require_public_enabled()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.current_status, s.last_checked_at,
                   o.started_at AS down_since
            FROM servers s
            LEFT JOIN outage_events o ON o.server_id = s.id AND o.ended_at IS NULL
            WHERE s.enabled
            ORDER BY s.name
            """
        )
        rows = cur.fetchall()
    return jsonify(
        [
            {
                "id": r["id"],
                "name": r["name"],
                "current_status": r["current_status"],
                "last_checked_at": r["last_checked_at"].isoformat() if r["last_checked_at"] else None,
                "down_since": r["down_since"].isoformat() if r["down_since"] else None,
            }
            for r in rows
        ]
    )


@bp.get("/uptime")
def public_uptime():
    require_public_enabled()
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 30
    if days not in (30, 90):
        days = 30
    payload = uptime_payload(days, enabled_only=True)
    payload.pop("timezone", None)
    return jsonify(payload)
