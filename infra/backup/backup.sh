#!/usr/bin/env bash
# Two backups, because they answer two different questions.
#
#   portable  `lab backup create` — a logical dump, every object copied through
#             the application's own store port, and a manifest recording the app
#             version, Alembic revision, PostgreSQL version, extension versions
#             and per-workspace counts. Restores onto a VM nobody has
#             provisioned yet. This is the one that makes a move possible, and
#             it backs up an S3 installation exactly as well as a local one.
#
#   physical  `pg_basebackup` plus the archived WAL — point-in-time recovery to
#             any moment since the base, which is what the 15-minute RPO target
#             actually rests on. It only restores onto compatible
#             infrastructure: same major version, same platform, same page
#             layout. It is the fast path, not the portable one.
#
# The predecessor had only the physical half, plus an `rsync` of a local objects
# directory — which is not a backup of a bucket at all — and recorded nothing
# that a restore could check itself against.
#
# Safe to run twice: each run writes its own timestamped directory and never
# mutates an existing one. Prints no secrets, ever: the password reaches
# PostgreSQL through the environment, never on a command line where `ps` would
# show it.
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/lab-assistant}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_ROOT}/${STAMP}"
COMPOSE=(docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml)
# Set to 0 on a deployment that has accepted a 24-hour RPO and does not archive WAL.
WITH_PHYSICAL="${WITH_PHYSICAL:-1}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

mkdir -p "${TARGET}"

# ── the portable backup ─────────────────────────────────────────────────────
# Runs in the api container: it needs the application's configuration to know
# which object store to read, and the PostgreSQL client tools to take the dump.
log "portable backup -> ${TARGET}/portable"
"${COMPOSE[@]}" run --rm \
  -v "${TARGET}:/backup" \
  api lab backup create /backup/portable

log "verifying what was just written"
"${COMPOSE[@]}" run --rm \
  -v "${TARGET}:/backup" \
  api lab backup verify /backup/portable

# ── the physical backup ─────────────────────────────────────────────────────
if [[ "${WITH_PHYSICAL}" == "1" ]]; then
  : "${PGHOST:?set PGHOST, or set WITH_PHYSICAL=0 to take the portable backup alone}"
  : "${PGUSER:?set PGUSER}"
  : "${PGPASSWORD:?set PGPASSWORD (from the secret manager, not from a file in the repo)}"

  log "base backup -> ${TARGET}/pgdata (point-in-time recovery)"
  pg_basebackup --pgdata="${TARGET}/pgdata" --wal-method=stream --checkpoint=fast --progress
else
  log "physical backup skipped (WITH_PHYSICAL=0): recovery is to the portable backup's point"
fi

log "recording provenance"
{
  printf 'taken_at=%s\n' "${STAMP}"
  printf 'portable=%s\n' "portable/MANIFEST.json"
  printf 'physical=%s\n' "$([[ "${WITH_PHYSICAL}" == "1" ]] && echo pgdata || echo none)"
  printf 'deployed_commit=%s\n' "$(git -C "${GIT_ROOT:-/opt/lab-assistant}" rev-parse HEAD 2>/dev/null || echo unknown)"
} > "${TARGET}/MANIFEST"

# `.env` is deliberately absent: it belongs in the secret manager, and the
# portable manifest lists which entries a restore will need by name. An archive
# that carries the database password makes every off-site copy a credential leak.
log "pruning backups older than ${KEEP_DAYS} days"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} +

log "done: ${TARGET}"
log "this holds password hashes, unreviewed evidence and chat history."
log "it belongs in encrypted off-site storage, not on this VM."
