# Quiz → VPS Localizer: private Docker cutover

Status: prepared in feature branch, **not yet switched on the live VPS**.
Old API remains `https://ai-localizer-api.onrender.com` and must stay available for rollback.

## Architecture

- Quiz Compose project: `/srv/ai-quiz-game/source` (`ai-quiz-game`).
- Localizer Compose project: `/srv/ai-localizer/source` (`ai-localizer`).
- Private Docker bridge: `ai-localizer-clients`, created with `--internal`, used only by Quiz backend and Localizer API.
- Docker DNS alias inside this network: `localizer-api:8080`.
- Quiz environment: `AI_LOCALIZER_BASE_URL=http://localizer-api:8080`.
- Quiz environment: `AI_LOCALIZER_API_KEY=<separate quiz bearer token>`.
- Localizer environment: `LOCALIZER_API_KEY_HASHES` JSON list includes quiz key SHA-256 digest; `LOCALIZER_REQUIRE_API_KEY=true` enforced via its Compose file.
- Localizer host port remains `127.0.0.1:18080`. Caddy still returns 403 on public `/translate`. No new public firewall rules.
- PostgreSQL translation cache and all game data are unchanged. Browser JavaScript never sees a Localizer URL or bearer token.

## Before making changes

Check that `git status --short` is clean in **both** repositories. Verify Localizer PR #13 and Quiz feature PR CI are passing and check the exact branch commit. Never overwrite active `.env` or commit secrets.

## Step A: create the internal bridge once

```bash
sudo docker network create --driver bridge --internal ai-localizer-clients
```

Do not create if it already exists. The internal network is isolated but neither service's existing network is deleted.

## Step B: generate independent Quiz secret on VPS

```bash
umask 077
python3 -c 'from pathlib import Path; import secrets; p=Path("/srv/ai-localizer/credentials/quiz.key"); assert not p.exists(), "Quiz key already exists"; p.write_text("loc_sk_"+secrets.token_urlsafe(32)); p.chmod(0o600); print("Quiz Localizer key created")'
```

Update the Localizer `.env` hash list with the digest of the new token, retaining the existing test key digest. Edit only locally and never paste secrets:

```bash
python3 -c 'from pathlib import Path; import hashlib,json; p=Path("/srv/ai-localizer/source/.env"); k=Path("/srv/ai-localizer/credentials/quiz.key").read_text().strip(); h=hashlib.sha256(k.encode()).hexdigest(); ls=p.read_text().splitlines(); old=[s for s in ls if s.startswith("LOCALIZER_API_KEY_HASHES=")]; assert len(old)==1, "Expected exactly one API key hashes setting"; hashes=json.loads(old[0].split("=",1)[1]); assert isinstance(hashes,list); hashes=sorted(set(hashes+[h])); ls=[s for s in ls if not s.startswith("LOCALIZER_API_KEY_HASHES=")]; ls.append("LOCALIZER_API_KEY_HASHES="+json.dumps(hashes)); p.write_text("\n".join(ls)+"\n"); p.chmod(0o600); print("Localizer Quiz key hash registered")'
```

Set the separate Quiz backend `.env` without printing the raw token:

```bash
python3 -c 'from pathlib import Path; p=Path("/srv/ai-quiz-game/source/.env"); key=Path("/srv/ai-localizer/credentials/quiz.key").read_text().strip(); vals={"AI_LOCALIZER_BASE_URL":"http://localizer-api:8080","AI_LOCALIZER_API_KEY":key}; lines=[s for s in p.read_text().splitlines() if not any(s.startswith(n+"=") for n in vals)]; lines.extend(n+"="+v for n,v in vals.items()); p.write_text("\n".join(lines)+"\n"); p.chmod(0o600); print("Quiz private Localizer client configured")'
```

## Step C: enable the Localizer network first

```bash
cd /srv/ai-localizer/source
git status --short
git pull --ff-only origin feature/vps-migration-isolated-20261114
sudo docker compose config --quiet
sudo docker compose up -d --no-deps api
sudo docker compose ps
```

Expected: Localizer `Up (healthy)`. Its API remains private. Missing external network would cause Compose to fail: create it in Step A first.

## Step D: update Quiz code + attach private network

From Quiz repository, only if working tree is clean and feature branch exists on GitHub:

```bash
cd /srv/ai-quiz-game/source
git status --short
git fetch origin
git switch --track origin/feature/vps-localizer-internal-20261114
sudo docker compose config --quiet
sudo docker compose build backend
sudo docker compose up -d --no-deps backend
sudo docker compose ps
```

CAUTION: Only the Quiz backend container gets recreated, which can interrupt active API requests. The PostgreSQL volume and web container are not restarted or deleted.

## Step E: smoke checks (no paid model call)

From the Quiz backend container, verify direct private endpoint (without leaking token):

```bash
cd /srv/ai-quiz-game/source
sudo docker compose exec -T backend python -c 'import os,httpx; k=os.environ["AI_LOCALIZER_API_KEY"]; u=os.environ["AI_LOCALIZER_BASE_URL"]+"/translate"; r=httpx.post(u,json={"dictionary":{"hello":"Hello"},"language_code":"en"},headers={"Authorization":"Bearer "+k},timeout=10,trust_env=False); print("status",r.status_code,"fallback",r.json().get("fallback"))'
```

Expected: `status 200 fallback False`. This is English pass-through, not an OpenAI call.

Check no-auth still denied, and Caddy still returns public 403:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://v2202611457040557919.quicksrv.de/translate -H 'Content-Type: application/json' --data '{"dictionary":{"x":"Hello"},"language_code":"en"}'
```

Expected public: `403`. Then trigger a **legitimate UI language whose current cache has missing keys** and check `GET /api/localization/dictionary/<locale>` returns `missing_count=0` / `fallback=false`. Cached locales will not call Localizer; **do not delete production translation rows to force a test**. For fresh locales, translating will incur a real OpenAI API cost.

## Rollback

If integration fails, keep existing Quiz DB and service image. In `/srv/ai-quiz-game/source/.env`, set `AI_LOCALIZER_BASE_URL=https://ai-localizer-api.onrender.com`, leave the Quiz internal bearer key in .env (the client deliberately never forwards it to HTTPS legacy endpoints), and re-create only backend with `sudo docker compose up -d --no-deps backend`. Note that restoring old code via master may also remove the private network attachment; the network can remain unused safely. Never run `docker compose down -v`.

Do not change the public Angular Localizer frontend, Caddy routes or Quiz database.
