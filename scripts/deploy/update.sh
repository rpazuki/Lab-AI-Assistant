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
  --cpu-percent N    Most of this host's CPU every container together may use
                     (default: 80). Applied to the labassistant.slice systemd
                     slice that every container runs in.
  --help

The installation role is read from .env, .env.server and .env.site (last wins,
as Compose reads them). A reader (LAB_INSTALLATION_ROLE=reader) is deployed
without workers or the scanner (infra/compose/docker-compose.reader.yml), needs
its own addresses in .env.site, and is accepted as deployed while the only thing
it lacks is a corpus: the first import comes after the first install
(scripts/archive.sh sync).

Take a protected backup of PostgreSQL and the objects volume first (see
docs/fresh_install_linux_redhat.md, "Local storage and backups").

The old services keep serving while images build. They then stop, migrations run
(`alembic upgrade head`, which applies only pending revisions), and the stack
starts and must pass /health, /ready and the login-page check over loopback.
A failure leaves the stack stopped or unhealthy for diagnosis; nothing is rolled
back. The database container is started if stopped, and recreated only when
its configuration changed (its data is in a named volume). nginx, SELinux, the
firewall, and the other applications on this server are never touched; the one
host change is the labassistant.slice unit that caps this stack's CPU. Secrets stay in .env and are never printed. Do not run with bash -x.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null || fail "Required command not found: $1"; }
as_app() { sudo -u "$app_user" -- "$@"; }
# Extra Compose files for this host's role; set once in main.
overlay=()
compose() {
    docker compose -p "$project" \
        -f "$repo/docker-compose.yml" -f "$repo/infra/compose/docker-compose.prod.yml" \
        "${overlay[@]}" "$@"
}

# One setting's effective value across .env, .env.server and .env.site, the
# last file winning as it does for Compose. Prints nothing when it is unset.
setting() {
    local file found value=
    for file in "$repo/.env" "$repo/.env.server" "$repo/.env.site"; do
        [[ -f $file ]] || continue
        found=$(sed -n "s/^$1=//p" "$file" | tail -n1)
        [[ -n $found ]] && value=$found
    done
    value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
    printf '%s' "$value"
}

# Whether a key has a non-empty value in .env, without reading the value out.
has_secret() { grep -Eq "^$1=.+" "$repo/.env"; }

# The systemd slice every container runs in (cgroup_parent in the production
# overlay). Its quota caps the containers together: per-container limits cannot,
# since each can sit under its own limit while together they take the whole VM.
CPU_SLICE=labassistant.slice

cpu_quota() { printf '%s' $(( $(nproc) * $1 )); }

cap_cpus() {
    # cap_cpus PERCENT: write the slice unit and apply its quota now.
    local percent=$1 quota unit=/etc/systemd/system/$CPU_SLICE content
    quota=$(cpu_quota "$percent")
    content=$(printf '%s\n' \
        '# Written by scripts/deploy/update.sh; rewritten on every deployment.' \
        '# Every Lab AI Assistant container runs in this slice, so together they' \
        "# never use more than ${percent}% of this host's $(nproc) CPUs." \
        '[Unit]' \
        'Description=Lab AI Assistant containers, CPU capped' \
        '' \
        '[Slice]' \
        "CPUQuota=${quota}%")
    if [[ ! -f $unit || $(cat "$unit") != "$content" ]]; then
        printf '%s\n' "$content" > "$unit"
        systemctl daemon-reload
    fi
    # Applies to the running slice too, without restarting anything in it.
    systemctl set-property --runtime "$CPU_SLICE" "CPUQuota=${quota}%"
    note "CPU cap: all containers together use at most ${percent}% of $(nproc) CPUs (CPUQuota=${quota}%)."
}

verify_cpu_cap() {
    local id parent outside=()
    for id in $(compose ps -q); do
        parent=$(docker inspect -f '{{.HostConfig.CgroupParent}}' "$id")
        [[ $parent == "$CPU_SLICE" ]] || outside+=("$(docker inspect -f '{{.Name}}' "$id")")
    done
    (( ${#outside[@]} == 0 )) \
        || fail "Running outside the CPU cap ($CPU_SLICE): ${outside[*]}. Recreate them: sudo ./scripts/restart.sh --recreate"
    note "Every container runs in $CPU_SLICE."
}

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
    # wait_for URL EXPECTED_STATUS LABEL [ATTEMPTS, 3 s apart; default 60]
    local attempt status
    for (( attempt=1; attempt<=${4:-60}; attempt++ )); do
        status=$(http_status "$1")
        [[ $status == "$2" ]] && { note "$3: $status"; return 0; }
        sleep 3
    done
    printf '%s answered %s, expected %s: %s\n' "$3" "${status:-nothing}" "$2" "$1" >&2
    return 1
}

# A reader that has never imported a snapshot is not ready by design: it has
# nothing to answer with (application/readiness.py). That is the expected state
# straight after a first install, so it is accepted when `corpus` is the only
# blocking failure, and any other failure still stops the deployment.
reader_awaiting_import() {
    compose exec -T api python - <<'PY'
import json
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=5)
    raise SystemExit(0)
except urllib.error.HTTPError as error:
    try:
        body = json.loads(error.read().decode() or "{}")
    except ValueError:
        raise SystemExit(1)
failing = {
    check.get("name")
    for check in body.get("checks", [])
    if not check.get("ok") and check.get("blocks_serving", True)
}
raise SystemExit(0 if failing == {"corpus"} else 1)
PY
}

main() {
    local check_only=false first_install=false repo app_user=labassistant project=lab-assistant
    local cpu_percent=80
    local api_port=8100 web_port=3300 base_path=/assist
    local before after app_stopped=false db_running role
    local -a app_services build_services
    repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
    while (( $# )); do
        case "$1" in
            --help|-h) usage; return 0 ;;
            --check) check_only=true; shift ;;
            --first-install) first_install=true; shift ;;
            --repo|--user|--project|--api-port|--web-port|--base-path|--cpu-percent)
                (( $# >= 2 )) || fail "Missing value for $1"
                case "$1" in
                    --repo) repo=$2 ;; --user) app_user=$2 ;; --project) project=$2 ;;
                    --api-port) api_port=$2 ;; --web-port) web_port=$2 ;; --base-path) base_path=$2 ;;
                    --cpu-percent) cpu_percent=$2 ;;
                esac
                shift 2 ;;
            *) fail "Unknown option: $1 (see --help)" ;;
        esac
    done
    [[ $base_path == /* && $base_path != */ && $base_path != *' '* ]] \
        || fail 'Use a base path such as /assist, without a trailing slash.'
    [[ $api_port =~ ^[0-9]+$ && $web_port =~ ^[0-9]+$ ]] || fail 'Ports must be numbers.'
    [[ $cpu_percent =~ ^[0-9]+$ ]] && (( cpu_percent >= 10 && cpu_percent <= 100 )) \
        || fail '--cpu-percent takes a whole number from 10 to 100.'

    (( EUID == 0 )) || fail 'Run this script with sudo bash.'
    for command in git sudo flock curl docker systemctl nproc; do need "$command"; done
    docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required.'
    # The CPU cap is a systemd slice; under the cgroupfs driver Docker would put
    # the containers in a plain cgroup of that name, with no cap at all.
    [[ $(docker info --format '{{.CgroupDriver}}') == systemd ]] \
        || fail "Docker uses the $(docker info --format '{{.CgroupDriver}}') cgroup driver; the CPU cap needs the systemd driver (the default on Red Hat with cgroup v2)."

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

    role=$(setting LAB_INSTALLATION_ROLE)
    role=${role:-publisher}
    case $role in
        publisher|maintenance)
            app_services=(api web worker-network worker-compute)
            build_services=(api web worker-network worker-compute scanner) ;;
        reader)
            # .env.server carries the Imperial VM's addresses; a reader on any
            # other host would serve with them and every login would fail CORS.
            [[ -f .env.site ]] \
                || fail 'A reader needs .env.site with its own LAB_CORS_ORIGINS and LAB_APP_PUBLIC_URL (see .env.site.example).'
            overlay=(-f "$repo/infra/compose/docker-compose.reader.yml")
            app_services=(api web)
            build_services=(api web) ;;
        *) fail "LAB_INSTALLATION_ROLE=$role is not one of: publisher, reader, maintenance." ;;
    esac
    note "Installation role: $role."

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
        note "CPU cap to apply: ${cpu_percent}% of $(nproc) CPUs (CPUQuota=$(cpu_quota "$cpu_percent")%)."
        trap - ERR
        return 0
    fi

    as_app git pull --ff-only
    after=$(as_app git rev-parse HEAD)
    note "Deploying Lab AI Assistant ${before:0:12} -> ${after:0:12}."
    # Before anything starts: a container joining the slice must find its quota.
    cap_cpus "$cpu_percent"

    # Build while the old version keeps serving. Rebuilding at the same revision
    # is deliberate: it retries an interrupted deployment and picks up changes to
    # .env.server or the build configuration.
    compose build "${build_services[@]}"

    if [[ -n $(compose ps -q "${app_services[@]}") ]]; then
        compose stop "${app_services[@]}"
    fi
    if [[ $role == reader ]]; then
        # Containers left from when this host was a publisher keep restarting
        # under their old policy unless stopped; the profile makes them nameable.
        compose --profile publisher stop worker-network worker-compute scanner >/dev/null 2>&1 || true
    fi
    app_stopped=true

    # The database is started if it is not running and is never recreated: a
    # recreated database container is how a deploy loses a volume mount.
    compose up -d --no-recreate --wait --wait-timeout 900 db
    if [[ $role != reader ]]; then
        compose up -d --wait --wait-timeout 900 scanner
    fi
    compose run --rm --no-deps --user 0 --entrypoint sh api \
        -c 'chown -R 10001:10001 /var/lib/lab-assistant/objects'
    note 'Applying pending migrations.'
    compose run --rm --no-deps -w /app/database api alembic upgrade head
    if [[ $role != reader ]]; then
        # Fills the candidate filter index and the duplicate-review queue for
        # anything that predates them; a no-op once they are complete.
        note 'Building the review index.'
        compose run --rm --no-deps api lab db backfill-review
    fi

    compose up -d --wait --wait-timeout 900
    verify_cpu_cap

    wait_for "http://127.0.0.1:${api_port}/health" 200 'API liveness'
    # 200 means fit to serve and 503 means not: readiness reports with the status code.
    if [[ $role == reader ]]; then
        if ! wait_for "http://127.0.0.1:${api_port}/ready" 200 'API readiness' 20; then
            reader_awaiting_import \
                || fail "Not ready. 'sudo ./scripts/status.sh' names the failing check; the stack is left running for diagnosis."
            note 'API readiness: serving nothing yet, as a reader does before its first import.'
        fi
    else
        wait_for "http://127.0.0.1:${api_port}/ready" 200 'API readiness' \
            || fail "Not ready. 'sudo ./scripts/status.sh' names the failing check; the stack is left running for diagnosis."
    fi
    wait_for "http://127.0.0.1:${web_port}${base_path}/login" 200 'Login page'
    COMPOSE_PROJECT_NAME="$project" bash "$repo/scripts/check-web.sh"
    app_stopped=false

    note "Deployment complete at ${after:0:12}."
    if [[ $first_install == true && $role == reader ]]; then
        note 'First installation of a reader: create the administrator, import the library, then put TLS in front of the web port:'
        printf '  sudo docker compose -p %s -f docker-compose.yml -f infra/compose/docker-compose.prod.yml -f infra/compose/docker-compose.reader.yml exec api lab db create-admin --email YOU@example.org --workspace rlalab\n' "$project"
        printf '  sudo bash scripts/archive.sh sync\n'
        printf '  sudo bash scripts/archive.sh schedule\n'
        printf '  see docs/operations/reader-mirror.md\n'
    elif [[ $first_install == true ]]; then
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
