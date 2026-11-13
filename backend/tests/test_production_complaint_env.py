"""Regression guard: deployed Caddy/Judge model env names match the worker."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from persistence.appeal_worker import _config


class ProductionComplaintEnvTest(unittest.TestCase):
    def test_model_variables_in_deployment_template_match_worker(self):
        template = Path(__file__).resolve().parents[2] / ".env.production.example"
        entries = {}
        for raw in template.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                entries[name] = value

        self.assertNotIn("QUIZ_COMPLAINT_KEDDY_MODEL_ID", entries)
        with patch.dict(os.environ, entries, clear=True):
            self.assertEqual(_config("CADDY")[0], "gpt-6-luna")
            self.assertEqual(_config("JUDGE")[0], "gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
