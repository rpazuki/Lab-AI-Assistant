#!/usr/bin/env bash
# Deploy: fast-forward from GitHub, rebuild only what changed, verify, roll back on
# failure.
#
#   scripts/deploy.sh [--ref main] [--all] [--dry-run]
#
# Rolling back on a failed health check is the whole point: a deploy that leaves
# the VM serving a broken build is worse than one that refuses to finish.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

REF="main"; REBUILD_ALL=0; DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --all) REBUILD_ALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown flag $1" ;;
  esac
done

require_env POSTGRES_PASSWORD
require_env LAB_SECRET_KEY

PREVIOUS="$(deployed_commit)"
log "deployed commit is ${PREVIOUS}"

log "fetching ${REF}"
git -C "$REPO_ROOT" fetch --quiet origin "$REF"
if ! git -C "$REPO_ROOT" merge-base --is-ancestor HEAD "origin/${REF}"; then
  die "HEAD is not an ancestor of origin/${REF}; refusing a non-fast-forward deploy"
fi

CHANGED="$(git -C "$REPO_ROOT" diff --name-only "HEAD..origin/${REF}")"
if [[ -z "$CHANGED" ]]; then
  log "already up to date"
  exit 0
fi

if [[ "$REBUILD_ALL" == 1 ]]; then
  SERVICES=(api web worker-network worker-compute scanner)
else
  mapfile -t SERVICES < <(printf '%s\n' "$CHANGED" | services_for_paths)
fi
[[ ${#SERVICES[@]} -gt 0 ]] || SERVICES=(api)

log "changed paths affect: ${SERVICES[*]}"
if [[ "$DRY_RUN" == 1 ]]; then
  printf '%s\n' "$CHANGED"
  exit 0
fi

git -C "$REPO_ROOT" merge --ff-only "origin/${REF}"
NEW="$(deployed_commit)"
log "now at ${NEW}"

rollback() {
  warn "rolling back to ${PREVIOUS}"
  git -C "$REPO_ROOT" reset --hard "$PREVIOUS" >/dev/null
  compose up -d --build api web worker-network worker-compute
  health_check || warn "the rolled-back stack is also unhealthy; escalate"
  die "deploy failed and was rolled back"
}

log "building ${SERVICES[*]}"
compose build "${SERVICES[@]}" || rollback

if printf '%s\n' "$CHANGED" | grep -q '^database/'; then
  log "applying database migrations"
  compose run --rm api sh -lc 'cd /app/database && alembic upgrade head' || rollback
fi

log "starting services"
compose up -d --wait --wait-timeout 900 "${SERVICES[@]}" || rollback

log "waiting for health"
health_check || rollback
readiness_check || rollback
web_check || rollback

log "deployed ${NEW} (services: ${SERVICES[*]})"
