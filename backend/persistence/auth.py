"""Argon2id username/password authentication, hashed opaque browser sessions.

This is deliberately separate from the existing in-memory game prototype.
Cookies are HttpOnly, Secure by default and SameSite=Strict; no JWT/client
access token storage. Google OAuth identities can be added to the same user.
"""
import hashlib
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher, exceptions
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence.database import get_db
from persistence.models import AuthIdentity, AuthSession, User

AUTH_COOKIE = "quiz_session"
DAYS_VALID = 7
# Reasonable fixed parameters; review performance and security periodically.
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,29}$")
router = APIRouter(prefix="/api/auth", tags=["Accounts"])


def username_key(username: str) -> str:
    name = unicodedata.normalize("NFKC", username.strip())
    if not USERNAME.fullmatch(name):
        raise ValueError("Username must start with a letter and contain 3-30 ASCII letters, digits or _")
    return name.casefold()


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        username_key(value)
        return unicodedata.normalize("NFKC", value.strip())


class AccountOut(BaseModel):
    id: UUID
    username: str
    display_name: str | None = None
    preferred_language: str
    theme_key: str

    model_config = ConfigDict(from_attributes=True)


def utc_now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def session_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def validate_origin(request: Request):
    # Protect write requests made from cookie-authenticated browsers.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site request denied")
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit
        host = urlsplit(origin).netloc.casefold()
        if not host or host != request.headers.get("host", "").casefold():
            raise HTTPException(status_code=403, detail="Origin mismatch")


def secure_cookie() -> bool:
    return os.environ.get("AUTH_COOKIE_SECURE", "true").strip().lower() == "true"


def start_session(db: Session, user: User, response: Response):
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(
        user_id=user.id, token_hash=session_digest(token),
        expires_at=utc_now() + timedelta(days=DAYS_VALID),
    ))
    db.commit()
    response.set_cookie(
        key=AUTH_COOKIE, value=token, httponly=True,
        secure=secure_cookie(), samesite="strict",
        path="/", max_age=DAYS_VALID * 86400,
    )


def authenticated_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(AUTH_COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == session_digest(token)))
    if not session or session.revoked_at or aware(session.expires_at) <= utc_now():
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, session.user_id)
    if user is None or user.disabled_at is not None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.post("/register", response_model=AccountOut, status_code=201)
def register(payload: Credentials, request: Request, response: Response,
             db: Session = Depends(get_db)):
    validate_origin(request)
    key = username_key(payload.username)
    if db.scalar(select(User.id).where(User.username_key == key)) is not None:
        raise HTTPException(status_code=409, detail="Username unavailable")
    user = User(username=payload.username, username_key=key, display_name=payload.username)
    db.add(user)
    db.flush()
    db.add(AuthIdentity(user_id=user.id, provider="password",
                        provider_subject=key, password_hash=HASHER.hash(payload.password)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username unavailable") from None
    start_session(db, user, response)
    return user


@router.post("/login", response_model=AccountOut)
def login(payload: Credentials, request: Request, response: Response,
          db: Session = Depends(get_db)):
    validate_origin(request)
    # Never reveal which part of a credential was wrong.
    key = username_key(payload.username)
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.provider == "password", AuthIdentity.provider_subject == key))
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if identity.locked_until and aware(identity.locked_until) > utc_now():
        raise HTTPException(status_code=429, detail="Please try again later")
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
    user = db.get(User, identity.user_id)
    if user is None or user.disabled_at is not None:
        db.rollback()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    db.commit()
    start_session(db, user, response)
    return user


@router.get("/me", response_model=AccountOut)
def me(user: User = Depends(authenticated_user)):
    return user


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    validate_origin(request)
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        existing = db.scalar(select(AuthSession).where(
            AuthSession.token_hash == session_digest(token)))
        if existing:
            existing.revoked_at = utc_now()
            db.commit()
    response.delete_cookie(key=AUTH_COOKIE, path="/", samesite="strict",
                           secure=secure_cookie(), httponly=True)
