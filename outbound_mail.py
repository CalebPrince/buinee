"""Outbound email via plain SMTP - the one direction mailbox.py doesn't
cover (mailbox.py only ever reads). Stdlib only: smtplib + email.message.

Credentials are SMTP_* environment variables, not database-stored, so they
don't go through secretstore - consistent with how PROVIDER_KEYS and the
Paystack keys already work (server secrets live in .env/Passenger env, only
per-user tokens that get stored in the DB need encryption at rest).

Fail-open by design, unlike secretstore: a reminder or any other in-app
feature that merely *also* wants to email someone should keep working with
SMTP unconfigured. Callers must check is_configured()/why_unavailable()
themselves and decide what "SMTP isn't set up" means for them - this module
never raises just because nothing is configured, and never sends silently
if it isn't.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")


class MailSendError(Exception):
    """A user-facing (or log-facing) send failure - auth, connect, refuse."""


def why_unavailable(cfg: dict) -> str:
    """Empty string when usable, otherwise the reason, for logs/UI to show."""
    if not cfg.get("SMTP_HOST", "").strip():
        return "SMTP_HOST isn't set, so Buinee can't send email yet."
    if not cfg.get("SMTP_FROM", "").strip():
        return "SMTP_FROM isn't set, so outgoing mail has no sender address."
    try:
        int(cfg.get("SMTP_PORT", "587") or "587")
    except ValueError:
        return "SMTP_PORT isn't a number."
    return ""


def is_configured(cfg: dict) -> bool:
    return not why_unavailable(cfg)


def send(cfg: dict, to_addr: str, subject: str, body: str) -> None:
    """Raises MailSendError on any failure - callers decide whether that's
    fatal to them (see server.send_due_reminder for the fail-open example)."""
    reason = why_unavailable(cfg)
    if reason:
        raise MailSendError(reason)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["SMTP_FROM"].strip()
    msg["To"] = to_addr
    msg.set_content(body)

    host = cfg["SMTP_HOST"].strip()
    port = int(cfg.get("SMTP_PORT", "587") or "587")
    user = cfg.get("SMTP_USER", "").strip()
    password = cfg.get("SMTP_PASS", "")

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        with server:
            if user:
                server.login(user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailSendError(str(exc)) from exc
