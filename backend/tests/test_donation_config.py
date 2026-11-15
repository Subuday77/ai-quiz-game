"""Public donation configuration must never expose arbitrary links."""
import os
import unittest
from unittest.mock import patch

from api import public_client_config, public_donation_url


class DonationConfigTests(unittest.TestCase):
    def test_optional_link_is_off_by_default(self):
        with patch.dict(os.environ, {"BUY_ME_A_COFFEE_URL": ""}):
            self.assertIsNone(public_donation_url())
            self.assertIsNone(public_client_config()["donation_url"])

    def test_accepts_only_personal_https_bmac_profile(self):
        for url in ("https://buymeacoffee.com/squizthebot",
                    "https://www.buymeacoffee.com/squiz_the_bot"):
            with self.subTest(url=url), patch.dict(os.environ, {"BUY_ME_A_COFFEE_URL": url}):
                self.assertEqual(public_donation_url(), url)
                self.assertEqual(public_client_config()["donation_url"], url)

    def test_rejects_untrusted_or_invalid_links(self):
        invalid = (
            "http://buymeacoffee.com/squizthebot",
            "https://buymeacoffee.com",
            "https://buymeacoffee.com/",
            "https://buymeacoffee.com/another/profile",
            "https://buymeacoffee.com.evil.invalid/squizthebot",
            "https://buymeacoffee.com:443/squizthebot",
            "https://buymeacoffee.com@evil.invalid/squizthebot",
            "https://buymeacoffee.com/squizthebot?redirect=evil",
            "https://buymeacoffee.com/squizthebot#section",
            "javascript:alert(1)",
        )
        for url in invalid:
            with self.subTest(url=url), patch.dict(os.environ, {"BUY_ME_A_COFFEE_URL": url}):
                self.assertIsNone(public_donation_url())
                self.assertIsNone(public_client_config()["donation_url"])


if __name__ == "__main__":
    unittest.main()
