import secrets
from functools import wraps

import bcrypt
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from . import db

bp = Blueprint("auth", __name__)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def ensure_bootstrap_admin() -> str | None:
    """Create a default admin if none exists. Returns the generated password if created."""
    with db.cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM admins LIMIT 1")
        if cur.fetchone():
            return None
        password = secrets.token_urlsafe(16)
        cur.execute(
            "INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
            ("admin", hash_password(password)),
        )
        return password


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM admins WHERE username = %s", (username,)
            )
            row = cur.fetchone()
        if row and verify_password(password, row["password_hash"]):
            session.clear()
            session["admin_id"] = row["id"]
            session["username"] = username
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.post("/api/auth/change-password")
@login_required
def change_password():
    body = request.get_json(silent=True) or {}
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""
    if len(new) < 8:
        return jsonify(error="New password must be at least 8 characters."), 400
    with db.cursor() as cur:
        cur.execute(
            "SELECT password_hash FROM admins WHERE id = %s", (session["admin_id"],)
        )
        row = cur.fetchone()
    if not row or not verify_password(current, row["password_hash"]):
        return jsonify(error="Current password is incorrect."), 400
    with db.cursor(commit=True) as cur:
        cur.execute(
            "UPDATE admins SET password_hash = %s WHERE id = %s",
            (hash_password(new), session["admin_id"]),
        )
    return jsonify(changed=True)
