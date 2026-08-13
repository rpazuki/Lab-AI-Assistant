#!/usr/bin/env bash
# Restore the control plane and object store, then verify before serving.
#
# Refuses to start the API: verification comes first. A restore nobody has verified
# is a hypothesis, and the point of verifying is to find out on a scratch VM rather
# than from a researcher reading a broken citation.
set -euo pipefail

BACKUP_DIR="${1:-}"
WORKSPACE="${WORKSPACE:-}"
COMPOSE=(docker compose -f docker-compose.yml -f infra/compose/docker-compose.prod.yml)

usage() {
  cat >&2 <<'USAGE'
usage: WORKSPACE=<slug> restore.sh /var/backups/lab-assistant/<stamp> [recovery_target_time]

  recovery_target_time  optional ISO-8601 timestamp for point-in-time recovery.
                        Omit to recover to the end of the archived WAL.
USAGE
  exit 2
}

[[ -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" ]] || usage
[[ -n "${WORKSPACE}" ]] || usage
TARGET_TIME="${2:-}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

log "stopping the stack"
"${COMPOSE[@]}" down

log "restoring pgdata from ${BACKUP_DIR}"
docker run --rm \
  -v lab-assistant_pgdata:/target \
  -v "${BACKUP_DIR}/pgdata:/source:ro" \
  alpine:3 sh -c 'rm -rf /target/* && cp -a /source/. /target/'

if [[ -n "${TARGET_TIME}" ]]; then
  log "setting recovery target ${TARGET_TIME}"
  docker run --rm -v lab-assistant_pgdata:/target alpine:3 sh -c \
    "printf 'recovery_target_time = %s\nrecovery_target_action = promote\n' \"'${TARGET_TIME}'\" >> /target/postgresql.auto.conf && touch /target/recovery.signal"
fi

# At or after the database's recovery point, never before it.
log "restoring the object store"
docker run --rm \
  -v lab-assistant_objects:/target \
  -v "${BACKUP_DIR}/objects:/source:ro" \
  alpine:3 sh -c 'cp -a /source/. /target/'

log "starting the database"
"${COMPOSE[@]}" up -d db

log "applying migrations (a no-op when the backup was current)"
"${COMPOSE[@]}" run --rm api alembic upgrade head

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

log "verified. Put .env back from the secret manager, then bring up the rest:"
log "  ${COMPOSE[*]} up -d"
