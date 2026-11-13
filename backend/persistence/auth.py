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
from sqlalchemy import select, update
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
    ui_language: str
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
        parsed = urlsplit(origin)
        expected = urlsplit(str(request.base_url))
        # Angular dev proxy uses changeOrigin=true, so its browser Origin
        # differs from the forwarded Host. Explicitly allow trusted frontends.
        extra = {item.strip().rstrip("/") for item in
                 os.environ.get("AUTH_ALLOWED_ORIGINS", "").split(",") if item.strip()}
        allowed = {f"{expected.scheme}://{expected.netloc}", *extra}
        if parsed.scheme not in {"http", "https"} or (
                f"{parsed.scheme}://{parsed.netloc}" not in allowed):
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


@router.get("/username-suggestions")
def username_suggestions(username: str, db: Session = Depends(get_db)) -> dict[str, list[str]]:
    """Offer three currently unused usernames, never claiming to reserve them.

    The same database uniqueness constraint still decides the eventual signup.
    Suggestions follow the existing ASCII username policy and are only offered
    for valid proposed usernames.
    """
    try:
        username_key(username)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid username") from None
    base = unicodedata.normalize("NFKC", username.strip())
    suffixes = ("2026", "2000", "123", "_1", "_2", "_3", "_4", "_5")
    suffixes += tuple(str(number) for number in range(1, 201))
    suggestions: list[str] = []
    seen: set[str] = {username_key(base)}
    for suffix in suffixes:
        candidate = base[:30 - len(suffix)] + suffix
        key = username_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        if db.scalar(select(User.id).where(User.username_key == key)) is None:
            suggestions.append(candidate)
            if len(suggestions) == 3:
                break
    return {"suggestions": suggestions}


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


class UsernameChange(BaseModel):
    """Change the real sign-in username, not just its display label."""
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=40)

    @field_validator("username")
    @classmethod
    def validate_new_username(cls, value: str) -> str:
        username_key(value)
        return unicodedata.normalize("NFKC", value.strip())


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    confirm_password: str = Field(min_length=8, max_length=256)


@router.patch("/username", response_model=AccountOut)
def change_username(payload: UsernameChange, request: Request,
                    user: User = Depends(authenticated_user),
                    db: Session = Depends(get_db)):
    validate_origin(request)
    # Lock the existing account to serialize concurrent profile updates.
    account = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if account is None or account.disabled_at is not None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    key = username_key(payload.username)
    if key != account.username_key and db.scalar(
        select(User.id).where(User.username_key == key)
    ) is not None:
        raise HTTPException(status_code=409, detail="Username unavailable")

    # Password sign-in uses provider_subject, not users.username_key.
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.user_id == account.id,
        AuthIdentity.provider == "password",
    ).with_for_update())
    old_username = account.username
    if payload.username == old_username:
        return account
    account.username = payload.username
    account.username_key = key
    if account.display_name == old_username or not account.display_name:
        account.display_name = payload.username
    if identity is not None:
        identity.provider_subject = key
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username unavailable") from None
    db.refresh(account)
    return account


@router.patch("/password", status_code=204)
def change_password(payload: PasswordChange, request: Request,
                    user: User = Depends(authenticated_user),
                    db: Session = Depends(get_db)):
    validate_origin(request)
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=422, detail="New passwords do not match")
    # Serialize password changes to prevent two competing updates.
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.user_id == user.id,
        AuthIdentity.provider == "password",
    ).with_for_update())
    if identity is None:
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
        raise HTTPException(status_code=401, detail="Current password is incorrect") from None
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=422, detail="Choose a different new password")
    identity.password_hash = HASHER.hash(payload.new_password)
    identity.failed_logins = 0
    identity.locked_until = None
    # Keep this browser signed in while invalidating sessions on other devices.
    token = request.cookies.get(AUTH_COOKIE, "")
    db.execute(update(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None),
        AuthSession.token_hash != session_digest(token),
    ).values(revoked_at=utc_now()))
    db.commit()
    return Response(status_code=204)


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
