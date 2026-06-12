"""
Durable email outbox.

Alert emails are enqueued here instead of being sent inline, then drained by
a scheduler job with exponential backoff. This makes alert state transitions
(down_alert_sent_at etc.) independent of SMTP availability: a failed send is
retried instead of silently lost.

Note: the web container's "check now" can enqueue, but only the scheduler
drains — worst case ~30s of added delivery delay.
"""
import logging

from . import db, mailer

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8  # 1+2+4+...+128 min ≈ 4.2h of retries before giving up
DRAIN_BATCH = 10


def enqueue(server_id: int | None, kind: str, recipients: list[str],
            subject: str, html: str, text: str) -> int:
    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO email_outbox (server_id, kind, recipients, subject, body_html, body_text)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (server_id, kind, recipients, subject, html, text),
        )
        return cur.fetchone()["id"]


def cancel_pending(server_id: int, kinds: tuple[str, ...]) -> int:
    """Cancel queued-but-unsent emails that a newer transition has made stale
    (e.g. never deliver a DOWN after the RECOVERY already went out)."""
    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE email_outbox SET status = 'cancelled'
            WHERE server_id = %s AND kind = ANY(%s) AND status = 'pending'
            """,
            (server_id, list(kinds)),
        )
        cancelled = cur.rowcount
    if cancelled:
        logger.info("cancelled %d stale pending email(s) for server id=%s", cancelled, server_id)
    return cancelled


def drain() -> int:
    """Send due pending emails. Returns the number delivered."""
    with db.cursor(commit=True) as cur:
        cur.execute(
            """
            SELECT id, recipients, subject, body_html, body_text, attempts
            FROM email_outbox
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY created_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (DRAIN_BATCH,),
        )
        rows = cur.fetchall()

    delivered = 0
    for row in rows:
        try:
            mailer.send_email(row["recipients"], row["subject"], row["body_html"], row["body_text"])
        except mailer.MailerError as exc:
            attempts = row["attempts"] + 1
            with db.cursor(commit=True) as cur:
                if attempts >= MAX_ATTEMPTS:
                    cur.execute(
                        """
                        UPDATE email_outbox
                        SET status = 'failed', attempts = %s, last_error = %s
                        WHERE id = %s
                        """,
                        (attempts, str(exc), row["id"]),
                    )
                    logger.error(
                        "email permanently failed after %d attempts (id=%s, subject=%s): %s",
                        attempts, row["id"], row["subject"], exc,
                    )
                else:
                    cur.execute(
                        """
                        UPDATE email_outbox
                        SET attempts = %s, last_error = %s,
                            next_attempt_at = now() + make_interval(mins => %s)
                        WHERE id = %s
                        """,
                        (attempts, str(exc), 2 ** (attempts - 1), row["id"]),
                    )
                    logger.warning(
                        "email send failed, retry #%d in %dm (id=%s): %s",
                        attempts, 2 ** (attempts - 1), row["id"], exc,
                    )
        else:
            with db.cursor(commit=True) as cur:
                cur.execute(
                    "UPDATE email_outbox SET status = 'sent', sent_at = now() WHERE id = %s",
                    (row["id"],),
                )
            delivered += 1
    return delivered
