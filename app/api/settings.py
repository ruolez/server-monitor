from datetime import date, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

from .. import auth, crypto, db, mailer, reports

bp = Blueprint("api_settings", __name__, url_prefix="/api/settings")


def _public_settings(row: dict) -> dict:
    out = dict(row)
    has_password = bool(out.pop("smtp_password_encrypted", None))
    out["smtp_password_set"] = has_password
    # Flask's JSON provider can't serialize time, and renders date as an HTTP date.
    if isinstance(out.get("daily_report_time"), time):
        out["daily_report_time"] = out["daily_report_time"].strftime("%H:%M")
    if isinstance(out.get("daily_report_last_sent_on"), date):
        out["daily_report_last_sent_on"] = out["daily_report_last_sent_on"].isoformat()
    return out


@bp.get("")
@auth.login_required
def get_settings():
    with db.cursor() as cur:
        cur.execute("SELECT * FROM app_settings WHERE id = 1")
        row = cur.fetchone()
    return jsonify(_public_settings(row))


@bp.put("")
@auth.login_required
def update_settings():
    body = request.get_json(silent=True) or {}
    fields = {}

    for key in (
        "smtp_host",
        "smtp_username",
        "smtp_from_address",
        "smtp_from_name",
    ):
        if key in body:
            v = body[key]
            fields[key] = (v or "").strip() or None

    if "smtp_port" in body:
        try:
            port = int(body["smtp_port"])
            if not 1 <= port <= 65535:
                raise ValueError
            fields["smtp_port"] = port
        except (TypeError, ValueError):
            return jsonify(error="smtp_port must be 1-65535"), 400

    if "smtp_use_starttls" in body:
        fields["smtp_use_starttls"] = bool(body["smtp_use_starttls"])

    if "reminder_interval_minutes" in body:
        try:
            v = int(body["reminder_interval_minutes"])
            if v < 5:
                raise ValueError
            fields["reminder_interval_minutes"] = v
        except (TypeError, ValueError):
            return jsonify(error="reminder_interval_minutes must be >= 5"), 400

    if "retention_days" in body:
        try:
            v = int(body["retention_days"])
            if v < 7:
                raise ValueError
            fields["retention_days"] = v
        except (TypeError, ValueError):
            return jsonify(error="retention_days must be >= 7"), 400

    if "default_check_interval_seconds" in body:
        try:
            v = int(body["default_check_interval_seconds"])
            if v < 5:
                raise ValueError
            fields["default_check_interval_seconds"] = v
        except (TypeError, ValueError):
            return jsonify(error="default_check_interval_seconds must be >= 5"), 400

    if "daily_report_enabled" in body:
        fields["daily_report_enabled"] = bool(body["daily_report_enabled"])

    if "daily_report_time" in body:
        try:
            fields["daily_report_time"] = time.fromisoformat(str(body["daily_report_time"]))
        except ValueError:
            return jsonify(error="daily_report_time must be HH:MM"), 400

    if "daily_report_timezone" in body:
        tz_name = (body["daily_report_timezone"] or "").strip()
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return jsonify(error="daily_report_timezone must be a valid IANA timezone"), 400
        fields["daily_report_timezone"] = tz_name

    # Password is write-only. Only update if a new value is supplied.
    if body.get("smtp_password"):
        fields["smtp_password_encrypted"] = crypto.encrypt(body["smtp_password"])
    elif body.get("smtp_password_clear") is True:
        fields["smtp_password_encrypted"] = None

    if not fields:
        return jsonify(error="no fields to update"), 400

    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    fields["_id"] = 1
    with db.cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE app_settings SET {sets}, updated_at = now() WHERE id = %(_id)s RETURNING *",
            fields,
        )
        row = cur.fetchone()
    return jsonify(_public_settings(row))


@bp.post("/test-smtp")
@auth.login_required
def test_smtp():
    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip().lower()
    if not to:
        return jsonify(error="'to' is required"), 400
    try:
        mailer.send_test_email(to)
    except mailer.MailerError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(sent=True, to=to)


@bp.post("/send-daily-report")
@auth.login_required
def send_daily_report_now():
    """Manual trigger. Doesn't touch daily_report_last_sent_on, so the scheduled send still fires."""
    try:
        reports.send_daily_report()
    except mailer.MailerError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(sent=True)
