"""PostgreSQL connection boundary; no implicit in-memory production fallback."""
import os
from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    # Managed providers can supply old postgres:// URLs.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("DATABASE_URL must point to PostgreSQL with psycopg")
    return url


@lru_cache(maxsize=1)
def engine():
    return create_engine(database_url(), pool_pre_ping=True, pool_recycle=1800)


def get_db():
    """Yield a request-scoped transaction; account API is disabled without DB."""
    try:
        db = sessionmaker(bind=engine(), autoflush=False, expire_on_commit=False)()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Account database unavailable") from None
    try:
        yield db
    finally:
        db.close()
