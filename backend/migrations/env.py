"""Environment-managed Alembic; password never stored in alembic.ini."""
from alembic import context
from sqlalchemy import engine_from_config, pool
from dotenv import load_dotenv
from pathlib import Path

from persistence.database import database_url
from persistence.models import Base

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
config = context.config
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"},
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    settings = config.get_section(config.config_ini_section)
    settings["sqlalchemy.url"] = database_url()
    connection_engine = engine_from_config(settings, prefix="sqlalchemy.",
                                           poolclass=pool.NullPool)
    with connection_engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    connection_engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
