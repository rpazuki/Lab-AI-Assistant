#!/usr/bin/env bash
# Shared helpers for the operations scripts.
#
# Ported deliberately from the predecessor's `scripts/` — deploy, restart, status,
# logs, and the redacted support bundle encode real operational knowledge, and
# rediscovering it would be waste (delivery §5, epic E14).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILES=(-f "${REPO_ROOT}/docker-compose.yml" -f "${REPO_ROOT}/infra/compose/docker-compose.prod.yml")

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

compose() {
  docker compose "${COMPOSE_FILES[@]}" "$@"
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

readiness_check() {
  local body
  body="$(compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5).read().decode())" 2>/dev/null || true)"
  [[ "$body" == *'"status": "ok"'* || "$body" == *'"status":"ok"'* ]]
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
