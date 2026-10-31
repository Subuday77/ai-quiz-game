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


## Switching languages without flashing English

The Angular localization store commits the **language code and complete
dictionary together**. While a new language loads, the previous page remains
unchanged and a full-screen loading overlay prevents clicking stale controls.
Its illuminated two-orbit spinner has rotating status messages in **all eleven
native languages** (English, German, French, Spanish, Italian, Portuguese,
Russian, Hebrew, Arabic, Japanese, Chinese).

Already prepared languages switch as soon as their dictionary response arrives.
Incomplete or temporarily unavailable translations are not installed as a new
English-looking interface. The client briefly polls for an in-progress
translation (up to 12 retries with 2.5-second gaps); if it cannot complete,
the old language stays selected and a visible error message is shown.
On the next attempt, the user can try again; failed/partial results are never
stored as an approved language preference.

The question language is still fully independent. Switching interface
direction never changes question text or an in-progress game.

## Optional LOCAL-ONLY translation reset button

This is a **temporary development utility**, not a normal user feature.
By default the button and the write endpoint are disabled. To enable it only
for local testing, set in the untracked root `.env`:

```dotenv
LOCALIZATION_RESET_ENABLED=true
```

Restart the local FastAPI service. Use the local Angular address
`http://127.0.0.1:4200` and sign in. A small **Reset translations (dev)**
button appears near the interface-language selector.

When pressed, SweetAlert2 explains in plain language:

> The site keeps translations in a shared database. Removing a language
> deletes the translations already saved for everyone. The next time someone
> opens it, AI Localizer will be asked to translate it again. That may cost
> money. Player accounts, questions and results are not deleted.

Choose a single non-English language or **all translated languages**, then
confirm. The button calls `POST /api/localization/reset` with
`{"language_code":"de"}` or `{"language_code":"all"}`. It deletes matching
records in `ui_translations` and `ui_locale_sync`, **not** the English source,
users, questions, game results, or the account's language preference.

A reset does **not** change the page immediately; when the current language
was cleared, it is re-fetched with the full-screen spinner. Other languages
are generated the next time someone selects them.

The backend requires **all three** before a reset is allowed: an authenticated
Quiz session, `LOCALIZATION_RESET_ENABLED=true`, and a request that comes
from loopback (`127.0.0.1`, `::1` or `localhost`), with the usual Origin
check for cookie-authenticated POSTs. The capability endpoint
`GET /api/localization/reset-capability` reports availability to a signed-in
user. Do not set this flag on Render or other public services.

To disable the control again, use `LOCALIZATION_RESET_ENABLED=false` and
restart FastAPI. No migration or manual SQL is needed.

## Acceptance checks for this change

1. Switch Deutsch to Français after both languages have been generated:
   German should remain until French replaces it, without an English flash.
2. Pick a new language: a full-screen spinner should rotate with messages
   in multiple languages while the remote translation runs.
3. Deliberately stop the Localizer, select an uncached language, and ensure
   the prior language stays displayed if translation fails.
4. With reset disabled, no reset button should appear. Its API must reject
   a POST even if someone constructs the request manually.
5. Enable reset locally, sign in, clear German, and verify French stays
   cached. Selecting German should produce a fresh translation.
6. Clear all translations and verify the English master and all quiz/player
   data remain intact.


## Language menu labels and reset dialog (2026-10-31 update)

The **interface-language** dropdown continues to display native names regardless
of the UI locale (Deutsch, Français, Русский, עברית, etc.).

The separate **question-language** dropdown now translates its labels using
seven new `questionLanguage.*` keys in `backend/localization/en.json`.
Only the **visible label** changes. The HTML option value sent to the game
backend remains the existing English identifier (`English`, `Russian`,
`Hebrew`, `German`, `French`, `Spanish`, `Italian`). Changing the UI
language does not change the question language.

The **Reset translations (dev)** button and **all contents** of its SweetAlert2
confirmation/success dialogs stay in English, including the choice labels,
buttons, and explanatory warning. Their fixed copy lives in
`frontend/src/app/localization/reset-copy.ts` and is intentionally **excluded
from the AI translation source dictionary**. Its optional failure message is
also shown in English.

The loading overlay now uses one continuously visible status line, updated every
two seconds by RxJS (rather than eleven independent CSS opacity animations).
This avoids blank frames. The spinner rings remain CSS-animated, including
their slower rotation under reduced-motion settings.

**Note:** because seven new English keys were added, each already-cached
non-English dictionary will automatically request **only seven new strings**
on its next load. No manual database reset or new migration is needed. Removed
reset-dialog keys are no longer displayed or sent for translation.
