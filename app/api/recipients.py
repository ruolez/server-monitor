import re

from flask import Blueprint, jsonify, request

from .. import auth, db

bp = Blueprint("api_recipients", __name__, url_prefix="/api/recipients")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate(data: dict, partial: bool = False) -> tuple[dict | None, str | None]:
    out: dict = {}
    if not partial or "email" in data:
        email = (data.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            return None, "invalid email"
        out["email"] = email
    if "name" in data:
        out["name"] = (data.get("name") or "").strip() or None
    if "is_default" in data:
        out["is_default"] = bool(data["is_default"])
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    return out, None


@bp.get("")
@auth.login_required
def list_recipients():
    with db.cursor() as cur:
        cur.execute("SELECT * FROM recipients ORDER BY email")
        return jsonify(cur.fetchall())


@bp.post("")
@auth.login_required
def create_recipient():
    data, err = _validate(request.get_json(silent=True) or {}, partial=False)
    if err:
        return jsonify(error=err), 400
    with db.cursor(commit=True) as cur:
        try:
            cur.execute(
                """
                INSERT INTO recipients (email, name, is_default, enabled)
                VALUES (%s, %s, %s, %s) RETURNING *
                """,
                (
                    data["email"],
                    data.get("name"),
                    data.get("is_default", False),
                    data.get("enabled", True),
                ),
            )
            row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            return jsonify(error=f"insert failed: {exc}"), 400
    return jsonify(row), 201


@bp.put("/<int:recipient_id>")
@auth.login_required
def update_recipient(recipient_id: int):
    data, err = _validate(request.get_json(silent=True) or {}, partial=True)
    if err:
        return jsonify(error=err), 400
    if not data:
        return jsonify(error="no fields"), 400
    with db.cursor(commit=True) as cur:
        sets = ", ".join(f"{k} = %({k})s" for k in data)
        data["id"] = recipient_id
        cur.execute(f"UPDATE recipients SET {sets} WHERE id = %(id)s RETURNING *", data)
        row = cur.fetchone()
    if not row:
        return jsonify(error="not found"), 404
    return jsonify(row)


@bp.delete("/<int:recipient_id>")
@auth.login_required
def delete_recipient(recipient_id: int):
    with db.cursor(commit=True) as cur:
        cur.execute("DELETE FROM recipients WHERE id = %s", (recipient_id,))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(deleted=recipient_id)
