"""PostgreSQL regression fixture for the additive email-account migrations.

Run before and after Alembic upgrade; intended for isolated CI database only.
Never run the seed phase against a production database.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from persistence.database import engine

USER_ID = UUID("00000000-0000-4000-8000-000000000038")
SESSION_ID = UUID("00000000-0000-4000-8000-000000000039")
TOKEN_HASH = "c" * 64


def seed():
    now = datetime.now(timezone.utc)
    with engine().begin() as connection:
        connection.execute(
            text("""INSERT INTO users
                 (id, username, username_key, display_name,
                  preferred_language, ui_language, theme_key, created_at)
                 VALUES (:id, :username, :username_key, :display_name,
                         :preferred_language, :ui_language, :theme_key, :created_at)"""),
            dict(id=USER_ID, username="LegacyMigrationUser",
                 username_key="legacymigrationuser", display_name="Legacy Migration",
                 preferred_language="English", ui_language="en", theme_key="default",
                 created_at=now),
        )
        connection.execute(
            text("""INSERT INTO auth_sessions
                 (id, user_id, token_hash, created_at, expires_at)
                 VALUES (:id, :user_id, :token_hash, :created_at, :expires_at)"""),
            dict(id=SESSION_ID, user_id=USER_ID, token_hash=TOKEN_HASH,
                 created_at=now, expires_at=now + timedelta(days=7)),
        )
    print("legacy_fixture_seeded")


def validate():
    with engine().begin() as connection:
        account = connection.execute(
            text("SELECT id, username, email, email_verified_at FROM users WHERE id = :id"),
            {"id": USER_ID},
        ).one()
        assert str(account.id) == str(USER_ID)
        assert account.username == "LegacyMigrationUser"
        assert account.email is None and account.email_verified_at is None
        session = connection.execute(
            text("SELECT user_id, token_hash FROM auth_sessions WHERE id = :id"),
            {"id": SESSION_ID},
        ).one()
        assert str(session.user_id) == str(USER_ID)
        assert session.token_hash == TOKEN_HASH
        for table in ("account_email_tokens", "auth_rate_buckets"):
            assert connection.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name = :name"
            ), {"name": table}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one() == 1
    print("email_migration_preserved_legacy_account_and_session")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "validate"}:
        raise SystemExit("Usage: python -m scripts.check_email_migration_postgres seed|validate")
    {"seed": seed, "validate": validate}[sys.argv[1]]()
