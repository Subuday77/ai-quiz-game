"""Offline regression: the existing FastAPI server owns appeal retries."""
import asyncio
import os
import threading
import unittest
from unittest.mock import patch

from api import app
from persistence.appeal_scheduler import appeal_lifespan


class AppealSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_database_does_not_start_maintenance(self):
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True), patch(
            "persistence.appeal_scheduler.process_due"
        ) as process_due:
            async with appeal_lifespan(app):
                await asyncio.sleep(0)
            process_due.assert_not_called()

    async def test_database_runs_due_processing_on_startup_and_stops(self):
        called = threading.Event()

        def drain():
            called.set()
            return {}

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://test/test"}), patch(
            "persistence.appeal_scheduler.process_due", side_effect=drain
        ) as process_due:
            async with appeal_lifespan(app):
                ran = await asyncio.wait_for(asyncio.to_thread(called.wait, 2), timeout=3)
                self.assertTrue(ran, "FastAPI did not start appeal maintenance")
            self.assertGreaterEqual(process_due.call_count, 1)

    def test_server_wires_lifespan(self):
        self.assertIs(app.router.lifespan_context, appeal_lifespan)


if __name__ == "__main__":
    unittest.main()
