import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, session

from . import auth, db, selfmon
from .api import history as api_history
from .api import maintenance as api_maintenance
from .api import metrics as api_metrics
from .api import public as api_public
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
    app.register_blueprint(api_maintenance.bp)
    app.register_blueprint(api_metrics.bp)
    app.register_blueprint(api_public.bp)

    @app.get("/health")
    def health():
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return jsonify(status="error", db="error", error=str(exc)), 503
        sched = selfmon.heartbeat_status()
        selfmon.maybe_alert(sched)
        # Scheduler staleness is reported as payload but stays HTTP 200: a 503
        # would flip the web container unhealthy and deadlock cold starts once
        # scheduler/nginx depend_on web:service_healthy (the scheduler can't
        # have beaten before it is allowed to start).
        status = "degraded" if sched["stale"] else "ok"
        return jsonify(status=status, db="ok", scheduler=sched)

    @app.get("/status")
    def public_status_page():
        # Read-only LAN status page; unauthenticated by design, gated by the
        # settings toggle (default off). base.html hides the nav when
        # username is falsy.
        api_public.require_public_enabled()
        return render_template("status_public.html", page="status", username=None)

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
