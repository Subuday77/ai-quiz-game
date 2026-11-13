# VPS: first private staging boot, without a domain

This runbook is for a **new empty test database**, not migrating real accounts.
Do not make the service Internet-accessible. Only Docker's web port is published,
bound to `127.0.0.1:8080`; FastAPI and PostgreSQL have no host ports.

## 1. Get reviewed sources

In Bitvise as `deploy`, one command at a time. The checkout is currently on the
older `feature/question-votes-pool-first-20261110` branch; do not overwrite it
with `git reset --hard`.

```bash
git -C /srv/ai-quiz-game/source status -sb
git -C /srv/ai-quiz-game/source remote set-branches --add origin feature/vps-release-prep-20261113
git -C /srv/ai-quiz-game/source fetch origin
git -C /srv/ai-quiz-game/source switch --track origin/feature/vps-release-prep-20261113
git -C /srv/ai-quiz-game/source status -sb
```

If the repo was cloned with `--single-branch`, the `remote set-branches --add`
step is necessary: `git fetch origin` alone will not discover the new branch.
If the target local branch already exists, use `git switch` without `--track`
then `git pull --ff-only`. Stop if local modifications exist.

## 2. Configure secrets on VPS, not in GitHub or chat

Work in `/srv/ai-quiz-game/source`, e.g. `cd /srv/ai-quiz-game/source`.
Use `umask 077`, copy `.env.production.example` to `.env`,
then run `chmod 600 .env`.

Edit `.env` **on the VPS**, not in a shared log or message. Create a random
URI-safe password (e.g. locally run `openssl rand -hex 32`) and enter it in
`POSTGRES_PASSWORD`. Insert your real `OPENAI_API_KEY`. Keep the real
`OPENAI_MODEL_ID`, reviewer, Keddy and quality settings consistent with
your tested local setup. The Render AI Localizer URL is already included.

For the **SSH-tunnel test only**, edit these two values:

```dotenv
AUTH_ALLOWED_ORIGINS=http://localhost:8080
AUTH_COOKIE_SECURE=false
```

With a permanent public site these must instead be set to
`https://<real-domain>` and `true`. Do not publish an HTTP service with
insecure session cookies.

## 3. Prepare and migrate the isolated new DB

Run commands separately and stop on error:

```bash
sudo docker compose config --quiet
sudo docker compose build
sudo docker compose up -d db
sudo docker compose run --rm backend python -m alembic upgrade head
sudo docker compose run --rm backend python -m persistence.seed_topics
sudo docker compose up -d backend web
sudo docker compose ps
```

The topic seed is mandatory for an empty database: Alembic creates the schema,
but not the 50 gameplay topics. Without it, `POST /api/v2/games` returns
HTTP 503 (`Topic catalog not initialized`). The seeder is idempotent and prints
`Seeded N missing topics.`; a second run normally prints `Seeded 0 missing topics.`

`backend` waits for healthy PostgreSQL. Do not run destructive volume commands,
especially **never** `docker compose down -v` on an account database.

## 4. Inspect locally via SSH forwarding

Create an SSH local forward from the Windows computer:
**local port 8080 -> VPS 127.0.0.1:8080**.
In a new PowerShell SSH session with forwarding, for example:

```powershell
ssh -N -L 8080:127.0.0.1:8080 deploy@202.61.249.2
```

(If your key is not automatically discovered, pass `-i` and its full path.
With Bitvise, use its Client-to-server port forwarding feature instead.)

On Windows, open `http://localhost:8080`. Test account registration,
login/logout, the About popup and one-time star reward, reload, category
selection, and **only then** live question generation and appeals.

A simple server health check (on the VPS) is:

```bash
curl -fsS http://127.0.0.1:8080/api/health
```

Do not paste credentials or full `.env` values into chat or logs.

## 5. Release gates

Before anything public: browser E2E with two users; audit actual quality,
tag dedup and model env settings against local; run an off-site PostgreSQL
backup and restoration drill; configure domain DNS, Caddy HTTPS, firewall
80/443 and `AUTH_COOKIE_SECURE=true`; pick a release tag after PR #24
and #27 are reviewed. Enable DB First only after its separate acceptance.
