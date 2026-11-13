# VPS release candidate — NOT approved for public deployment

This branch is based on `feature/question-votes-pool-first-20261110`, **not**
the older `master`. PR #24 is still draft; validate it before release.

## Preflight

- Do not expose PostgreSQL or backend ports.
- Web publishes only `127.0.0.1:8080`. Put a properly configured TLS
  reverse proxy (e.g. Caddy) in front after DNS is registered.
- Keep `AUTH_COOKIE_SECURE=true` and configure `AUTH_ALLOWED_ORIGINS`
  to the final HTTPS origin. A secure authentication cookie requires HTTPS
  in normal browsers.
- Only install reviewed environment values from the active development
  configuration; never copy local credentials into Git or into images.
- `OPENAI_MODEL_ID` is required by the generator. Check that it matches the
  tested version (at present gpt-5.4). Missing it can pass health checks but
  fail on the first real question.
- `AI_LOCALIZER_BASE_URL` points to the existing Render AI Localizer service.
  Russian and every other non-English language use the shared translator/cache.
- For **private HTTP testing through an SSH tunnel only**, set
  `AUTH_ALLOWED_ORIGINS=http://localhost:8080` and `AUTH_COOKIE_SECURE=false`.
  Restore `https://...` + `AUTH_COOKIE_SECURE=true` before any public access.
- `POSTGRES_PASSWORD` must be URI-safe if interpolated into DATABASE_URL.
  Store `.env` as mode 0600; never commit it.
- The API has built-in periodic appeal/vote maintenance. Run a **single**
  Uvicorn worker until duplicate worker scheduling has been assessed.
- Database schema: the latest Alembic migration is 0013. **Back up data**
  before any future migration on an existing PostgreSQL instance.
- **Do not silently omit question safeguards.** Keep
  `AI_TAG_DEDUP_ENABLED=true`, `AI_QUALITY_MODE=enforce`,
  `QUIZ_QUALITY_ARBITER_MODE=on`, `AI_LOCAL_LEAK_MODE=enforce`,
  `AI_STRUCTURED_OUTPUT_MODE=strict` and
  `AI_QUALITY_STRUCTURED_OUTPUT_MODE=strict`; pin independent
  `AI_QUALITY_MODEL_ID=gpt-5.4-mini` (different from the generator).
  Keep Judge `medium` reasoning, 1800 output tokens and 40s timeout;
  otherwise its fallback is `low` / 800 / 12s.
- **Existing databases:** `python -m tagging.backfill --dry-run` must report
  **0 missing** before turning tagged dedup on. Until then, do NOT turn
  the flag on blindly: `history_for_user` fails closed (HTTP 503).
- Run the read-only `python -m scripts.audit_runtime_guards --strict`
  **inside the running backend container** after any environment edit/recreate.
  This prints allowlisted flag/model policy statuses, never credentials.
  It does NOT inspect DB tag completeness, exposure/firewall, or backups.
- DB First is enabled in the full production profile for the current approved
  VPS test policy. Verify the running backend's actual .env after redeployment;
  do not overwrite or silently flip an existing server's mode. Complete
  two-user live-model browser regression (15 questions + bonus per run).
- Keep .env.production.example in parity with .env.example for all policy
  values except transport, secrets, Localizer and DB First. CI enforces this
  and catches missing settings and duplicate keys.
- Caddy/HTTPS, off-site PostgreSQL backups, resource alerts and final privacy
  documentation are prerequisites to a public release.

## Private staging first

See [VPS_STAGING.md](VPS_STAGING.md). Test over an SSH tunnel with only the web
port bound to **127.0.0.1:8080**, not an Internet-facing port. The DB and FastAPI
ports remain unpublished; do not open UFW 80/443 yet.

## Installation (when release approved)

On the VPS, in `/srv/ai-quiz-game/source`:

```bash
cp .env.production.example .env
chmod 600 .env
# Edit .env securely; do not use example placeholder secrets.
sudo docker compose build
sudo docker compose up -d db
sudo docker compose run --rm backend python -m alembic upgrade head
sudo docker compose run --rm backend python -m persistence.seed_topics
sudo docker compose up -d backend web
sudo docker compose ps
```

On a fresh database, `persistence.seed_topics` is required after Alembic: it
inserts the 50 gameplay categories. Without it, starting a game returns HTTP
503 (`Topic catalog not initialized`). The seeder is safe to rerun.

While web is still loopback-only, inspect through an SSH tunnel:
`ssh -L 8080:127.0.0.1:8080 deploy@YOUR_SERVER_IP`, then
visit http://localhost:8080 (authentication requires HTTPS for secure cookies).

Backups: configure tested daily external backups *before* real users arrive;
Docker named volumes alone are **not** backups. Do not run
`docker compose down -v` on a real database.

## Releasing

1. Approve draft feature copy and login/bonus UX.
2. CI: Python backend, frontend tests/build, migration on PostgreSQL, policy tests.
3. QA: duplicate-name suggestions, registration race, bonus concurrent claims,
   replay, F5, logout/login, multilingual UI, responsive scroll.
4. Merge reviewed changes in an agreed order; tag an exact release commit.
5. Deploy the release **tag**, not a moving experimental branch.
