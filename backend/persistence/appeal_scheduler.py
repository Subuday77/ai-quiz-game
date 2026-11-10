"""Built-in appeal retry scheduler; no second service or open terminal needed.

FastAPI owns this lightweight task for the lifetime of the server.  PostgreSQL
owns the durable retry deadlines and row-level claims. Each ASGI worker may
poll safely: the existing SELECT ... FOR UPDATE SKIP LOCKED + retry lease
ensures only one worker starts a due appeal.

When the server is offline no processing occurs; on restart the first scan
picks up all overdue cases, including expired seven-day deadlines.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from persistence.appeal_worker import process_due

LOG = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 60


async def _process_periodically(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            # Provider/SQL operations are synchronous; don't block FastAPI's
            # event loop while adjudicating an outstanding complaint.
            await asyncio.to_thread(process_due)
        except Exception as exc:
            # DB outages and SDK errors must not terminate the web server.
            # Deliberately log only the error class, not player evidence.
            LOG.warning("Appeal maintenance failed: %s", type(exc).__name__)

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def appeal_lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Offline prototype/tests without PostgreSQL must keep working.
    if not os.getenv("DATABASE_URL"):
        yield
        return

    stop = asyncio.Event()
    task = asyncio.create_task(_process_periodically(stop), name="appeal-maintenance")
    try:
        yield
    finally:
        stop.set()
        # A running synchronous provider request cannot be interrupted safely.
        # Cancel the scheduler task; any unfinished case is durable in the DB
        # and picked up after its retry lease expires on the next app startup.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
