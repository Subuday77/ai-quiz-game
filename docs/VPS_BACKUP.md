# VPS PostgreSQL backups (Quiz Game)

## Verified recovery baseline — 2026-11-15

A manual custom-format dump of database `quiz` was created using the running
PostgreSQL 17 container on the netcup VPS and copied over SFTP to Windows:
`quiz-backup-20261115T203559Z.dump` (about 404 KiB). Its SHA-256 matched
between VPS and Windows. `pg_restore --list` succeeded. A full restore into
an isolated temporary PostgreSQL 17 container succeeded (`RESTORE OK`), with
2 users, 219 questions and Alembic revision `0014_attempt_choice_order`.
The temporary restore container can be removed after verification.

This proves that **that specific snapshot** was restorable. It does **not**
mean scheduled backups or scheduled off-site replication are already working.

## Automated local dump (prepared; installation must be confirmed)

Files in `ops/backup/` implement a daily **03:15 UTC** systemd timer (with up
to 10 minutes of random delay), targeting the unchanged Docker Compose project
at `/srv/ai-quiz-game/source`.

Each run:
1. Writes a `pg_dump -Fc` to a mode-0600 temporary file in
   `/home/deploy/quiz-backups/`.
2. Verifies it is nonempty and readable with `pg_restore --list`.
3. Atomically publishes `quiz-backup-YYYYMMDDTHHMMSSZ.dump`, without
   overwriting an existing snapshot, and writes a SHA-256 sidecar.
4. Prints only the backup filename, size and sidecar path; no DB credentials.

No working DB, container, migration, application `.env`, or Compose network is
changed. No automatic deletion is enabled **until off-VPS backup replication
and retention are configured and verified**. Monitor the free disk space.

### Install without switching the running Quiz Git branch

Run as the `deploy` SSH account on the VPS **only after** these files are merged
into `origin/master`:

```bash
cd /srv/ai-quiz-game/source
git fetch origin master
sudo install -d -m 0700 -o deploy -g deploy /home/deploy/quiz-backups

git show origin/master:ops/backup/quiz-pg-backup.sh | sudo tee /usr/local/sbin/quiz-pg-backup >/dev/null
sudo chown root:root /usr/local/sbin/quiz-pg-backup
sudo chmod 0755 /usr/local/sbin/quiz-pg-backup

git show origin/master:ops/backup/quiz-pg-backup.service | sudo tee /etc/systemd/system/quiz-pg-backup.service >/dev/null
git show origin/master:ops/backup/quiz-pg-backup.timer | sudo tee /etc/systemd/system/quiz-pg-backup.timer >/dev/null
sudo chown root:root /etc/systemd/system/quiz-pg-backup.{service,timer}
sudo chmod 0644 /etc/systemd/system/quiz-pg-backup.{service,timer}

sudo systemctl daemon-reload
sudo systemctl start quiz-pg-backup.service
sudo systemctl status quiz-pg-backup.service --no-pager
sudo systemctl enable --now quiz-pg-backup.timer
sudo systemctl list-timers quiz-pg-backup.timer --no-pager
ls -lh /home/deploy/quiz-backups/
```

Stop and inspect the service logs if `status` is not `success`:
`sudo journalctl -u quiz-pg-backup.service -n 80 --no-pager`.
Do **not** use `docker compose down -v`, drop databases, or touch the
`quiz_pgdata` volume for backup setup.

### Validate a scheduled backup

```bash
cd /home/deploy/quiz-backups
sha256sum -c "$(ls -1t quiz-backup-*.dump.sha256 | head -n 1)"
```

The checksum check verifies file consistency, **not** restorable SQL contents.
Continue occasional isolated restore drills on fresh temporary databases.

## Important release gate: external automation still pending

`/home/deploy/quiz-backups/` is on the *same VPS*. The separately copied
Windows snapshot is a real off-host copy but **not an automated off-host
backup plan**. Before taking real player data, select and set up an independent
off-host destination (for example, encrypted SFTP/object storage or an online
Windows device that pulls backups), verify secure credentials, transfer,
retention, monitoring/alerts, and repeat an isolated restore from the *remote*
copy. Do not place dumps in GitHub, chat or a public bucket. Do not
invent a cloud location or silently expose database files.
