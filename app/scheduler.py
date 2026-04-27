"""
Standalone scheduler entrypoint.

Runs as a sidecar container so APScheduler isn't duplicated across
gunicorn workers. Connects to the same Postgres DB.
"""
import logging
import os
import signal
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from . import checker, db, retention

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scheduler")


def run_due_checks() -> None:
    try:
        due = checker.fetch_due_servers()
    except Exception:  # noqa: BLE001
        logger.exception("failed to fetch due servers")
        return
    for server in due:
        try:
            result = checker.process_server(server)
            logger.info(
                "checked %s: status=%s latency=%s transition=%s",
                server["name"],
                result["status"],
                result["latency_ms"],
                result["transition"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("check failed for server id=%s", server["id"])


def run_reminders() -> None:
    try:
        sent = checker.reminder_pass()
        if sent:
            logger.info("sent %d outage reminder(s)", sent)
    except Exception:  # noqa: BLE001
        logger.exception("reminder pass failed")


def run_retention() -> None:
    try:
        retention.prune_old_rows()
    except Exception:  # noqa: BLE001
        logger.exception("retention prune failed")


def _wait_for_db(max_seconds: int = 60) -> None:
    """Block until Postgres is reachable; the web container also runs migrations."""
    start = time.monotonic()
    while time.monotonic() - start < max_seconds:
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1 FROM app_settings WHERE id = 1")
                if cur.fetchone():
                    return
        except Exception as exc:  # noqa: BLE001
            logger.info("waiting for db: %s", exc)
        time.sleep(2)
    raise RuntimeError("database not ready after %ds" % max_seconds)


def main() -> None:
    _wait_for_db()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_due_checks,  "interval", seconds=2,  id="due_checks", max_instances=1, coalesce=True)
    scheduler.add_job(run_reminders,   "interval", minutes=1,  id="reminders", max_instances=1, coalesce=True)
    scheduler.add_job(run_retention,   "cron", hour=3, minute=0, id="retention")

    def _shutdown(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("scheduler starting")
    scheduler.start()


if __name__ == "__main__":
    main()
