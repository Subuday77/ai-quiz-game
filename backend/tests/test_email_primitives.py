"""Pure offline tests for email-first account primitives."""
import unittest
from datetime import datetime, timezone

from persistence.account_email import canonical_email, create_one_time_token, token_digest, token_expiry


class EmailPrimitivesTests(unittest.TestCase):
    def test_case_insensitive_email_identity(self):
        self.assertEqual(canonical_email("  Alice@EXAMPLE.COM "), "alice@example.com")
        self.assertEqual(canonical_email("ALICE@example.com"), canonical_email("alice@EXAMPLE.com"))

    def test_does_not_fold_plus_or_dots(self):
        self.assertNotEqual(canonical_email("a.b@example.com"), canonical_email("ab@example.com"))
        self.assertNotEqual(canonical_email("a+tag@example.com"), canonical_email("a@example.com"))

    def test_idna_domain(self):
        self.assertEqual(canonical_email("test@bücher.de"), "test@xn--bcher-kva.de")

    def test_reject_invalid_emails(self):
        for item in ("", "x", "a@@b.com", "a@localhost", "a@-example.com",
                     "a@example..com", "a@exa mple.com", "a@.example.com"):
            with self.subTest(item=item), self.assertRaises(ValueError):
                canonical_email(item)

    def test_tokens_are_unique_and_hashed(self):
        token_a, digest_a = create_one_time_token()
        token_b, digest_b = create_one_time_token()
        self.assertNotEqual(token_a, token_b)
        self.assertNotEqual(digest_a, digest_b)
        self.assertEqual(digest_a, token_digest(token_a))
        self.assertNotEqual(token_a, digest_a)
        self.assertEqual(len(digest_a), 64)

    def test_token_expiry_is_timezone_aware(self):
        self.assertGreater(token_expiry(), datetime.now(timezone.utc))
        with self.assertRaises(ValueError):
            token_expiry(minutes=0)


if __name__ == "__main__":
    unittest.main()
