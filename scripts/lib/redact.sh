#!/usr/bin/env bash
# Secret removal for support bundles, plus a verification pass.
#
# A bundle is handed to someone outside the deployment. Redaction runs, and then a
# separate pass *checks* that redaction worked — because a bundle that leaks a
# token is worse than no bundle.
set -euo pipefail

# Patterns replaced in any collected text.
redact_stream() {
  sed -E \
    -e 's/(Bearer )[A-Za-z0-9._~+/=-]{8,}/\1[REDACTED]/g' \
    -e 's/(eyJ[A-Za-z0-9_-]{6,})\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED_JWT]/g' \
    -e 's/(sk-[A-Za-z0-9]{8,})/[REDACTED_API_KEY]/g' \
    -e 's/((password|secret|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*)[^[:space:],;"]+/\1[REDACTED]/gI' \
    -e 's#(postgresql(\+asyncpg)?://[^:]+:)[^@]+@#\1[REDACTED]@#g' \
    -e 's/([?&](api_key|apikey|token|email)=)[^&[:space:]]+/\1[REDACTED]/gI' \
    -e 's/(Set-Cookie:[[:space:]]*lab_session=)[^;[:space:]]+/\1[REDACTED]/gI'
}

# Refuse to hand over a bundle that still contains something secret-shaped.
verify_redacted() {
  local target="$1" findings
  findings="$(grep -rEil \
    -e 'Bearer [A-Za-z0-9._~+/=-]{8,}' \
    -e 'eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+\.' \
    -e 'sk-[A-Za-z0-9]{16,}' \
    -e '(password|secret|api[_-]?key)[[:space:]]*[:=][[:space:]]*[^[:space:][REDACTED]]{8,}' \
    "$target" 2>/dev/null || true)"
  if [[ -n "$findings" ]]; then
    printf 'redaction verification FAILED in:\n%s\n' "$findings" >&2
    return 1
  fi
  return 0
}
