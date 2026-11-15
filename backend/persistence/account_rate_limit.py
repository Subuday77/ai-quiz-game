"""PostgreSQL/SQLite atomic 15-minute email-auth rate limiting.

Never trust arbitrary X-Forwarded-For headers. A proxy must be explicitly
listed by its immediate peer IP before we look at its forwarded client IP.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from persistence.models import AuthRateBucket

WINDOW_SECONDS = 15 * 60


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    configured = os.environ.get("AUTH_TRUSTED_PROXY_IPS", "")
    trusted = {part.strip() for part in configured.split(",") if part.strip()}
    if peer in trusted:
        hops = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if hops:
            candidate = hops[-1]  # the proxy's immediate client (avoid spoofable leftmost hops)
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
    return peer


def limit_email_action(db: Session, request: Request, action: str, limit: int):
    """Increment even rejected requests. Does not disclose email existence."""
    bucket_id = int(datetime.now(timezone.utc).timestamp() // WINDOW_SECONDS)
    key = hashlib.sha256(f"{action}|{client_address(request)}|{bucket_id}".encode()).hexdigest()
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        insert = pg_insert(AuthRateBucket)
    elif dialect == "sqlite":
        insert = sqlite_insert(AuthRateBucket)
    else:
        raise HTTPException(status_code=503, detail="Account rate limiting unavailable")
    stmt = insert.values(key_hash=key, window_id=bucket_id, hits=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AuthRateBucket.key_hash],
        set_={"hits": AuthRateBucket.hits + 1},
    ).returning(AuthRateBucket.hits)
    hits = db.scalar(stmt)
    db.commit()
    if hits > limit:
        raise HTTPException(status_code=429, detail="Too many account requests. Try again later")
