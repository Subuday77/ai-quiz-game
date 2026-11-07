"""Faker helper names must not use AI or a translation call."""
import unittest
from random import Random
from unittest.mock import patch

from platinum_helper.identity import PERSON_LOCALES, generate_identity


class HelperIdentityTests(unittest.TestCase):
    def test_name_is_stable_with_seed_and_contains_a_surname(self):
        first = generate_identity(Random(911))
        second = generate_identity(Random(911))
        self.assertEqual(first, second)
        self.assertIn(" ", first["name"])
        self.assertIn(first["locale"], PERSON_LOCALES)
        self.assertIn(first["gender"], ("male", "female"))
        self.assertEqual(first["nationality"], PERSON_LOCALES[first["locale"]])

    def test_each_locale_supports_male_and_female_names(self):
        for locale in PERSON_LOCALES:
            for gender in ("male", "female"):
                rng = Random(42)
                with patch.object(rng, "choice", side_effect=[locale, gender]):
                    identity = generate_identity(rng)
                with self.subTest(locale=locale, gender=gender):
                    self.assertTrue(identity["name"].strip())
                    self.assertEqual(identity["gender"], gender)
                    self.assertEqual(identity["locale"], locale)


if __name__ == "__main__":
    unittest.main()
