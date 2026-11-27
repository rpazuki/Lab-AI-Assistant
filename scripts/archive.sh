#!/usr/bin/env bash
# Publish this library to the shared archive, or bring a reader up to date from it.
#
#   sudo bash scripts/archive.sh publish    [--workspace rlalab] [--keep 3]
#   sudo bash scripts/archive.sh policy     [--workspace rlalab]
#   sudo bash scripts/archive.sh sync       [--workspace rlalab] [--keep 3]
#                                           [--library-id UUID] [--group-map FROM=TO ...]
#   sudo bash scripts/archive.sh schedule   [the options above, kept for every run]
#   sudo bash scripts/archive.sh unschedule
#
# publish (publisher): issue a fresh policy checkpoint, package every servable
#   release, upload the snapshot and move the library head to it — in that order,
#   because a package records the policy head current when it was written.
# policy (publisher): issue a fresh policy checkpoint only. Cheap: no package.
# sync (reader): fetch the snapshot the head names, GET only, and import it.
#
# schedule installs systemd timers for this host's role, in UTC: a publisher
#   publishes daily at 02:15 and renews the checkpoint at 08:15, 14:15 and 20:15;
#   a reader syncs daily at 03:15. A checkpoint is valid for
#   LAB_ARCHIVE_POLICY_VALIDITY_HOURS (24 by default) and a reader then stops
#   serving restricted material, so renewing every 6 hours leaves three failed
#   runs before it lapses. A timer missed while the host was off runs at boot.
#   Logs: journalctl -u lab-archive-publish (or -policy, -sync).
# unschedule removes those timers.
#
# The archive's location comes from LAB_ARCHIVE_STORE_* (in .env.site or .env).
# Snapshots are written under --dir (default /srv/lab-archive) and the newest
# --keep of them are kept. Credential and signing-key files must live under
# --secrets-dir (default /etc/lab-assistant), which is mounted read-only.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

usage() { sed -n '2,31p' "$0"; }

COMMAND="${1:-}"; [[ $# -gt 0 ]] && shift
WORKSPACE="rlalab"; KEEP=3; WORK_DIR="/srv/lab-archive"; SECRETS_DIR="/etc/lab-assistant"
LIBRARY_ID=""; GROUP_MAPS=()
# Every option as given, so a scheduled run is the run the operator asked for.
# A string rather than an array: bash 3.2 refuses an empty array under set -u.
OPTIONS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace|--keep|--dir|--secrets-dir|--library-id|--group-map)
      [[ $# -ge 2 ]] || die "$1 takes a value"
      [[ "$2" =~ ^[^[:space:]]+$ ]] || die "$1 takes a value without spaces"
      OPTIONS+=" $1 $2" ;;
  esac
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
case "$COMMAND" in
  publish|policy|sync|schedule|unschedule) ;;
  -h|--help|"") usage; exit 0 ;;
  *) die "unknown command ${COMMAND}: publish, policy, sync, schedule or unschedule" ;;
esac
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "--keep takes a positive number"
[[ "$WORK_DIR" == /* && "$SECRETS_DIR" == /* ]] || die "--dir and --secrets-dir take absolute paths"

# The container user, which must own the snapshot directory to write into it.
# Overridable only so the tests can run this without root.
APP_UID="${LAB_ARCHIVE_APP_UID:-10001}"

# ── timers ───────────────────────────────────────────────────────────────────
# Where the units go, and the command that runs them. Overridable only so the
# tests can run this without root or systemd.
UNIT_DIR="${LAB_ARCHIVE_UNIT_DIR:-/etc/systemd/system}"
UNITS=(lab-archive-publish lab-archive-policy lab-archive-sync)

if [[ "$COMMAND" == unschedule ]]; then
  [[ $EUID -eq 0 || -n "${LAB_ARCHIVE_UNIT_DIR:-}" ]] || die "run with sudo: the timers are systemd units"
  removed=0
  for unit in "${UNITS[@]}"; do
    [[ -f "${UNIT_DIR}/${unit}.timer" || -f "${UNIT_DIR}/${unit}.service" ]] || continue
    systemctl disable --now "${unit}.timer" >/dev/null 2>&1 || true
    rm -f -- "${UNIT_DIR}/${unit}.timer" "${UNIT_DIR}/${unit}.service"
    log "removed ${unit}.timer"
    removed=$((removed + 1))
  done
  if (( removed == 0 )); then log "no archive timers are installed"; exit 0; fi
  systemctl daemon-reload
  exit 0
fi

# ── preflight ────────────────────────────────────────────────────────────────
[[ -n "$(site_value LAB_ARCHIVE_STORE_KIND)" ]] \
  || die "LAB_ARCHIVE_STORE_KIND is not set (in .env.site or .env): there is no archive to use"
case "$COMMAND:$INSTALLATION_ROLE" in
  publish:publisher|policy:publisher|sync:reader|schedule:publisher|schedule:reader) ;;
  publish:*|policy:*) die "this installation is a ${INSTALLATION_ROLE}; only the publisher publishes" ;;
  sync:*)    die "this installation is a ${INSTALLATION_ROLE}; sync is for a reader (the publisher's corpus is its own)" ;;
  schedule:*) die "this installation's role is ${INSTALLATION_ROLE}; schedule needs a publisher or a reader" ;;
esac
if [[ "$INSTALLATION_ROLE" == reader && "$COMMAND" =~ ^(sync|schedule)$ ]]; then
  LIBRARY_ID="${LIBRARY_ID:-$(site_value LAB_ARCHIVE_LIBRARY_ID)}"
  [[ "$LIBRARY_ID" =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]] \
    || die "set LAB_ARCHIVE_LIBRARY_ID in .env.site, or pass --library-id: the publisher's library UUID"
fi

# Every file a setting names must be inside the one directory that is mounted,
# or the command fails inside the container with a path that looks right.
for key in LAB_ARCHIVE_STORE_CREDENTIALS_PATH LAB_ARCHIVE_SIGNING_KEY_PATH; do
  path="$(site_value "$key")"
  [[ -z "$path" ]] && continue
  [[ "$path" == "$SECRETS_DIR"/* ]] || die "${key} must be a file under ${SECRETS_DIR}, which is what gets mounted"
  [[ -f "$path" ]] || die "${key} names ${path}, which does not exist on this host"
done

# ── schedule ─────────────────────────────────────────────────────────────────
# One oneshot service and one timer per job. Rewritten only when they differ, so
# running schedule again after a change of options is how the options change.
write_unit() {
  local file="$1" content="$2"
  if [[ ! -f "$file" || "$(cat "$file")" != "$content" ]]; then
    printf '%s\n' "$content" > "$file"
    UNITS_CHANGED=true
  fi
}

schedule_job() {
  local unit="$1" command="$2" when="$3" what="$4"
  local header="# Written by scripts/archive.sh schedule; rewritten each time it runs."
  write_unit "${UNIT_DIR}/${unit}.service" "$(printf '%s\n' \
    "$header" \
    '[Unit]' \
    "Description=Lab AI Assistant archive: ${what}" \
    'After=docker.service network-online.target' \
    'Wants=network-online.target' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    "WorkingDirectory=${REPO_ROOT}" \
    "ExecStart=/bin/bash ${REPO_ROOT}/scripts/archive.sh ${command}${OPTIONS}" \
    "SyslogIdentifier=${unit}")"
  write_unit "${UNIT_DIR}/${unit}.timer" "$(printf '%s\n' \
    "$header" \
    '[Unit]' \
    "Description=Lab AI Assistant archive: ${what}, on a timer" \
    '' \
    '[Timer]' \
    "OnCalendar=${when}" \
    'Persistent=true' \
    'AccuracySec=1min' \
    '' \
    '[Install]' \
    'WantedBy=timers.target')"
  JOBS+=("$unit")
}

if [[ "$COMMAND" == schedule ]]; then
  [[ $EUID -eq 0 || -n "${LAB_ARCHIVE_UNIT_DIR:-}" ]] || die "run with sudo: the timers are systemd units"
  command -v systemctl >/dev/null || die "systemctl not found: schedule needs systemd"
  [[ "$REPO_ROOT" =~ ^/[^[:space:]]+$ ]] || die "the checkout's path ${REPO_ROOT} has a space, which a unit's ExecStart cannot carry"
  [[ -d "$UNIT_DIR" ]] || die "${UNIT_DIR} does not exist"
  UNITS_CHANGED=false; JOBS=()
  if [[ "$INSTALLATION_ROLE" == publisher ]]; then
    validity="$(site_value LAB_ARCHIVE_POLICY_VALIDITY_HOURS)"; validity="${validity:-24}"
    [[ "$validity" =~ ^[0-9]+$ ]] || die "LAB_ARCHIVE_POLICY_VALIDITY_HOURS is ${validity}, not a number of hours"
    (( validity > 6 )) || die "LAB_ARCHIVE_POLICY_VALIDITY_HOURS is ${validity}: a checkpoint renewed every 6 hours would lapse between renewals"
    (( validity >= 12 )) || warn "LAB_ARCHIVE_POLICY_VALIDITY_HOURS is ${validity}: one failed renewal will let the checkpoint lapse"
    schedule_job lab-archive-publish publish '*-*-* 02:15:00 UTC' 'publish the library'
    schedule_job lab-archive-policy policy '*-*-* 08,14,20:15:00 UTC' 'renew the policy checkpoint'
    stale=(lab-archive-sync)
  else
    schedule_job lab-archive-sync sync '*-*-* 03:15:00 UTC' 'sync from the shared archive'
    stale=(lab-archive-publish lab-archive-policy)
  fi
  # A host that changed role keeps no timer of its old one.
  for unit in "${stale[@]}"; do
    [[ -f "${UNIT_DIR}/${unit}.timer" ]] || continue
    systemctl disable --now "${unit}.timer" >/dev/null 2>&1 || true
    rm -f -- "${UNIT_DIR}/${unit}.timer" "${UNIT_DIR}/${unit}.service"
    UNITS_CHANGED=true
  done
  [[ "$UNITS_CHANGED" == true ]] && systemctl daemon-reload
  for unit in "${JOBS[@]}"; do
    systemctl enable --now "${unit}.timer" >/dev/null
    log "${unit}.timer: $(sed -n 's/^OnCalendar=//p' "${UNIT_DIR}/${unit}.timer")"
  done
  # Left in place, a crontab line runs the same job twice, or fails on its own
  # and makes a working timer look broken.
  if command -v crontab >/dev/null; then
    for user in root ${SUDO_USER:-}; do
      if crontab -l -u "$user" 2>/dev/null | grep -v '^[[:space:]]*#' | grep -q 'archive\.sh'; then
        warn "${user}'s crontab also runs archive.sh: remove that line (sudo crontab -e -u ${user}), the timers replace it"
      fi
    done
  fi
  log "scheduled. Next runs: systemctl list-timers 'lab-archive-*'"
  log "run one now to check it: sudo systemctl start ${JOBS[0]}.service; then journalctl -u ${JOBS[0]} -n 50"
  exit 0
fi

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

# One run at a time. A renewal and a publish both move the policy head, and the
# second of two concurrent swaps fails by design, so they wait for each other.
if command -v flock >/dev/null; then
  exec 9>"${WORK_DIR}/.archive.lock"
  flock -w 3600 9 || die "another archive run has held ${WORK_DIR}/.archive.lock for an hour: see journalctl -u 'lab-archive-*'"
else
  warn "flock is not installed: nothing stops two archive runs overlapping"
fi

# ── policy ───────────────────────────────────────────────────────────────────
if [[ "$COMMAND" == policy ]]; then
  log "renewing the policy checkpoint for ${WORKSPACE}"
  lab archive policy publish --workspace "$WORKSPACE" --apply
  log "renewed"
  exit 0
fi

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
dir="${WORK_DIR}/fetch-${STAMP}"
log "fetching library ${LIBRARY_ID} into ${dir}"
lab archive fetch "$dir" --library-id "$LIBRARY_ID"
log "importing into ${WORKSPACE}"
lab archive import "$dir" --workspace "$WORKSPACE" --library-id "$LIBRARY_ID" "${GROUP_MAPS[@]}"
prune 'fetch-*'
log "synced"
