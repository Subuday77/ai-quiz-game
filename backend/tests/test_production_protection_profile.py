"""Guard against silent production fallback to disabled quiz protections."""
from pathlib import Path
import unittest

from scripts.audit_runtime_guards import audit


ROOT = Path(__file__).resolve().parents[2]
PROFILE = {
    name.strip(): value.strip()
    for raw in (ROOT / ".env.production.example").read_text(encoding="utf-8").splitlines()
    if (line := raw.strip()) and not line.startswith("#") and "=" in line
    for name, value in [line.split("=", 1)]
}


class ProtectionProfileTests(unittest.TestCase):
    def test_production_template_pins_approved_safeguards(self):
        safeguards = {
            "AI_TAG_DEDUP_ENABLED": "true",
            "AI_QUALITY_MODE": "enforce",
            "AI_LOCAL_LEAK_MODE": "enforce",
            "QUIZ_QUALITY_ARBITER_MODE": "on",
            "AI_STRUCTURED_OUTPUT_MODE": "strict",
            "AI_QUALITY_STRUCTURED_OUTPUT_MODE": "strict",
            "AI_QUALITY_CONTRACT": "v2",
            "AI_QUALITY_MODEL_ID": "gpt-5.4-mini",
            "AI_QUALITY_PROVIDER": "openai",
            "QUIZ_QUESTION_POOL_FALLBACK_ENABLED": "true",
            "QUIZ_TOO_EASY_ONLY_FALLBACK_ENABLED": "true",
        }
        for key, expected in safeguards.items():
            with self.subTest(key=key):
                self.assertEqual(PROFILE.get(key), expected)
        self.assertNotEqual(PROFILE["OPENAI_MODEL_ID"], PROFILE["AI_QUALITY_MODEL_ID"])
        self.assertFalse([f for f in audit(PROFILE) if f.level == "FAIL"])

    def test_unset_protections_are_flagged_without_secrets(self):
        incomplete = {
            "OPENAI_MODEL_ID": "generator",
            "QUIZ_COMPLAINT_KEDDY_MODEL_ID": "parser",
            "QUIZ_COMPLAINT_JUDGE_MODEL_ID": "judge",
            "AUTH_ALLOWED_ORIGINS": "http://localhost:8080",
            "AUTH_COOKIE_SECURE": "false",
        }
        findings = {item.key: item for item in audit(incomplete)}
        for key in (
            "AI_TAG_DEDUP_ENABLED", "AI_QUALITY_MODE", "QUIZ_QUALITY_ARBITER_MODE",
            "AI_STRUCTURED_OUTPUT_MODE", "AI_QUALITY_MODEL_ID",
        ):
            self.assertEqual(findings[key].level, "FAIL", key)
        self.assertEqual(findings["AI_LOCAL_LEAK_MODE"].level, "PASS")
        self.assertEqual(findings["AUTH_COOKIE_SECURE"].level, "WARN")

    def test_public_insecure_cookies_are_reported(self):
        values = dict(PROFILE, AUTH_ALLOWED_ORIGINS="https://quiz.example",
                      AUTH_COOKIE_SECURE="false")
        findings = {item.key: item for item in audit(values)}
        self.assertEqual(findings["AUTH_COOKIE_SECURE"].level, "FAIL")


if __name__ == "__main__":
    unittest.main()
