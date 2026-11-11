"""Offline UI localization tests: no external network or paid API calls."""
import asyncio
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
from localization.dev_tools import reset_available
from starlette.requests import Request
from localization.service import _batches, dictionary_response
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
        # unittest executes cleanup callbacks in reverse registration order.
        # Close the Session while its SQLite connection is still alive.
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
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
            self.assertTrue(set(missing).issubset(english_dictionary()))
            return {key: "DE " + value for key, value in missing.items()}

        translator = AsyncMock(side_effect=fake_translate)
        with patch("localization.service.translate_missing", translator):
            first = await dictionary_response(self.db, "de")
            second = await dictionary_response(self.db, "de")
        self.assertFalse(first["fallback"])
        self.assertEqual(first, second)
        self.assertEqual(translator.await_count, len(_batches(english_dictionary())))
        self.assertEqual(self.db.scalar(select(func.count(UiTranslation.string_key))),
                         len(english_dictionary()))
        self.assertEqual(first["dictionary"]["hero.ready"], "DE Ready to play?")

    async def test_helper_templates_reuse_shared_localizer_cache(self):
        from helper_fallback_fixtures import russian_templates
        from platinum_helper.fallback_locale import (
            HelperFallbackKey, MissingHelperTranslation,
            cached_helper_templates,
        )

        expected = russian_templates()
        async def translate(missing, code):
            self.assertEqual(code, "ru")
            return {key: expected.get(key, "RU " + value)
                    for key, value in missing.items()}

        with self.assertRaises(MissingHelperTranslation):
            cached_helper_templates(self.db, "ru")

        with patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=translate)) as translator:
            await dictionary_response(self.db, "ru")
            self.assertEqual(translator.await_count, len(_batches(english_dictionary())))

        # Helper runtime does not invoke the Localizer, even with it offline.
        with patch("localization.service.translate_missing",
                   side_effect=AssertionError("No localizer during helper call")):
            templates = cached_helper_templates(self.db, "ru")
        self.assertEqual(templates, expected)
        self.assertEqual(len(templates), len(HelperFallbackKey))

        # After a source-language edit, stale cached translations are unusable.
        row = self.db.get(UiTranslation, ("ru", HelperFallbackKey.CONFIDENT_1.value))
        row.source_hash = "stale-source-version"
        self.db.commit()
        with self.assertRaises(MissingHelperTranslation):
            cached_helper_templates(self.db, "ru")

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
        self.assertEqual(translator.await_count, len(_batches(english_dictionary())))
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


    async def test_failed_chunk_is_saved_as_missing_and_only_that_chunk_is_retried(self):
        # A half-completed German locale must never be presented as ready.
        source = {letter: letter.upper() for letter in "abcdef"}
        failed_once = False
        calls = []

        async def translate(missing, code):
            nonlocal failed_once
            calls.append(tuple(missing))
            if "c" in missing and not failed_once:
                failed_once = True
                raise LocalizationUnavailable("temporary provider failure")
            return {key: "DE " + text for key, text in missing.items()}

        with patch("localization.service.english_dictionary", return_value=source), \
             patch("localization.service.BATCH_MAX_KEYS", 2), \
             patch("localization.service.BATCH_MAX_CHARS", 1000), \
             patch("localization.service.PARTIAL_RETRY_SECONDS", 0), \
             patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=translate)):
            first = await dictionary_response(self.db, "de")
            self.assertTrue(first["fallback"])
            self.assertEqual(first["missing_count"], 2)
            self.assertEqual(first["dictionary"]["a"], "DE A")
            self.assertEqual(first["dictionary"]["c"], "C")
            self.assertEqual(self.db.scalar(select(func.count()).select_from(UiTranslation)), 4)
            self.assertIsNotNone(self.db.get(UiLocaleSync, "de").retry_after)

            second = await dictionary_response(self.db, "de")

        self.assertEqual(calls, [("a", "b"), ("c", "d"), ("e", "f"), ("c", "d")])
        self.assertFalse(second["fallback"])
        self.assertEqual(second["missing_count"], 0)
        self.assertEqual(second["dictionary"], {key: "DE " + value
                                                for key, value in source.items()})
        self.assertEqual(self.db.scalar(select(func.count()).select_from(UiTranslation)), 6)

    async def test_many_chunks_keep_provider_concurrency_bounded(self):
        source = {str(index): str(index) for index in range(9)}
        active = 0
        peak = 0

        async def translate(missing, code):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return {key: "RU " + text for key, text in missing.items()}

        with patch("localization.service.english_dictionary", return_value=source), \
             patch("localization.service.BATCH_MAX_KEYS", 1), \
             patch("localization.service.BATCH_MAX_CHARS", 1000), \
             patch("localization.service.BATCH_CONCURRENCY", 2), \
             patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=translate)):
            result = await dictionary_response(self.db, "ru")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(peak, 2)

    async def test_timeout_keeps_successful_chunks_and_releases_lease(self):
        source = {"a": "one", "b": "two"}

        async def translate(missing, code):
            if "b" in missing:
                await asyncio.sleep(1)
            return {key: "HE " + value for key, value in missing.items()}

        with patch("localization.service.english_dictionary", return_value=source), \
             patch("localization.service.BATCH_MAX_KEYS", 1), \
             patch("localization.service.BATCH_CONCURRENCY", 2), \
             patch("localization.service.TRANSLATION_BUDGET_SECONDS", 0.04), \
             patch("localization.service.translate_missing",
                   new=AsyncMock(side_effect=translate)):
            result = await dictionary_response(self.db, "he")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["dictionary"], {"a": "HE one", "b": "two"})
        state = self.db.get(UiLocaleSync, "he")
        self.assertIsNone(state.lease_token)
        self.assertIsNone(state.lease_until)
        self.assertIsNotNone(state.retry_after)

    def test_batches_obey_key_and_character_limits(self):
        source = {"a": "A" * 8, "b": "B" * 8, "c": "C" * 3}
        with patch("localization.service.BATCH_MAX_KEYS", 3), \
             patch("localization.service.BATCH_MAX_CHARS", 10):
            self.assertEqual(_batches(source), [
                {"a": "A" * 8}, {"b": "B" * 8}, {"c": "C" * 3},
            ])

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
            "LOCALIZATION_RESET_ENABLED": "false",
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


    def test_reset_capability_is_disabled_by_default_and_requires_login(self):
        self.assertEqual(self.client.get(
            "/api/localization/reset-capability"
        ).status_code, 401)
        self.assertEqual(self.client.post(
            "/api/localization/reset", json={"language_code": "de"}
        ).status_code, 401)
        registration = self.client.post("/api/auth/register", json={
            "username": "TestReset", "password": "offline-test-password-2026!",
        })
        self.assertEqual(registration.status_code, 201, registration.text)
        self.assertEqual(self.client.get(
            "/api/localization/reset-capability"
        ).json(), {"enabled": False})
        self.assertEqual(self.client.post(
            "/api/localization/reset", json={"language_code": "de"}
        ).status_code, 404)

    def test_reset_removes_only_selected_locale_and_retains_users(self):
        registration = self.client.post("/api/auth/register", json={
            "username": "TestReset", "password": "offline-test-password-2026!",
        })
        self.assertEqual(registration.status_code, 201, registration.text)
        with Session(self.engine) as db:
            for code in ("de", "fr"):
                db.add(UiTranslation(
                    locale_code=code, string_key="hero.ready",
                    source_hash=source_hash(english_dictionary()["hero.ready"]),
                    text=f"translated {code}",
                ))
                db.add(UiLocaleSync(locale_code=code))
            db.commit()

        with patch("localization.dev_tools.reset_available", return_value=True):
            response = self.client.post(
                "/api/localization/reset", json={"language_code": "de"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["removed_count"], 1)
            self.assertEqual(self.client.post(
                "/api/localization/reset", json={"language_code": "en"}
            ).status_code, 422)
            self.assertEqual(self.client.post(
                "/api/localization/reset", json={"language_code": "xx"}
            ).status_code, 422)
        with Session(self.engine) as db:
            self.assertIsNone(db.get(UiTranslation, ("de", "hero.ready")))
            self.assertIsNone(db.get(UiLocaleSync, "de"))
            self.assertIsNotNone(db.get(UiTranslation, ("fr", "hero.ready")))
            self.assertIsNotNone(db.get(UiLocaleSync, "fr"))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)

        with patch("localization.dev_tools.reset_available", return_value=True):
            response = self.client.post(
                "/api/localization/reset", json={"language_code": "all"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["removed_count"], 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(UiTranslation)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(UiLocaleSync)), 0)

    def test_reset_guard_needs_opt_in_and_loopback_address(self):
        # The guard is independently tested; reset endpoints also check login
        # and validate Origin. A spoofed Host alone cannot enable the tool.
        scope = {
            "type": "http", "scheme": "http", "method": "POST",
            "path": "/api/localization/reset", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8000")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
        }
        req = Request(scope)
        with patch.dict(os.environ, {"LOCALIZATION_RESET_ENABLED": "true"}):
            self.assertTrue(reset_available(req))
            remote = dict(scope)
            remote["client"] = ("192.0.2.2", 12345)
            self.assertFalse(reset_available(Request(remote)))
        with patch.dict(os.environ, {"LOCALIZATION_RESET_ENABLED": "false"}):
            self.assertFalse(reset_available(req))


if __name__ == "__main__":
    unittest.main()
