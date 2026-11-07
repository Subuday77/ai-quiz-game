"""Faker helper names follow *UI* locale; never request AI translation."""
import unittest
from random import Random

from platinum_helper.identity import (
    PERSON_LOCALES, UI_FAKER_LOCALES, generate_identity,
)


class HelperIdentityTests(unittest.TestCase):
    def test_name_is_stable_with_seed_and_contains_a_surname(self):
        first = generate_identity(Random(911), ui_language="ru")
        second = generate_identity(Random(911), ui_language="ru")
        self.assertEqual(first, second)
        self.assertIn(" ", first["name"])
        self.assertEqual(first["locale"], "ru_RU")
        self.assertIn(first["gender"], ("male", "female"))
        self.assertEqual(first["nationality"], PERSON_LOCALES[first["locale"]])

    def test_every_supported_interface_language_has_a_named_faker_provider(self):
        self.assertEqual(
            set(UI_FAKER_LOCALES),
            {"en", "de", "fr", "es", "it", "pt", "ru", "he", "ar", "ja", "zh"},
        )
        from faker.config import AVAILABLE_LOCALES
        for code, locale in UI_FAKER_LOCALES.items():
            with self.subTest(code=code):
                self.assertIn(locale, AVAILABLE_LOCALES)
                name = generate_identity(Random(42), ui_language=code)
                self.assertEqual(name["locale"], locale)
                self.assertTrue(name["name"].strip())
                self.assertEqual(name["nationality"], PERSON_LOCALES[locale])

    def test_russian_interface_generates_cyrillic_not_latin_names(self):
        for seed in range(30):
            name = generate_identity(Random(seed), ui_language="ru")["name"]
            with self.subTest(seed=seed):
                self.assertRegex(name, r"^[А-Яа-яЁё -]+$")
                self.assertRegex(name, r"[А-Яа-яЁё]")

    def test_other_non_latin_interface_names_are_not_english_fallbacks(self):
        for language, script in (
            ("he", r"[\u0590-\u05ff]"),
            ("ar", r"[\u0600-\u06ff]"),
            ("ja", r"[\u3040-\u30ff\u4e00-\u9fff]"),
            ("zh", r"[\u4e00-\u9fff]"),
        ):
            with self.subTest(language=language):
                name = generate_identity(Random(17), ui_language=language)["name"]
                self.assertRegex(name, script)

    def test_unknown_language_is_explicitly_rejected(self):
        with self.assertRaises(ValueError):
            generate_identity(Random(42), ui_language="unknown")


if __name__ == "__main__":
    unittest.main()
