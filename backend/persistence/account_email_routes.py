"""Email-first account endpoints, maintained alongside legacy username auth.

Strictly opt-in during rollout: existing users and sessions retain their UUIDs.
"""
from __future__ import annotations

import os
import ssl
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence.account_email import canonical_email, create_one_time_token, token_digest, token_expiry
from persistence.account_rate_limit import limit_email_action
from persistence.auth import (HASHER, authenticated_user, aware,
                              start_session, utc_now, validate_origin)
from persistence.database import get_db
from persistence.models import AccountEmailToken, AuthIdentity, AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["Accounts"])
GENERIC_RESET = {"message": "If this email is registered and verified, a reset link will be sent."}


class EmailRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)


class EmailLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str


class EmailInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str


class ReplaceUnverifiedEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    current_password: str = Field(min_length=1, max_length=256)


class TokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=256)


class ResetPassword(TokenInput):
    new_password: str = Field(min_length=8, max_length=256)


class DisplayNameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=80)


def normalized_name(raw: str) -> str:
    name = raw.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise HTTPException(status_code=422, detail="Invalid display name")
    return name


def mail_configured() -> bool:
    return bool(os.environ.get("AUTH_SMTP_HOST") and os.environ.get("AUTH_FROM_EMAIL")
                and os.environ.get("AUTH_PUBLIC_URL"))


def send_account_email(recipient: str, token: str, *, purpose: str):
    """SMTP over TLS, no token logging, secrets kept solely in environment."""
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    base = os.environ["AUTH_PUBLIC_URL"].strip().rstrip("/")
    from urllib.parse import urlsplit, quote
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'})) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=503, detail="Invalid public email URL")
    # URL fragments are not sent to the web server, proxy logs, or referrer headers.
    link = base + ("/#verify_email=" if purpose == "verify_email" else "/#reset_password=") + quote(token)
    msg = EmailMessage()
    msg["From"] = os.environ["AUTH_FROM_EMAIL"].strip()
    msg["To"] = recipient
    msg["Subject"] = "AI Quiz Game — Verify your email" if purpose == "verify_email" else "AI Quiz Game — Reset your password"
    msg.set_content("Open this one-time link within 30 minutes:\n\n" + link +
                    "\n\nIf you did not request this, you can ignore this message.")
    try:
        port = int(os.environ.get("AUTH_SMTP_PORT", "587"))
    except ValueError:
        raise HTTPException(status_code=503, detail="Invalid SMTP port") from None
    if port not in (465, 587, 2525):
        raise HTTPException(status_code=503, detail="Invalid SMTP port")
    if os.environ.get("AUTH_SMTP_USERNAME") and not os.environ.get("AUTH_SMTP_PASSWORD"):
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    try:
        if port == 465:
            client = smtplib.SMTP_SSL(os.environ["AUTH_SMTP_HOST"], port, timeout=10)
        else:
            client = smtplib.SMTP(os.environ["AUTH_SMTP_HOST"], port, timeout=10)
        with client as smtp:
            if port in (587, 2525):
                smtp.starttls(context=ssl.create_default_context())
            if os.environ.get("AUTH_SMTP_USERNAME"):
                smtp.login(os.environ["AUTH_SMTP_USERNAME"], os.environ["AUTH_SMTP_PASSWORD"])
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(status_code=503, detail="Email delivery unavailable") from None


def new_token(db: Session, user: User, purpose: str):
    token, digest = create_one_time_token()
    db.add(AccountEmailToken(id=uuid4(), user_id=user.id, email=user.email,
                             purpose=purpose, token_hash=digest,
                             expires_at=token_expiry()))
    return token


def throttle(db: Session, user: User, purpose: str) -> bool:
    count = db.scalar(select(func.count()).select_from(AccountEmailToken).where(
        AccountEmailToken.user_id == user.id,
        AccountEmailToken.purpose == purpose,
        AccountEmailToken.created_at >= utc_now() - timedelta(hours=1)))
    return count < 3


def account_response(user: User):
    return {"id": str(user.id), "username": user.username, "display_name": user.display_name,
            "email": user.email, "email_verified": user.email_verified_at is not None,
            "preferred_language": user.preferred_language, "ui_language": user.ui_language,
            "theme_key": user.theme_key}


@router.post("/email/register", status_code=201)
def register(payload: EmailRegister, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "register", 8)
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    try:
        email = canonical_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email") from None
    name = normalized_name(payload.display_name)
    user_id = uuid4()
    legacy = "u" + user_id.hex[:29]
    user = User(id=user_id, username=legacy, username_key=legacy,
                display_name=name, email=email)
    db.add(user)
    db.add(AuthIdentity(user_id=user_id, provider="password", provider_subject=legacy,
                        password_hash=HASHER.hash(payload.password)))
    token = new_token(db, user, "verify_email")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    send_account_email(email, token, purpose="verify_email")
    start_session(db, user, response)
    return account_response(user)


@router.post("/email/login")
def login(payload: EmailLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "login", 50)
    try:
        email = canonical_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid credentials") from None
    user = db.scalar(select(User).where(User.email == email))
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.user_id == user.id, AuthIdentity.provider == "password"
    )) if user else None
    if identity is None or user.disabled_at is not None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if identity.locked_until and aware(identity.locked_until) > utc_now():
        raise HTTPException(status_code=429, detail="Please try again later")
    from argon2 import exceptions
    try:
        HASHER.verify(identity.password_hash, payload.password)
    except (exceptions.VerifyMismatchError, exceptions.VerificationError):
        identity.failed_logins += 1
        if identity.failed_logins >= 5:
            identity.locked_until = utc_now() + timedelta(minutes=15)
            identity.failed_logins = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials") from None
    identity.failed_logins = 0
    identity.locked_until = None
    if HASHER.check_needs_rehash(identity.password_hash):
        identity.password_hash = HASHER.hash(payload.password)
    db.commit()
    start_session(db, user, response)
    return account_response(user)


@router.post("/email/claim")
def claim(payload: EmailInput, request: Request, user: User = Depends(authenticated_user),
          db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "claim", 12)
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    if user.email:
        raise HTTPException(status_code=409, detail="Email already assigned")
    if not throttle(db, user, "verify_email"):
        raise HTTPException(status_code=429, detail="Please try again later")
    try:
        email = canonical_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email") from None
    account = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if account.email:
        raise HTTPException(status_code=409, detail="Email already assigned")
    account.email = email
    token = new_token(db, account, "verify_email")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    send_account_email(email, token, purpose="verify_email")
    return {"message": "Verification email requested"}


@router.post("/email/resend")
def resend(request: Request, user: User = Depends(authenticated_user),
           db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "resend", 12)
    if not user.email or user.email_verified_at:
        raise HTTPException(status_code=409, detail="Email verification unavailable")
    if not throttle(db, user, "verify_email"):
        raise HTTPException(status_code=429, detail="Please try again later")
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    token = new_token(db, user, "verify_email")
    db.commit()
    send_account_email(user.email, token, purpose="verify_email")
    return {"message": "Verification email requested"}


@router.post("/email/verify")
def verify(payload: TokenInput, request: Request, db: Session = Depends(get_db)):
    validate_origin(request)
    digest = token_digest(payload.token)
    candidate = db.scalar(select(AccountEmailToken).where(
        AccountEmailToken.token_hash == digest,
        AccountEmailToken.purpose == "verify_email"))
    if not candidate:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    # Account lock first, then token lock, to serialize email corrections.
    user = db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    row = db.scalar(select(AccountEmailToken).where(
        AccountEmailToken.token_hash == digest,
        AccountEmailToken.purpose == "verify_email").with_for_update())
    if (not user or not row or row.used_at or aware(row.expires_at) <= utc_now()
            or user.email != row.email):
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    user.email_verified_at = utc_now()
    row.used_at = utc_now()
    db.commit()
    return {"message": "Email verified"}


@router.post("/email/replace-unverified")
def replace_unverified(payload: ReplaceUnverifiedEmail, request: Request,
                       user: User = Depends(authenticated_user),
                       db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "replace-unverified", 12)
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    try:
        email = canonical_email(payload.email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email") from None
    account = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not account or account.email_verified_at is not None or not account.email:
        raise HTTPException(status_code=409, detail="Cannot change a verified email here")
    if account.email == email:
        raise HTTPException(status_code=409, detail="Email is unchanged")
    if not throttle(db, account, "verify_email"):
        raise HTTPException(status_code=429, detail="Please try again later")
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.user_id == account.id,
        AuthIdentity.provider == "password").with_for_update())
    from argon2 import exceptions
    if not identity:
        raise HTTPException(status_code=409, detail="Password login not configured")
    if identity.locked_until and aware(identity.locked_until) > utc_now():
        raise HTTPException(status_code=429, detail="Please try again later")
    try:
        HASHER.verify(identity.password_hash, payload.current_password)
    except (exceptions.VerifyMismatchError, exceptions.VerificationError):
        identity.failed_logins += 1
        if identity.failed_logins >= 5:
            identity.locked_until = utc_now() + timedelta(minutes=15)
            identity.failed_logins = 0
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials") from None
    identity.failed_logins = 0
    identity.locked_until = None
    account.email = email
    db.execute(update(AccountEmailToken).where(
        AccountEmailToken.user_id == account.id,
        AccountEmailToken.purpose == "verify_email",
        AccountEmailToken.used_at.is_(None),
    ).values(used_at=utc_now()))
    token = new_token(db, account, "verify_email")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    send_account_email(email, token, purpose="verify_email")
    return account_response(account)


@router.post("/password/forgot")
def forgot(payload: EmailInput, request: Request, db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "forgot", 20)
    if not mail_configured():
        raise HTTPException(status_code=503, detail="Email delivery unavailable")
    try:
        email = canonical_email(payload.email)
    except ValueError:
        return GENERIC_RESET
    user = db.scalar(select(User).where(User.email == email))
    if user and user.email_verified_at and user.disabled_at is None and throttle(db, user, "reset_password"):
        token = new_token(db, user, "reset_password")
        db.commit()
        try:
            send_account_email(user.email, token, purpose="reset_password")
        except HTTPException:
            # Do not expose mailbox existence or mail outcomes.
            pass
    return GENERIC_RESET


@router.post("/password/reset")
def reset(payload: ResetPassword, request: Request, db: Session = Depends(get_db)):
    validate_origin(request)
    limit_email_action(db, request, "reset", 30)
    digest = token_digest(payload.token)
    candidate = db.scalar(select(AccountEmailToken).where(
        AccountEmailToken.token_hash == digest,
        AccountEmailToken.purpose == "reset_password"))
    if not candidate:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    # Lock the user FIRST: two different valid reset links must not race.
    user = db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    row = db.scalar(select(AccountEmailToken).where(
        AccountEmailToken.token_hash == digest,
        AccountEmailToken.purpose == "reset_password").with_for_update())
    if (not user or not row or row.used_at or aware(row.expires_at) <= utc_now()
            or user.email != row.email or not user.email_verified_at):
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.user_id == user.id, AuthIdentity.provider == "password").with_for_update())
    if not identity:
        raise HTTPException(status_code=400, detail="Invalid or expired link")
    identity.password_hash = HASHER.hash(payload.new_password)
    identity.failed_logins = 0
    identity.locked_until = None
    changed_at = utc_now()
    db.execute(update(AccountEmailToken).where(
        AccountEmailToken.user_id == user.id,
        AccountEmailToken.purpose == "reset_password",
        AccountEmailToken.used_at.is_(None),
    ).values(used_at=changed_at))
    db.execute(update(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
    ).values(revoked_at=utc_now()))
    db.commit()
    return {"message": "Password changed; please sign in again"}


@router.patch("/display-name")
def rename(payload: DisplayNameInput, request: Request,
           user: User = Depends(authenticated_user), db: Session = Depends(get_db)):
    validate_origin(request)
    account = db.scalar(select(User).where(User.id == user.id).with_for_update())
    account.display_name = normalized_name(payload.display_name)
    db.commit()
    return account_response(account)
