#!/usr/bin/env bash
# Shared helpers for the operations scripts.
#
# Ported deliberately from the predecessor's `scripts/` — deploy, restart, status,
# logs, and the redacted support bundle encode real operational knowledge, and
# rediscovering it would be waste (delivery §5, epic E14).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# One setting's effective value across .env, .env.server and .env.site, the last
# file winning as it does for Compose. For role and addresses, which may live in
# any of the three; `env_value` below reads .env alone, for secrets.
site_value() {
  local file found value=""
  for file in "${REPO_ROOT}/.env" "${REPO_ROOT}/.env.server" "${REPO_ROOT}/.env.site"; do
    [[ -f "$file" ]] || continue
    found="$(sed -n "s/^$1=//p" "$file" | tail -n1)"
    [[ -n "$found" ]] && value="$found"
  done
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

# A reader runs no workers and no scanner (infra/compose/docker-compose.reader.yml),
# so every script that names services takes them from APP_SERVICES.
INSTALLATION_ROLE="$(site_value LAB_INSTALLATION_ROLE)"
INSTALLATION_ROLE="${INSTALLATION_ROLE:-publisher}"
COMPOSE_FILES=(-f "${REPO_ROOT}/docker-compose.yml" -f "${REPO_ROOT}/infra/compose/docker-compose.prod.yml")
if [[ "$INSTALLATION_ROLE" == reader ]]; then
  COMPOSE_FILES+=(-f "${REPO_ROOT}/infra/compose/docker-compose.reader.yml")
  APP_SERVICES=(api web)
else
  APP_SERVICES=(api web worker-network worker-compute)
fi

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# The project is named, as the deploy script names it. Left to Compose, it would
# be the checkout's directory name, and a clone called anything but
# `lab-assistant` would have every script here looking at an empty project.
compose() {
  docker compose -p "${COMPOSE_PROJECT_NAME:-lab-assistant}" "${COMPOSE_FILES[@]}" "$@"
}

# Read one key from .env without sourcing the file. The file holds no addresses,
# so sourcing it is safe — but a script that only needs one value should not
# import twenty.
env_value() {
  local key="$1" file="${REPO_ROOT}/.env"
  [[ -f "$file" ]] || die "missing ${file}; copy .env.example and fill it in"
  sed -n "s/^${key}=//p" "$file" | tail -n1
}

require_env() {
  local key="$1"
  [[ -n "$(env_value "$key")" ]] || die "${key} is not set in .env"
}

# Which services a changed path affects. Rebuilding everything on every deploy
# wastes minutes the VM does not have.
services_for_paths() {
  local -A affected=()
  local path
  while read -r path; do
    case "$path" in
      packages/python/*|services/api/*) affected[api]=1; affected[worker-network]=1; affected[worker-compute]=1 ;;
      workers/*)                        affected[worker-network]=1; affected[worker-compute]=1 ;;
      apps/web/*)                       affected[web]=1 ;;
      database/*)                       affected[api]=1 ;;
      docker-compose.yml|infra/*)       affected[api]=1; affected[web]=1; affected[worker-network]=1; affected[worker-compute]=1; affected[scanner]=1 ;;
    esac
  done
  printf '%s\n' "${!affected[@]}"
}

health_check() {
  local attempts="${1:-30}"
  for ((i = 1; i <= attempts; i++)); do
    if compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Readiness decides whether a deploy proceeds or rolls back, so it reads the
# status code rather than matching a string in the body: 200 means serve, 503
# means do not. It also prints which check failed, because "not ready" on its
# own sends an operator to the logs to find out what this already knows.
readiness_check() {
  compose exec -T api python - <<'READY' 2>&1 >/dev/null
import json
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=5) as response:
        body = json.loads(response.read().decode())
        code = response.status
except urllib.error.HTTPError as error:  # 503: running, and not fit to serve
    code = error.code
    try:
        body = json.loads(error.read().decode())
    except Exception:
        body = {}
except Exception as error:  # no answer at all
    print(f"not ready: {type(error).__name__}", file=sys.stderr)
    raise SystemExit(1)

if code == 200:
    raise SystemExit(0)

for check in body.get("checks", []):
    if not check.get("ok") and check.get("blocks_serving", True):
        print(f"not ready: {check['name']}: {check.get('detail')}", file=sys.stderr)
raise SystemExit(1)
READY
}

deployed_commit() {
  git -C "$REPO_ROOT" rev-parse --short HEAD
}

# Check the actual prefixed page and its CSS; a running Next process can still
# serve 404 everywhere when its runtime configuration was omitted from the image.
web_check() {
  compose exec -T web node - <<'JS'
(async () => {
  const base = (process.env.APP_BASE_PATH || '/assist').replace(/\/$/, '');
  const origin = 'http://127.0.0.1:3002';
  const page = await fetch(`${origin}${base}/login`, { signal: AbortSignal.timeout(15000) });
  if (!page.ok) throw new Error(`login returned ${page.status}`);
  const html = await page.text();
  const styles = [...html.matchAll(/href="([^"]+\.css[^\"]*)"/g)];
  if (!styles.length) throw new Error('login page has no stylesheet');
  for (const [, path] of styles) {
    const css = await fetch(new URL(path.replaceAll('&amp;', '&'), origin), { signal: AbortSignal.timeout(15000) });
    if (!css.ok || !css.headers.get('content-type')?.includes('text/css')) {
      throw new Error(`stylesheet failed: ${path} (${css.status})`);
    }
  }
  console.log('Web login page and stylesheets are healthy');
})().catch(error => { console.error(error.message); process.exit(1); });
JS
}
