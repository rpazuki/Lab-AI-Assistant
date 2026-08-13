#!/usr/bin/env bash
# Bounce or recreate services without touching git.
#
#   scripts/restart.sh [service ...] [--recreate]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

RECREATE=0; SERVICES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --recreate) RECREATE=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) SERVICES+=("$1"); shift ;;
  esac
done
[[ ${#SERVICES[@]} -gt 0 ]] || SERVICES=(api web worker-network worker-compute)

if [[ "$RECREATE" == 1 ]]; then
  log "recreating ${SERVICES[*]}"
  compose up -d --force-recreate "${SERVICES[@]}"
else
  log "restarting ${SERVICES[*]}"
  compose restart "${SERVICES[@]}"
fi
health_check && log "healthy" || warn "health check did not pass"
