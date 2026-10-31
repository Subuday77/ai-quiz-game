# Quiz UI localization (independent of question language)

This change is **only for AI Quiz Game**. Neither MIRA nor the AI Localizer
repository has been modified.

## Independent language settings

- **Interface language**: 11 languages, in a separate dropdown. Each
  language name is displayed in its **own native language**, never translated:
  English, Deutsch, Français, Español, Italiano, Português, Русский, עברית,
  العربية, 日本語, 中文.
- **Question language**: the original seven choices remain separate. Changing
  the interface does not alter question generation or an existing game round.
- The selected interface locale is stored on the account in
  `users.ui_language` (default `en`). `users.preferred_language` and
  `game_runs.language` remain independent.
- A guest can select interface language for the current browser session.
  After login the account's saved locale takes precedence.

## English source and translation cache

- **Single English source of truth**: `backend/localization/en.json`.
- On every `GET /api/localization/dictionary/{language_code}`, FastAPI reads
  the current English dictionary and calculates SHA-256 hashes per value.
- `ui_translations` stores per-locale, per-key translation, source hash, and
  timestamp. Translations are shared by all users.
- Unchanged English text -> reuse stored translation, **no remote request**.
- New or changed English text -> send **only** those entries in the dictionary
  to the existing AI Localizer via `POST /translate`.
- Removed keys are absent from API output; old DB rows may remain unused.
- During an AI Localizer outage, saved matching translations survive and
  uncached/outdated keys fall back to the latest English text.
- A 120-second per-locale lease and 5-minute error cooldown prevent repeated
  paid translation attempts during concurrent requests or outages.
- The frontend never calls the AI Localizer directly and never sees its keys.
- The language dropdown's native labels are **not part of en.json** and
  never go to the translator.

## PostgreSQL migration

This branch adds migration `0002_ui_localization`:

- `users.ui_language` with a default of `en`.
- `ui_translations` for translated strings.
- `ui_locale_sync` for short leases and retry cooldown.

Earlier migration `0001_initial` is unchanged. Back up data before
applying any migration to an existing installation. From `backend/` in
PyCharm:

```powershell
python -m alembic -c alembic.ini upgrade head
python -m alembic -c alembic.ini current
python -m unittest discover -s tests -p test_localization.py -v
python -m unittest discover -s tests -q
```

Expected Alembic head: `0002_ui_localization`. Do not run `initdb`, drop
tables, or recreate the PostgreSQL cluster.

## Environment configuration

In the **root** untracked `.env`, set:

```dotenv
AI_LOCALIZER_BASE_URL=https://your-ai-localizer.onrender.com
```

Use the actual deployed **AI Localizer** HTTPS base URL, with no final
`/translate`. This server-to-server configuration is separate from the
Quiz's `LLM_PROVIDER` and AI question-generator settings.

If the URL is missing, FastAPI supplies cached translations and English
fallback but does not call the translator. When testing a local Localizer,
`http://127.0.0.1:8080` is allowed.

Quiz endpoints:

- `GET /api/localization/languages` – native names for 11 locales.
- `GET /api/localization/dictionary/en` – canonical English source.
- `GET /api/localization/dictionary/de` (and other codes) – version-checked
  cached dictionary; `fallback` indicates any still-English keys.
- `PATCH /api/localization/preference` with
  `{"language_code":"de"}` – authenticated user preference, protected by
  the existing origin check.
- `GET /api/auth/me` now includes `ui_language`.

## Angular

In VS Code from `frontend/`:

```powershell
npm ci
npm test
npm run build
npm start
```

Open `http://127.0.0.1:4200`. The frontend only contacts the Quiz API.
The interface dropdown shows each language's native name, while the separate
question-language dropdown still determines the AI question language.
For Hebrew and Arabic interface locales, surrounding UI switches to RTL;
question text uses RTL based on **question language** rather than UI locale.

Manual smoke tests:

1. Choose Deutsch for the interface, Russian for questions. The UI becomes
   German; AI question text stays Russian.
2. Refresh and log back in. The saved account locale is restored.
3. Repeat the German selection. Check FastAPI logs: it should NOT call
   AI Localizer again if the dictionary has not changed.
4. Add a test string to `backend/localization/en.json`, restart the backend,
   request German again, and verify only that new key reaches the Localizer.
5. Disconnect the Localizer and verify existing translations remain; missing
   keys show English and the next attempt is delayed by the cooldown.
6. Check `/api/localization/languages`: every displayed language name is
   native even while the surrounding UI is another language.
7. Ensure registration still shows the minimum password length hint but
   login does not.

## Verification status

The code and offline tests are in the feature branch, but tests and the
Angular build must still be run locally on the user's Windows machine.
No migration has been executed against the user's PostgreSQL automatically.
Do not merge to `master` or deploy to Render until those checks pass.
