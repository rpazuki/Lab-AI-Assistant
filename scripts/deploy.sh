#!/usr/bin/env bash
# Deploy: kept as an entry point, and now only that.
#
#   sudo bash scripts/deploy.sh [--check] [--first-install]
#
# This used to fast-forward, rebuild changed services, migrate, and on a failed
# health check `git reset --hard` back to the previous commit. That rollback was
# the defect audit F14 names: after a migration, the previous code runs against a
# schema it has never seen, which is worse than a stopped service. The supported
# updater is `scripts/deploy/update.sh`, which never rolls back; this hands to it.
# `--dry-run` is accepted as the old spelling of `--check`.
set -euo pipefail
args=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) args+=(--check) ;;
    --ref|--all) printf 'error: %s is no longer supported; deploy the checked-out branch, see scripts/deploy/update.sh --help\n' "$arg" >&2; exit 2 ;;
    *) args+=("$arg") ;;
  esac
done
exec bash "$(dirname "${BASH_SOURCE[0]}")/deploy/update.sh" "${args[@]}"
