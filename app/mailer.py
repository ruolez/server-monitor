import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable

from . import crypto, db

logger = logging.getLogger(__name__)


class MailerError(RuntimeError):
    pass


def _load_settings() -> dict:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT smtp_host, smtp_port, smtp_username, smtp_password_encrypted,
                   smtp_from_address, smtp_from_name, smtp_use_starttls
            FROM app_settings WHERE id = 1
            """
        )
        row = cur.fetchone()
    if not row or not row["smtp_host"]:
        raise MailerError("SMTP is not configured. Set it in Settings.")
    return row


def _build_message(to_addrs: Iterable[str], subject: str, html: str, text: str, settings: dict) -> EmailMessage:
    msg = EmailMessage()
    sender = settings.get("smtp_from_address") or settings["smtp_username"]
    sender_name = settings.get("smtp_from_name") or "Server Monitor"
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_email(to_addrs: list[str], subject: str, html: str, text: str) -> None:
    if not to_addrs:
        logger.warning("send_email called with no recipients (subject=%s)", subject)
        return

    settings = _load_settings()
    password = crypto.decrypt(settings.get("smtp_password_encrypted") or "")
    msg = _build_message(to_addrs, subject, html, text, settings)
    host = settings["smtp_host"]
    port = settings["smtp_port"] or 587

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if settings.get("smtp_use_starttls", True):
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if settings.get("smtp_username"):
                smtp.login(settings["smtp_username"], password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailerError(f"SMTP send failed: {exc}") from exc


def send_test_email(to_addr: str) -> None:
    html = """
    <p>Hello from <b>Server Monitor</b>.</p>
    <p>If you're reading this, your SMTP configuration is working.</p>
    """
    text = "Hello from Server Monitor. SMTP configuration is working."
    send_email([to_addr], "Server Monitor — SMTP test", html, text)
