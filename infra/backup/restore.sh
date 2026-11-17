#!/usr/bin/env bash
# Restore, then verify, then — and only then — serve.
#
# Two routes, matching the two backups `backup.sh` takes:
#
#   portable  the default. `lab backup restore` checks the backup's manifest
#             against this destination first — app version, Alembic revision,
#             PostgreSQL major, extension versions — and refuses before touching
#             anything when they do not agree. It also decides what this restore
#             *is*: a clone that may not publish, or a fenced takeover.
#
#   physical  `--physical`, for point-in-time recovery onto compatible
#             infrastructure. This is the route that writes into Docker volumes,
#             and it is the one that needed the most repair: the script this
#             replaces named `lab-assistant_pgdata` literally and ran
#             `rm -rf /target/*` on whatever it found there.
#
# Neither route starts the API. Verification comes first: a restore nobody has
# verified is a hypothesis, and the point is to find out on a scratch VM rather
# than from a researcher reading a broken citation.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/volumes.sh
source "${HERE}/lib/volumes.sh"

COMPOSE=(docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml)
MODE="portable"
INTENT="${INTENT:-clone}"
BACKUP_DIR=""
TARGET_TIME=""

usage() {
  cat >&2 <<'USAGE'
usage: WORKSPACE=<slug> restore.sh [--physical] <backup-dir> [recovery_target_time]

  <backup-dir>          a directory written by backup.sh. The portable route
                        reads <backup-dir>/portable; --physical reads
                        <backup-dir>/pgdata.

  recovery_target_time  --physical only: ISO-8601 timestamp for point-in-time
                        recovery. Omit to recover to the end of the archived WAL.

environment:
  WORKSPACE   required; the slug `lab db verify-restore` checks.
  INTENT      clone (default) or replace. `replace` takes over the original
              installation's identity and its right to publish, and requires
              ORIGIN_IS_FENCED=1 — meaning you have stopped the original. Two
              publishers on one library diverge silently, which is why this is
              a deliberate word rather than a default.
  PGDATA_VOLUME, OBJECTS_VOLUME
              --physical only; set to name the target volumes explicitly when
              this host runs more than one deployment.
USAGE
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --physical) MODE="physical"; shift ;;
    --help|-h) usage ;;
    *) if [[ -z "${BACKUP_DIR}" ]]; then BACKUP_DIR="$1"; else TARGET_TIME="$1"; fi; shift ;;
  esac
done

[[ -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" ]] || usage
[[ -n "${WORKSPACE:-}" ]] || usage
BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

verify_before_serving() {
  log "verifying: audit chain, manifest digests, cited objects, alias"
  if ! "${COMPOSE[@]}" run --rm api lab db verify-restore --workspace "${WORKSPACE}"; then
    cat >&2 <<'FAILED'

Verification failed. Do NOT start the API.
Every problem it printed must be explained before this deployment serves anything:
see docs/operations/backup-and-restore.md and, if the audit chain broke,
docs/operations/runbooks/audit-chain-failure.md.
FAILED
    exit 1
  fi
}

if [[ "${MODE}" == "portable" ]]; then
  [[ -d "${BACKUP_DIR}/portable" ]] || {
    echo "no portable backup at ${BACKUP_DIR}/portable" >&2
    exit 2
  }

  restore_flags=(--intent "${INTENT}")
  if [[ "${INTENT}" == "replace" ]]; then
    [[ "${ORIGIN_IS_FENCED:-0}" == "1" ]] || {
      cat >&2 <<'UNFENCED'
INTENT=replace takes over the original installation's right to publish.

Stop the original installation first, then re-run with ORIGIN_IS_FENCED=1 to
assert that you have. This is refused rather than prompted because two
publishers on one library do not fail — they both work, and diverge.
UNFENCED
      exit 2
    }
    restore_flags+=(--origin-is-fenced)
  fi

  log "bringing up the database alone"
  "${COMPOSE[@]}" up -d db

  log "preflight: does this backup fit this destination?"
  "${COMPOSE[@]}" run --rm -v "${BACKUP_DIR}:/backup:ro" api \
    lab backup restore /backup/portable "${restore_flags[@]}"

  log "restoring"
  "${COMPOSE[@]}" run --rm -v "${BACKUP_DIR}:/backup:ro" api \
    lab backup restore /backup/portable "${restore_flags[@]}" --apply

  log "applying migrations (a no-op when the backup was current)"
  "${COMPOSE[@]}" run --rm api alembic upgrade head

  verify_before_serving
  log "verified. Put the secrets back from the secret manager — the manifest's"
  log "secret_references lists them by name — then bring up the rest:"
  log "  ${COMPOSE[*]} up -d"
  exit 0
fi

# ── physical route ──────────────────────────────────────────────────────────
log "stopping the stack"
"${COMPOSE[@]}" down

# Resolved, never guessed. `resolve_volume` fails when there is no match or more
# than one; a second deployment on this host is exactly when the old hardcoded
# name sent a restore into the wrong installation.
PGDATA_VOLUME="${PGDATA_VOLUME:-$(resolve_volume pgdata)}"
OBJECTS_VOLUME="${OBJECTS_VOLUME:-$(resolve_volume objects)}"
log "target volumes: ${PGDATA_VOLUME}, ${OBJECTS_VOLUME}"

# A volume that already holds data belongs to a running installation. The old
# script's `rm -rf /target/*` is how that installation stopped existing.
require_fresh_volume "${PGDATA_VOLUME}"

log "restoring pgdata from ${BACKUP_DIR}/pgdata"
docker run --rm \
  -v "${PGDATA_VOLUME}:/target" \
  -v "${BACKUP_DIR}/pgdata:/source:ro" \
  alpine:3 sh -c 'cp -a /source/. /target/'

if [[ -n "${TARGET_TIME}" ]]; then
  log "setting recovery target ${TARGET_TIME}"
  docker run --rm -v "${PGDATA_VOLUME}:/target" alpine:3 sh -c \
    "printf 'recovery_target_time = %s\nrecovery_target_action = promote\n' \"'${TARGET_TIME}'\" >> /target/postgresql.auto.conf && touch /target/recovery.signal"
fi

# At or after the database's recovery point, never before it: a row citing an
# object the store does not have is a broken citation, a spare object is not.
if [[ -d "${BACKUP_DIR}/objects" ]]; then
  log "restoring the object store"
  docker run --rm \
    -v "${OBJECTS_VOLUME}:/target" \
    -v "${BACKUP_DIR}/objects:/source:ro" \
    alpine:3 sh -c 'cp -a /source/. /target/'
else
  log "no objects directory in this backup; restoring them from the portable copy"
  "${COMPOSE[@]}" up -d db
  "${COMPOSE[@]}" run --rm -v "${BACKUP_DIR}:/backup:ro" api \
    lab backup restore /backup/portable --intent "${INTENT}" --apply
fi

log "starting the database"
"${COMPOSE[@]}" up -d db

log "applying migrations (a no-op when the backup was current)"
"${COMPOSE[@]}" run --rm api alembic upgrade head

verify_before_serving
log "verified. Put .env back from the secret manager, then bring up the rest:"
log "  ${COMPOSE[*]} up -d"
