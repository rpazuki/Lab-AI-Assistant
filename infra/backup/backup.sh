#!/usr/bin/env bash
# Base backup of the control plane plus a snapshot of the object store.
#
# Safe to run twice: each run writes its own timestamped directory and never mutates
# an existing one. Prints no secrets, ever — the password is passed to PostgreSQL
# through the environment, never on a command line where `ps` would show it.
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/lab-assistant}"
OBJECT_ROOT="${OBJECT_ROOT:-/var/lib/lab-assistant/objects}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_ROOT}/${STAMP}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

: "${PGHOST:?set PGHOST}"
: "${PGUSER:?set PGUSER}"
: "${PGPASSWORD:?set PGPASSWORD (from the secret manager, not from a file in the repo)}"

mkdir -p "${TARGET}"

log "base backup -> ${TARGET}/pgdata"
pg_basebackup --pgdata="${TARGET}/pgdata" --wal-method=stream --checkpoint=fast --progress

# The object store first, then the database's own recovery point, so the store is
# never behind the control plane: a missing object breaks a citation, a spare one
# does not.
log "object store -> ${TARGET}/objects"
rsync --archive --delete --quiet "${OBJECT_ROOT}/" "${TARGET}/objects/"

log "recording provenance"
{
  printf 'taken_at=%s\n' "${STAMP}"
  printf 'pg_host=%s\n' "${PGHOST}"
  printf 'object_root=%s\n' "${OBJECT_ROOT}"
  printf 'deployed_commit=%s\n' "$(git -C "${GIT_ROOT:-/opt/lab-assistant}" rev-parse HEAD 2>/dev/null || echo unknown)"
} > "${TARGET}/MANIFEST"

# `.env` is deliberately absent: it belongs in the secret manager. An archive that
# carries the database password makes every off-site copy a credential leak.
log "pruning backups older than ${KEEP_DAYS} days"
find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} +

log "done: ${TARGET}"
