#!/usr/bin/env bash
# Build a redacted support bundle for a bug report.
#
#   scripts/collect-logs.sh [--since 2h] [--out /tmp]
#
# Redaction runs, then a verification pass checks it worked. If verification
# fails, the bundle is deleted rather than handed over.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/redact.sh"

SINCE="2h"; OUT_DIR="/tmp"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown flag $1" ;;
  esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
BUNDLE="${OUT_DIR}/lab-assistant-support-${STAMP}.tar.gz"
trap 'rm -rf "$WORK"' EXIT

log "collecting into ${WORK}"
{
  printf 'collected_at: %s\n' "$STAMP"
  printf 'commit: %s\n' "$(deployed_commit)"
  printf 'environment: %s\n' "$(env_value LAB_ENV)"
  docker --version
  docker compose version
  uname -a
} > "${WORK}/environment.txt"

compose ps > "${WORK}/containers.txt" 2>&1 || true
for service in "${APP_SERVICES[@]}" db; do
  compose logs --since "$SINCE" --no-color "$service" 2>&1 | redact_stream > "${WORK}/${service}.log" || true
done

# Effective configuration, already redacted by the application itself.
curl -fsS http://localhost:8000/api/v1/config 2>/dev/null | redact_stream > "${WORK}/effective-config.json" || true
curl -fsS http://localhost:8000/ready 2>/dev/null > "${WORK}/ready.json" || true

# Schema state and queue depth: the two questions every report needs answered.
compose exec -T db psql -U "$(env_value POSTGRES_USER)" -d "$(env_value POSTGRES_DB)" \
  -c '\dt' -c 'select stage, state, count(*) from tasks group by 1,2 order by 1,2' \
  > "${WORK}/database.txt" 2>&1 || true

df -h > "${WORK}/disk.txt" 2>&1 || true

log "verifying redaction"
if ! verify_redacted "$WORK"; then
  rm -rf "$WORK"
  die "redaction verification failed; no bundle was produced"
fi

tar -czf "$BUNDLE" -C "$WORK" .
log "bundle written to ${BUNDLE}"
printf 'Attach this file to the report. It contains no credentials, no document\n'
printf 'content, and no prompts; see docs/operations/support-bundles.md.\n'
