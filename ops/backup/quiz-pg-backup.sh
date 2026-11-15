#!/usr/bin/env bash
# Create an atomic, validated PostgreSQL custom-format dump without changing Quiz data.
set -Eeuo pipefail
umask 077

COMPOSE_DIR=/srv/ai-quiz-game/source
BACKUP_DIR=/home/deploy/quiz-backups

if (( EUID != 0 )); then
  echo "ERROR: run via the root-owned systemd service (or sudo)." >&2
  exit 1
fi
if [[ ! -f "$COMPOSE_DIR/compose.yaml" ]]; then
  echo "ERROR: Quiz Compose config missing: $COMPOSE_DIR/compose.yaml" >&2
  exit 1
fi
if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "ERROR: backup directory missing; install it as deploy:deploy mode 0700 first." >&2
  exit 1
fi

cd "$COMPOSE_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="quiz-backup-${stamp}.dump"
archive="$BACKUP_DIR/$name"
partial="$(mktemp "$BACKUP_DIR/.quiz-backup-${stamp}.XXXXXXXX.partial")"
hash_partial=''
trap 'rm -f -- "$partial" "$hash_partial"' EXIT

# Pipe only the dump's stdout into the restricted temporary file. Failure must
# stop the script, rather than publish a truncated backup as successful.
docker compose exec -T db pg_dump -U quiz -d quiz -Fc > "$partial"
test -s "$partial"
# A catalog check is NOT a restore drill, but catches malformed/incomplete dumps.
docker compose exec -T db pg_restore --list < "$partial" > /dev/null

# Hard-link within the same filesystem publishes without overwriting an older
# archive, even if two invocations happen in the same second.
ln -- "$partial" "$archive"
rm -- "$partial"
partial=''
hash_partial="$(mktemp "$BACKUP_DIR/.quiz-checksum-${stamp}.XXXXXXXX.partial")"
(
  cd "$BACKUP_DIR"
  sha256sum "$name"
) > "$hash_partial"
ln -- "$hash_partial" "$archive.sha256"
rm -- "$hash_partial"
hash_partial=''
chmod 0600 "$archive" "$archive.sha256"
chown deploy:deploy "$archive" "$archive.sha256"
printf 'BACKUP OK: %s (%s bytes); checksum: %s\n' \
  "$archive" "$(stat -c %s "$archive")" "$archive.sha256"

# Intentionally NO pruning: an off-VPS retention policy must be in place first.
