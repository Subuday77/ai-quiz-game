"""Offline UI localization tests: no external network or paid API calls."""
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import app
from localization.catalog import LANGUAGES, english_dictionary, source_hash
from localization.client import LocalizationUnavailable
from localization.service import dictionary_response
from persistence.database import get_db
from persistence.models import Base, UiLocaleSync, UiTranslation


class LocalizationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def enable_fks(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.addCleanup(self.engine.dispose)
        self.env = patch.dict(os.environ, {"AI_LOCALIZER_BASE_URL": "https://localizer.example"})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_english_dictionary_is_source_and_requires_no_ai(self):
        with patch("localization.service.translate_missing", new_callable=AsyncMock) as translator:
            result = await dictionary_response(self.db, "en")
        self.assertEqual(result["dictionary"], english_dictionary())
        self.assertEqual(result["missing_count"], 0)
        self.assertFalse(result["fallback"])
        translator.assert_not_called()

    async def test_first_translation_is_saved_and_subsequent_load_is_free(self):
        async def fake_translate(missing, code):
            self.assertEqual(code, "de")
            self.assertEqual(missing, english_dictionary())
            return {key: "DE " + value for key, value in missing.items()}

        translator = AsyncMock(side_effect=fake_translate)
        with patch("localization.service.translate_missing", translator):
            first = await dictionary_response(self.db, "de")
            second = await dictionary_response(self.db, "de")
        self.assertFalse(first["fallback"])
        self.assertEqual(first, second)
        self.assertEqual(translator.await_count, 1)
        self.assertEqual(self.db.scalar(select(func.count(UiTranslation.string_key))),
                         len(english_dictionary()))
        self.assertEqual(first["dictionary"]["hero.ready"], "DE Ready to play?")

    async def test_only_changed_and_added_strings_are_retranslated(self):
        source_v1 = {"a": "Hello", "b": "Bye"}
        source_v2 = {"a": "Hello", "b": "Goodbye", "c": "New"}
        calls = []
        async def fake_translate(missing, code):
            calls.append(dict(missing))
            return {key: "FR " + value for key, value in missing.items()}

        with patch("localization.service.english_dictionary", side_effect=[
            source_v1, source_v2, source_v2,
        ]), patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=fake_translate)):
            first = await dictionary_response(self.db, "fr")
            second = await dictionary_response(self.db, "fr")
            third = await dictionary_response(self.db, "fr")
        self.assertEqual(calls, [source_v1, {"b": "Goodbye", "c": "New"}])
        self.assertEqual(first["dictionary"], {"a": "FR Hello", "b": "FR Bye"})
        self.assertEqual(second["dictionary"], {"a": "FR Hello",
                          "b": "FR Goodbye", "c": "FR New"})
        self.assertEqual(third, second)

    async def test_failure_never_caches_english_and_has_cooldown(self):
        translator = AsyncMock(side_effect=LocalizationUnavailable("no service"))
        with patch("localization.service.translate_missing", translator):
            first = await dictionary_response(self.db, "he")
            second = await dictionary_response(self.db, "he")
        self.assertEqual(first["dictionary"], english_dictionary())
        self.assertEqual(first["missing_count"], len(english_dictionary()))
        self.assertTrue(first["fallback"])
        self.assertEqual(second, first)
        self.assertEqual(translator.await_count, 1)
        self.assertEqual(self.db.scalar(select(func.count(UiTranslation.string_key))), 0)
        state = self.db.get(UiLocaleSync, "he")
        self.assertIsNotNone(state.retry_after)

    async def test_partial_cached_string_is_reused_during_outage(self):
        self.db.add(UiTranslation(
            locale_code="ru", string_key="hero.ready",
            source_hash=source_hash(english_dictionary()["hero.ready"]),
            text="Начнём игру?",
        ))
        self.db.commit()
        with patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=LocalizationUnavailable("offline"))):
            result = await dictionary_response(self.db, "ru")
        self.assertEqual(result["dictionary"]["hero.ready"], "Начнём игру?")
        self.assertEqual(result["dictionary"]["hero.question"],
                         english_dictionary()["hero.question"])
        self.assertEqual(result["missing_count"], len(english_dictionary()) - 1)

    def test_locale_choices_have_eleven_native_names(self):
        self.assertEqual(len(LANGUAGES), 11)
        self.assertEqual(LANGUAGES["de"], "Deutsch")
        self.assertEqual(LANGUAGES["ru"], "Русский")
        self.assertEqual(LANGUAGES["he"], "עברית")
        self.assertEqual(LANGUAGES["ar"], "العربية")
        self.assertEqual(LANGUAGES["ja"], "日本語")
        self.assertEqual(LANGUAGES["zh"], "中文")


class LocalizationApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        @event.listens_for(self.engine, "connect")
        def enable_fks(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

        def override_db():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.enterContext(patch.dict(os.environ, {
            "AUTH_COOKIE_SECURE": "false",
            "AI_LOCALIZER_BASE_URL": "",
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_native_language_endpoint_and_english(self):
        response = self.client.get("/api/localization/languages")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 11)
        self.assertIn({"code": "he", "native_name": "עברית"}, response.json())
        english = self.client.get("/api/localization/dictionary/en")
        self.assertEqual(english.status_code, 200)
        self.assertEqual(english.json()["dictionary"], english_dictionary())

    def test_unsupported_locale_and_unauthorized_preference(self):
        self.assertEqual(self.client.get(
            "/api/localization/dictionary/xx"
        ).status_code, 404)
        self.assertEqual(self.client.patch(
            "/api/localization/preference", json={"language_code": "ru"}
        ).status_code, 401)

    def test_ui_preference_does_not_change_question_language(self):
        registration = self.client.post("/api/auth/register", json={
            "username": "Alice", "password": "offline-test-password-2026!",
        })
        self.assertEqual(registration.status_code, 201, registration.text)
        self.assertEqual(registration.json()["preferred_language"], "English")
        self.assertEqual(registration.json()["ui_language"], "en")
        response = self.client.patch("/api/localization/preference", json={
            "language_code": "he",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["language_code"], "he")
        me = self.client.get("/api/auth/me").json()
        self.assertEqual(me["ui_language"], "he")
        self.assertEqual(me["preferred_language"], "English")
        self.assertEqual(self.client.patch(
            "/api/localization/preference", json={"language_code": "xx"}
        ).status_code, 422)
        self.assertEqual(self.client.patch(
            "/api/localization/preference", json={"language_code": "ru"},
            headers={"Origin": "https://attacker.example"},
        ).status_code, 403)


if __name__ == "__main__":
    unittest.main()
