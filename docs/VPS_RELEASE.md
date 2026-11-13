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
- `POSTGRES_PASSWORD` must be URI-safe if interpolated into DATABASE_URL.
  Store `.env` as mode 0600; never commit it.
- The API has built-in periodic appeal/vote maintenance. Run a **single**
  Uvicorn worker until duplicate worker scheduling has been assessed.
- Database schema: the latest Alembic migration is 0013. **Back up data**
  before any future migration on an existing PostgreSQL instance.
- Start with DB First disabled. Complete two-user live-model browser
  regression (15 questions + bonus per run) and validate the new feature gates.
- Caddy/HTTPS, off-site PostgreSQL backups, resource alerts and final privacy
  documentation are prerequisites to a public release.

## Installation (when release approved)

On the VPS, in `/srv/ai-quiz-game/source`:

```bash
cp .env.production.example .env
chmod 600 .env
# Edit .env securely; do not use example placeholder secrets.
sudo docker compose build
sudo docker compose up -d db
sudo docker compose run --rm backend python -m alembic upgrade head
sudo docker compose up -d backend web
sudo docker compose ps
```

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
