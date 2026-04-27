import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, session

from . import auth, db
from .api import history as api_history
from .api import recipients as api_recipients
from .api import servers as api_servers
from .api import settings as api_settings
from .api import status as api_status

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=os.environ["APP_SECRET_KEY"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,  # set True behind HTTPS
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    )

    if os.environ.get("RUN_MIGRATIONS", "1") == "1":
        db.init_schema()
        bootstrap_password = auth.ensure_bootstrap_admin()
        if bootstrap_password:
            logger.warning(
                "=" * 60
                + "\nBOOTSTRAP ADMIN CREATED\n  username: admin\n  password: %s\n"
                + "Save it now — change it from the UI after first login.\n"
                + "=" * 60,
                bootstrap_password,
            )

    app.register_blueprint(auth.bp)
    app.register_blueprint(api_servers.bp)
    app.register_blueprint(api_recipients.bp)
    app.register_blueprint(api_settings.bp)
    app.register_blueprint(api_status.bp)
    app.register_blueprint(api_history.bp)

    @app.get("/health")
    def health():
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
            return jsonify(status="ok")
        except Exception as exc:  # noqa: BLE001
            return jsonify(status="error", error=str(exc)), 503

    @app.get("/")
    @auth.login_required
    def dashboard():
        return render_template("dashboard.html", page="dashboard", username=session.get("username"))

    @app.get("/servers")
    @auth.login_required
    def servers_page():
        return render_template("servers.html", page="servers", username=session.get("username"))

    @app.get("/recipients")
    @auth.login_required
    def recipients_page():
        return render_template("recipients.html", page="recipients", username=session.get("username"))

    @app.get("/history")
    @auth.login_required
    def history_page():
        return render_template("history.html", page="history", username=session.get("username"))

    @app.get("/settings")
    @auth.login_required
    def settings_page():
        return render_template("settings.html", page="settings", username=session.get("username"))

    return app
