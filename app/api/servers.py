from flask import Blueprint, jsonify, request

from .. import auth, checker, db

bp = Blueprint("api_servers", __name__, url_prefix="/api/servers")


def _validate_payload(data: dict, partial: bool = False) -> tuple[dict | None, str | None]:
    out: dict = {}
    if not partial or "name" in data:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 120:
            return None, "name is required (1-120 chars)"
        out["name"] = name
    if not partial or "hostname" in data:
        hostname = (data.get("hostname") or "").strip()
        if not hostname or len(hostname) > 255:
            return None, "hostname is required (1-255 chars)"
        out["hostname"] = hostname
    if not partial or "check_type" in data:
        check_type = (data.get("check_type") or "").strip().lower()
        if check_type not in ("icmp", "tcp"):
            return None, "check_type must be 'icmp' or 'tcp'"
        out["check_type"] = check_type
    if "tcp_port" in data and data["tcp_port"] not in (None, ""):
        try:
            port = int(data["tcp_port"])
        except (TypeError, ValueError):
            return None, "tcp_port must be an integer"
        if not 1 <= port <= 65535:
            return None, "tcp_port must be 1-65535"
        out["tcp_port"] = port
    elif not partial:
        out["tcp_port"] = None

    for field, lo in (
        ("interval_seconds", 5),
        ("timeout_seconds", 1),
        ("failure_threshold", 1),
    ):
        if field in data:
            try:
                v = int(data[field])
            except (TypeError, ValueError):
                return None, f"{field} must be an integer"
            if v < lo:
                return None, f"{field} must be >= {lo}"
            out[field] = v

    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])

    if (out.get("check_type") == "tcp" and out.get("tcp_port") is None) and not partial:
        return None, "tcp_port is required when check_type='tcp'"

    return out, None


def _server_with_recipients(server_id: int) -> dict | None:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM servers WHERE id = %s", (server_id,))
        s = cur.fetchone()
        if not s:
            return None
        cur.execute(
            "SELECT recipient_id FROM server_recipients WHERE server_id = %s",
            (server_id,),
        )
        s["override_recipient_ids"] = [r["recipient_id"] for r in cur.fetchall()]
    return s


@bp.get("")
@auth.login_required
def list_servers():
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT s.*,
                   (SELECT count(*) FROM server_recipients sr WHERE sr.server_id = s.id) AS override_count
            FROM servers s ORDER BY s.name
            """
        )
        rows = cur.fetchall()
    return jsonify(rows)


@bp.post("")
@auth.login_required
def create_server():
    data, err = _validate_payload(request.get_json(silent=True) or {}, partial=False)
    if err:
        return jsonify(error=err), 400
    overrides = (request.get_json(silent=True) or {}).get("override_recipient_ids") or []

    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO servers (name, hostname, check_type, tcp_port,
                                 interval_seconds, timeout_seconds, failure_threshold, enabled)
            VALUES (%(name)s, %(hostname)s, %(check_type)s, %(tcp_port)s,
                    %(interval_seconds)s, %(timeout_seconds)s, %(failure_threshold)s, %(enabled)s)
            RETURNING id
            """,
            {
                "interval_seconds": 60,
                "timeout_seconds": 5,
                "failure_threshold": 3,
                "enabled": True,
                **data,
            },
        )
        server_id = cur.fetchone()["id"]
        for rid in overrides:
            cur.execute(
                "INSERT INTO server_recipients (server_id, recipient_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (server_id, int(rid)),
            )
    return jsonify(_server_with_recipients(server_id)), 201


@bp.get("/<int:server_id>")
@auth.login_required
def get_server(server_id: int):
    server = _server_with_recipients(server_id)
    if not server:
        return jsonify(error="not found"), 404
    return jsonify(server)


@bp.put("/<int:server_id>")
@auth.login_required
def update_server(server_id: int):
    body = request.get_json(silent=True) or {}
    data, err = _validate_payload(body, partial=True)
    if err:
        return jsonify(error=err), 400

    if not data and "override_recipient_ids" not in body:
        return jsonify(error="no fields to update"), 400

    with db.cursor(commit=True) as cur:
        if data:
            cur.execute("SELECT 1 FROM servers WHERE id = %s", (server_id,))
            if not cur.fetchone():
                return jsonify(error="not found"), 404
            sets = ", ".join(f"{k} = %({k})s" for k in data)
            data["id"] = server_id
            cur.execute(
                f"UPDATE servers SET {sets}, updated_at = now() WHERE id = %(id)s",
                data,
            )
        if "override_recipient_ids" in body:
            cur.execute("DELETE FROM server_recipients WHERE server_id = %s", (server_id,))
            for rid in body["override_recipient_ids"] or []:
                cur.execute(
                    "INSERT INTO server_recipients (server_id, recipient_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (server_id, int(rid)),
                )
    return jsonify(_server_with_recipients(server_id))


@bp.delete("/<int:server_id>")
@auth.login_required
def delete_server(server_id: int):
    with db.cursor(commit=True) as cur:
        cur.execute("DELETE FROM servers WHERE id = %s", (server_id,))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(deleted=server_id)


@bp.post("/<int:server_id>/check-now")
@auth.login_required
def check_now(server_id: int):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM servers WHERE id = %s", (server_id,))
        server = cur.fetchone()
    if not server:
        return jsonify(error="not found"), 404
    return jsonify(checker.process_server(server))
