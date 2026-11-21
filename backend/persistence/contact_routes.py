"""Public contact form: deliver to a single configured operator mailbox via
the existing authenticated Brevo SMTP connection. Never expose an SMTP relay.
No contact submissions are persisted in the quiz database.
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from persistence.account_email_routes import mail_configured
from persistence.account_rate_limit import limit_email_action
from persistence.auth import validate_origin
from persistence.database import get_db

router = APIRouter(prefix="/api/contact", tags=["Public contact"])
_EMAIL_RE = re.compile(r"^[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+$")


class ContactMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    message: str = Field(min_length=1, max_length=2000)
    website: str = Field(default="", max_length=200)  # hidden bot-trap field


def _plain_address(raw: str) -> str | None:
    value = raw.strip()
    _name, parsed = parseaddr(value)
    if (parsed != value or not _EMAIL_RE.fullmatch(value)
            or any(c in value for c in "\r\n")):
        return None
    return value


def send_contact_email(payload: ContactMessage) -> None:
    """The visitor can choose Reply-To, but never the destination or From."""
    recipient = _plain_address(os.environ.get("CONTACT_RECIPIENT_EMAIL", ""))
    sender = os.environ.get("AUTH_FROM_EMAIL", "").strip()
    if not recipient or not mail_configured():
        raise HTTPException(status_code=503, detail="Contact form unavailable")
    reply_to = _plain_address(payload.email)
    if not reply_to:
        raise HTTPException(status_code=422, detail="Invalid email address")
    name, message = payload.name.strip(), payload.message.strip()
    if not name or not message or any(ord(c) < 32 and c not in "\t" for c in name):
        raise HTTPException(status_code=422, detail="Invalid contact message")

    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Reply-To"] = reply_to
    email["Subject"] = "AI Quiz Game — Website contact"
    email.set_content("Sender: " + name + "\nEmail: " + reply_to +
                      "\n\nMessage:\n" + message + "\n")

    try:
        port = int(os.environ.get("AUTH_SMTP_PORT", "587"))
        if port == 465:
            client = smtplib.SMTP_SSL(os.environ["AUTH_SMTP_HOST"], port, timeout=10)
        elif port in (587, 2525):
            client = smtplib.SMTP(os.environ["AUTH_SMTP_HOST"], port, timeout=10)
        else:
            raise HTTPException(status_code=503, detail="Contact form unavailable")
        with client as smtp:
            if port in (587, 2525):
                smtp.starttls(context=ssl.create_default_context())
            if os.environ.get("AUTH_SMTP_USERNAME"):
                smtp.login(os.environ["AUTH_SMTP_USERNAME"], os.environ["AUTH_SMTP_PASSWORD"])
            smtp.send_message(email)
    except (OSError, smtplib.SMTPException):
        # Do not leak SMTP hostname, credentials or the submitted message.
        raise HTTPException(status_code=503, detail="Contact form unavailable") from None


@router.post("")
def contact(payload: ContactMessage, request: Request,
            db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "public_contact", 4)
    # Ignore suspicious automated forms without sending anything.
    if payload.website.strip():
        return {"sent": True}
    send_contact_email(payload)
    return {"sent": True}
