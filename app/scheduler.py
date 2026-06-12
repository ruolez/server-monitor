"""
Standalone scheduler entrypoint.

Runs as a sidecar container so APScheduler isn't duplicated across
gunicorn workers. Connects to the same Postgres DB.
"""
import logging
import os
import pathlib
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.blocking import BlockingScheduler

from . import checker, db, outbox, reports, retention

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("scheduler")


# Checks run concurrently so unreachable hosts (each blocking for its full
# timeout) can't starve the rest of the sweep and push checks past their
# interval. Sized below the DB pool (max 8 connections).
_check_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="check")

# Heartbeat: the file mtime is what the container healthcheck reads (alive
# even if the DB is down — a restart wouldn't fix a DB outage); the DB row is
# the cross-container signal the web container alerts on.
HEARTBEAT_FILE = "/tmp/scheduler-heartbeat"
WATCHDOG_LIMIT_SECONDS = 120
_last_beat = time.monotonic()


def run_heartbeat() -> None:
    global _last_beat
    pathlib.Path(HEARTBEAT_FILE).touch()
    _last_beat = time.monotonic()
    try:
        with db.cursor(commit=True) as cur:
            cur.execute("UPDATE app_settings SET scheduler_heartbeat_at = now() WHERE id = 1")
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat DB write failed: %s", exc)


def _start_watchdog() -> None:
    def _watch() -> None:
        while True:
            time.sleep(15)
            if time.monotonic() - _last_beat > WATCHDOG_LIMIT_SECONDS:
                logger.critical("scheduler loop wedged (>%ds without a heartbeat), exiting for restart",
                                WATCHDOG_LIMIT_SECONDS)
                os._exit(1)

    threading.Thread(target=_watch, daemon=True, name="watchdog").start()


def _checked(server: dict) -> None:
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


def run_due_checks() -> None:
    try:
        due = checker.fetch_due_servers()
    except Exception:  # noqa: BLE001
        logger.exception("failed to fetch due servers")
        return
    if not due:
        return
    # Wait for the batch so max_instances=1/coalesce still bound concurrency.
    futures = [_check_pool.submit(_checked, server) for server in due]
    for f in futures:
        f.result()


def run_reminders() -> None:
    try:
        cleared = checker.flap_clear_pass()
        if cleared:
            logger.info("%d server(s) stabilized after flapping", cleared)
    except Exception:  # noqa: BLE001
        logger.exception("flap clear pass failed")
    try:
        fired = checker.maintenance_exit_pass()
        if fired:
            logger.info("fired %d deferred down alert(s) after maintenance", fired)
    except Exception:  # noqa: BLE001
        logger.exception("maintenance exit pass failed")
    try:
        sent = checker.reminder_pass()
        if sent:
            logger.info("sent %d outage reminder(s)", sent)
    except Exception:  # noqa: BLE001
        logger.exception("reminder pass failed")


def run_daily_report() -> None:
    try:
        if reports.daily_report_pass():
            logger.info("daily health report sent")
    except Exception:  # noqa: BLE001
        logger.exception("daily report pass failed")


def run_outbox_drain() -> None:
    try:
        sent = outbox.drain()
        if sent:
            logger.info("delivered %d outbox email(s)", sent)
    except Exception:  # noqa: BLE001
        logger.exception("outbox drain failed")


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
    scheduler.add_job(run_daily_report, "interval", minutes=1, id="daily_report", max_instances=1, coalesce=True)
    scheduler.add_job(run_retention,   "cron", hour=3, minute=0, id="retention")
    scheduler.add_job(run_heartbeat,   "interval", seconds=10, id="heartbeat", max_instances=1, coalesce=True)
    scheduler.add_job(run_outbox_drain, "interval", seconds=30, id="outbox_drain", max_instances=1, coalesce=True)

    def _shutdown(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    run_heartbeat()  # beat once before the first 10s tick so healthchecks pass promptly
    _start_watchdog()
    logger.info("scheduler starting")
    scheduler.start()
    _check_pool.shutdown(wait=True, cancel_futures=True)
    logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
