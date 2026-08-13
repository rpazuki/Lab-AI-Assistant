#!/usr/bin/env bash
# Live unified log view.
#
#   scripts/logs.sh [service ...] [--since 10m] [--grep pattern]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/redact.sh"

SINCE="10m"; PATTERN=""; SERVICES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --grep) PATTERN="$2"; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) SERVICES+=("$1"); shift ;;
  esac
done

# Redacted even in an interactive view: an operator sharing a screenshot is the
# most common way a token escapes.
if [[ -n "$PATTERN" ]]; then
  compose logs -f --since "$SINCE" "${SERVICES[@]}" | redact_stream | grep --line-buffered -E "$PATTERN"
else
  compose logs -f --since "$SINCE" "${SERVICES[@]}" | redact_stream
fi
