import logging

from . import db

logger = logging.getLogger(__name__)


def prune_old_rows() -> dict:
    with db.cursor(commit=True) as cur:
        cur.execute("SELECT retention_days FROM app_settings WHERE id = 1")
        days = cur.fetchone()["retention_days"]

        cur.execute(
            "DELETE FROM check_results WHERE checked_at < now() - (%s || ' days')::interval",
            (days,),
        )
        check_deleted = cur.rowcount

        cur.execute(
            "DELETE FROM outage_events WHERE ended_at IS NOT NULL "
            "AND ended_at < now() - (%s || ' days')::interval",
            (days,),
        )
        outage_deleted = cur.rowcount

        cur.execute(
            "DELETE FROM email_outbox WHERE status IN ('sent','cancelled','failed') "
            "AND created_at < now() - interval '7 days'"
        )
        outbox_deleted = cur.rowcount

    logger.info(
        "retention prune: removed %d check_results, %d outage_events older than %d days, %d outbox rows",
        check_deleted,
        outage_deleted,
        days,
        outbox_deleted,
    )
    return {"check_results": check_deleted, "outage_events": outage_deleted,
            "email_outbox": outbox_deleted, "days": days}
