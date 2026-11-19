#!/usr/bin/env bash
# Deploy or update Lab AI Assistant on the shared Red Hat server. Run as root.
# Every function is parsed before `git pull`, so pulling a new version of this
# file cannot change the run that pulled it.
#
# Modelled on the Protocols Analysis updater beside it on the same server, and
# for the same reasons: one updater per checkout, a fast-forward pull only,
# migrations run explicitly, and **no automatic rollback** — after a migration,
# old code on a new schema is a worse state than a stopped service (audit F14).
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: sudo bash scripts/deploy/update.sh [options]
  --check            Preflight only: validate the checkout, secrets, Compose
                     configuration and the running database, and fetch to show
                     what would deploy. Builds, stops and migrates nothing.
  --first-install    Also start the database and scanner when they are not running
                     yet. Needed once, on the first deployment of this checkout.
  --repo PATH        Checkout (default: the repository containing this script)
  --user NAME        Checkout owner, who runs git (default: labassistant)
  --project NAME     Compose project (default: lab-assistant)
  --api-port N       Loopback API port (default: 8100, LAB_API_HOST_PORT)
  --web-port N       Loopback web port (default: 3300, LAB_WEB_HOST_PORT)
  --base-path PATH   Web path prefix (default: /assist)
  --help

Take a protected backup of PostgreSQL and the objects volume first (see
docs/fresh_install_linux_redhat.md, "Local storage and backups").

The old services keep serving while images build. They then stop, migrations run
(`alembic upgrade head`, which applies only pending revisions), and the stack
starts and must pass /health, /ready and the login-page check over loopback.
A failure leaves the stack stopped or unhealthy for diagnosis; nothing is rolled
back. The database container is started if stopped and never recreated. nginx,
SELinux, the firewall, and the other applications on this server are never
touched. Secrets stay in .env and are never printed. Do not run with bash -x.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null || fail "Required command not found: $1"; }
as_app() { sudo -u "$app_user" -- "$@"; }
compose() {
    docker compose -p "$project" \
        -f "$repo/docker-compose.yml" -f "$repo/infra/compose/docker-compose.prod.yml" "$@"
}

# Whether a key has a non-empty value in .env, without reading the value out.
has_secret() { grep -Eq "^$1=.+" "$repo/.env"; }

on_error() {
    local result=$?
    trap - ERR
    printf '\nDeployment failed (exit %s). No Git or database rollback was attempted.\n' "$result" >&2
    if [[ ${app_stopped:-false} == true ]]; then
        printf 'Application services may be stopped. Inspect with:\n' >&2
        printf '  sudo docker compose -p %s -f docker-compose.yml -f infra/compose/docker-compose.prod.yml logs --tail=100 api web\n' "$project" >&2
        printf 'Correct the error and rerun this script. Do not restart an older image on a migrated schema.\n' >&2
    fi
    exit "$result"
}

http_status() {
    curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 2 --max-time 10 "$1" || true
}

wait_for() {
    # wait_for URL EXPECTED_STATUS LABEL
    local attempt status
    for (( attempt=1; attempt<=60; attempt++ )); do
        status=$(http_status "$1")
        [[ $status == "$2" ]] && { note "$3: $status"; return 0; }
        sleep 3
    done
    printf '%s answered %s, expected %s: %s\n' "$3" "${status:-nothing}" "$2" "$1" >&2
    return 1
}

main() {
    local check_only=false first_install=false repo app_user=labassistant project=lab-assistant
    local api_port=8100 web_port=3300 base_path=/assist
    local before after app_stopped=false db_running
    repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
    while (( $# )); do
        case "$1" in
            --help|-h) usage; return 0 ;;
            --check) check_only=true; shift ;;
            --first-install) first_install=true; shift ;;
            --repo|--user|--project|--api-port|--web-port|--base-path)
                (( $# >= 2 )) || fail "Missing value for $1"
                case "$1" in
                    --repo) repo=$2 ;; --user) app_user=$2 ;; --project) project=$2 ;;
                    --api-port) api_port=$2 ;; --web-port) web_port=$2 ;; --base-path) base_path=$2 ;;
                esac
                shift 2 ;;
            *) fail "Unknown option: $1 (see --help)" ;;
        esac
    done
    [[ $base_path == /* && $base_path != */ && $base_path != *' '* ]] \
        || fail 'Use a base path such as /assist, without a trailing slash.'
    [[ $api_port =~ ^[0-9]+$ && $web_port =~ ^[0-9]+$ ]] || fail 'Ports must be numbers.'

    (( EUID == 0 )) || fail 'Run this script with sudo bash.'
    for command in git sudo flock curl docker; do need "$command"; done
    docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required.'

    repo=$(cd -- "$repo" && pwd -P)
    cd -- "$repo"
    [[ -f docker-compose.yml && -f infra/compose/docker-compose.prod.yml && -d database/migrations ]] \
        || fail "Not a Lab AI Assistant checkout: $repo"
    id "$app_user" >/dev/null 2>&1 || fail "No such user: $app_user"

    # Secrets: present, not world-readable, never displayed.
    [[ -f .env ]] || fail 'Missing .env: copy .env.example and fill it in (see the Red Hat guide).'
    [[ $(stat -c '%a' .env) =~ ^[0-7][0-7]0$ ]] || fail '.env is readable by other users; chmod 600 it.'
    for key in POSTGRES_PASSWORD LAB_SECRET_KEY; do
        has_secret "$key" || fail "$key is not set in .env"
    done
    grep -Eq '^LAB_ENV=server$' .env .env.server 2>/dev/null \
        || fail 'LAB_ENV=server is set in neither .env nor .env.server.'

    # One updater per checkout. The lock lives in the git directory, not the tree.
    local git_dir
    git_dir=$(as_app git rev-parse --absolute-git-dir)
    exec 9>"$git_dir/lab-assistant-deploy.lock"
    flock -n 9 || fail 'Another deployment is running for this checkout.'
    trap on_error ERR

    [[ -z $(as_app git status --porcelain --untracked-files=normal) ]] \
        || fail 'Checkout has local edits or untracked files. Reconcile them before deploying.'
    as_app git symbolic-ref --quiet HEAD >/dev/null || fail 'Checkout is on a detached HEAD.'
    as_app git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1 \
        || fail 'Current branch has no upstream.'

    # Validate silently: the expanded configuration contains secrets.
    compose config --quiet
    db_running=$(compose ps --status running -q db)
    if [[ -z $db_running && $first_install != true ]]; then
        fail 'The database container is not running. On the first deployment pass --first-install; otherwise start it deliberately and investigate why it stopped.'
    fi

    before=$(as_app git rev-parse HEAD)
    if [[ $check_only == true ]]; then
        as_app git fetch --quiet
        after=$(as_app git rev-parse '@{upstream}')
        note "Preflight passed. Checkout ${before:0:12}; upstream ${after:0:12}."
        note "Database: ${db_running:+running}${db_running:-not running (needs --first-install)}."
        trap - ERR
        return 0
    fi

    as_app git pull --ff-only
    after=$(as_app git rev-parse HEAD)
    note "Deploying Lab AI Assistant ${before:0:12} -> ${after:0:12}."

    # Build while the old version keeps serving. Rebuilding at the same revision
    # is deliberate: it retries an interrupted deployment and picks up changes to
    # .env.server or the build configuration.
    compose build api web worker-network worker-compute scanner

    if [[ -n $(compose ps -q api web worker-network worker-compute) ]]; then
        compose stop web api worker-network worker-compute
    fi
    app_stopped=true

    # The database is started if it is not running and is never recreated: a
    # recreated database container is how a deploy loses a volume mount.
    compose up -d --no-recreate --wait --wait-timeout 900 db
    compose up -d --wait --wait-timeout 900 scanner
    compose run --rm --no-deps --user 0 --entrypoint sh api \
        -c 'chown -R 10001:10001 /var/lib/lab-assistant/objects'
    note 'Applying pending migrations.'
    compose run --rm --no-deps -w /app/database api alembic upgrade head

    compose up -d --wait --wait-timeout 900

    wait_for "http://127.0.0.1:${api_port}/health" 200 'API liveness'
    # 200 means fit to serve and 503 means not: readiness reports with the status code.
    wait_for "http://127.0.0.1:${api_port}/ready" 200 'API readiness' \
        || fail "Not ready. 'sudo ./scripts/status.sh' names the failing check; the stack is left running for diagnosis."
    wait_for "http://127.0.0.1:${web_port}${base_path}/login" 200 'Login page'
    COMPOSE_PROJECT_NAME="$project" bash "$repo/scripts/check-web.sh"
    app_stopped=false

    note "Deployment complete at ${after:0:12}."
    if [[ $first_install == true ]]; then
        note 'First installation: create the administrator, then configure nginx, SELinux and the firewall for 8445:'
        printf '  sudo docker compose -p %s -f docker-compose.yml -f infra/compose/docker-compose.prod.yml exec api lab db create-admin --email YOU@ic.ac.uk --workspace rlalab\n' "$project"
        printf '  see docs/fresh_install_linux_redhat.md, sections 5 and 6\n'
    fi
    note 'Also check login and CSS in a browser through nginx.'
    trap - ERR
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
