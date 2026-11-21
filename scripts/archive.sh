#!/usr/bin/env bash
# Publish this library to the shared archive, or bring a reader up to date from it.
#
#   sudo bash scripts/archive.sh publish [--workspace rlalab] [--keep 3]
#   sudo bash scripts/archive.sh sync    [--workspace rlalab] [--keep 3]
#                                        [--library-id UUID] [--group-map FROM=TO ...]
#
# publish (publisher): issue a fresh policy checkpoint, package every servable
#   release, upload the snapshot and move the library head to it — in that order,
#   because a package records the policy head current when it was written.
# sync (reader): fetch the snapshot the head names, GET only, and import it.
#
# Both are safe to run on a timer, and both must: a policy checkpoint is valid
# for LAB_ARCHIVE_POLICY_VALIDITY_HOURS (24 by default), after which a reader
# stops serving restricted material. Run publish, then sync, at least daily.
#
# The archive's location comes from LAB_ARCHIVE_STORE_* (in .env.site or .env).
# Snapshots are written under --dir (default /srv/lab-archive) and the newest
# --keep of them are kept. Credential and signing-key files must live under
# --secrets-dir (default /etc/lab-assistant), which is mounted read-only.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() { sed -n '2,20p' "$0"; }

COMMAND="${1:-}"; [[ $# -gt 0 ]] && shift
WORKSPACE="rlalab"; KEEP=3; WORK_DIR="/srv/lab-archive"; SECRETS_DIR="/etc/lab-assistant"
LIBRARY_ID=""; GROUP_MAPS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)   WORKSPACE="$2"; shift 2 ;;
    --keep)        KEEP="$2"; shift 2 ;;
    --dir)         WORK_DIR="$2"; shift 2 ;;
    --secrets-dir) SECRETS_DIR="$2"; shift 2 ;;
    --library-id)  LIBRARY_ID="$2"; shift 2 ;;
    --group-map)   GROUP_MAPS+=(--group-map "$2"); shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
case "$COMMAND" in publish|sync) ;; -h|--help|"") usage; exit 0 ;; *) die "unknown command ${COMMAND}: publish or sync" ;; esac
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "--keep takes a positive number"
[[ "$WORK_DIR" == /* && "$SECRETS_DIR" == /* ]] || die "--dir and --secrets-dir take absolute paths"

# The container user, which must own the snapshot directory to write into it.
# Overridable only so the tests can run this without root.
APP_UID="${LAB_ARCHIVE_APP_UID:-10001}"

# ── preflight ────────────────────────────────────────────────────────────────
[[ -n "$(site_value LAB_ARCHIVE_STORE_KIND)" ]] \
  || die "LAB_ARCHIVE_STORE_KIND is not set (in .env.site or .env): there is no archive to use"
case "$COMMAND:$INSTALLATION_ROLE" in
  publish:publisher|sync:reader) ;;
  publish:*) die "this installation is a ${INSTALLATION_ROLE}; only the publisher publishes" ;;
  sync:*)    die "this installation is a ${INSTALLATION_ROLE}; sync is for a reader (the publisher's corpus is its own)" ;;
esac

# Every file a setting names must be inside the one directory that is mounted,
# or the command fails inside the container with a path that looks right.
for key in LAB_ARCHIVE_STORE_CREDENTIALS_PATH LAB_ARCHIVE_SIGNING_KEY_PATH; do
  path="$(site_value "$key")"
  [[ -z "$path" ]] && continue
  [[ "$path" == "$SECRETS_DIR"/* ]] || die "${key} must be a file under ${SECRETS_DIR}, which is what gets mounted"
  [[ -f "$path" ]] || die "${key} names ${path}, which does not exist on this host"
done

if [[ ! -d "$WORK_DIR" ]]; then
  [[ $EUID -eq 0 ]] || die "${WORK_DIR} does not exist; run with sudo so it can be created"
  install -d -m 750 -o "$APP_UID" -g "$APP_UID" "$WORK_DIR"
elif [[ "$(ls -nd "$WORK_DIR" | awk '{print $3}')" != "$APP_UID" ]]; then
  [[ $EUID -eq 0 ]] || die "${WORK_DIR} is not owned by uid ${APP_UID}; run with sudo so it can be fixed"
  chown "$APP_UID:$APP_UID" "$WORK_DIR"
fi

mounts=(-v "${WORK_DIR}:${WORK_DIR}")
[[ -d "$SECRETS_DIR" ]] && mounts+=(-v "${SECRETS_DIR}:${SECRETS_DIR}:ro")

lab() {
  compose run --rm --no-deps -T "${mounts[@]}" api lab "$@"
}

# Delete all but the newest $KEEP directories matching a pattern, as the
# container user that owns them.
prune() {
  local pattern="$1" old
  old="$(find "$WORK_DIR" -mindepth 1 -maxdepth 1 -type d -name "$pattern" | sort -r | tail -n +"$((KEEP + 1))")"
  [[ -z "$old" ]] && return 0
  log "removing $(wc -l <<<"$old" | tr -d ' ') older snapshot director$( [[ $(wc -l <<<"$old") -eq 1 ]] && echo y || echo ies)"
  # shellcheck disable=SC2086 # one path per line, none with spaces: we named them
  compose run --rm --no-deps -T "${mounts[@]}" --entrypoint rm api -rf -- $old
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# ── publish ──────────────────────────────────────────────────────────────────
if [[ "$COMMAND" == publish ]]; then
  [[ -n "$(site_value LAB_ARCHIVE_SIGNING_KEY_PATH)" ]] \
    || warn "LAB_ARCHIVE_SIGNING_KEY_PATH is not set: the snapshot will be unsigned, and a reader with LAB_ARCHIVE_REQUIRE_SIGNATURE=true refuses it"
  dir="${WORK_DIR}/snap-${STAMP}"
  log "issuing a policy checkpoint for ${WORKSPACE}"
  lab archive policy publish --workspace "$WORKSPACE" --apply
  log "packaging ${WORKSPACE} into ${dir}"
  lab archive package --workspace "$WORKSPACE" "$dir"
  log "publishing ${dir}"
  lab archive publish --workspace "$WORKSPACE" "$dir" --apply
  prune 'snap-*'
  library="$(compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -v slug="$1"' sh "$WORKSPACE" \
    <<<"select id from workspaces where slug = :'slug';" 2>/dev/null || true)"
  log "published. A reader syncs it with LAB_ARCHIVE_LIBRARY_ID=${library:-<the workspace id>}"
  exit 0
fi

# ── sync ─────────────────────────────────────────────────────────────────────
LIBRARY_ID="${LIBRARY_ID:-$(site_value LAB_ARCHIVE_LIBRARY_ID)}"
[[ "$LIBRARY_ID" =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]] \
  || die "set LAB_ARCHIVE_LIBRARY_ID in .env.site, or pass --library-id: the publisher's library UUID"
dir="${WORK_DIR}/fetch-${STAMP}"
log "fetching library ${LIBRARY_ID} into ${dir}"
lab archive fetch "$dir" --library-id "$LIBRARY_ID"
log "importing into ${WORKSPACE}"
lab archive import "$dir" --workspace "$WORKSPACE" --library-id "$LIBRARY_ID" "${GROUP_MAPS[@]}"
prune 'fetch-*'
log "synced"
