#!/usr/bin/env bash
# Resolve Compose volume names instead of guessing them.
#
# The script this replaces named `lab-assistant_pgdata` and `lab-assistant_objects`
# literally. Compose prefixes volumes with the project name, which defaults to the
# directory the stack was brought up from — so a deployment in `/opt/lab` or a
# second stack on a shared host has different names. The old script then either
# removed the contents of a volume it had guessed at, or, when the guess did not
# exist, let Docker create an empty one and restored into nothing.
#
# `docker volume ls` is asked what this host actually has, and the suffix — the
# part Compose does not rewrite — selects among them. Where the answer is not
# exactly one volume, these functions fail.
# They never pick: two candidates on a host means two deployments, and choosing
# one is how a restore lands in the wrong installation.

# resolve_volume <suffix>
#
# Prints the one volume on this host whose name ends in `_<suffix>` (or is
# exactly `<suffix>`). Exits non-zero, with an explanation on stderr, when there
# is no such volume or more than one.
resolve_volume() {
  local suffix="${1:?usage: resolve_volume <suffix>}"
  local candidates
  candidates="$(docker volume ls --quiet 2>/dev/null || true)"

  local matches=()
  local name
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    if [[ "${name}" == *"_${suffix}" || "${name}" == "${suffix}" ]]; then
      matches+=("${name}")
    fi
  done <<< "${candidates}"

  if [[ "${#matches[@]}" -eq 0 ]]; then
    cat >&2 <<EOF
No Docker volume ending in "_${suffix}" exists on this host.

A restore must not create one: an empty volume restores into nothing and the
stack comes up looking healthy with no data. Bring the stack up once so Compose
declares its volumes, or set the name explicitly and re-run.

Volumes on this host:
${candidates:-  (none)}
EOF
    return 1
  fi

  if [[ "${#matches[@]}" -gt 1 ]]; then
    cat >&2 <<EOF
Ambiguous: ${#matches[@]} volumes end in "_${suffix}" on this host.

  $(printf '%s\n  ' "${matches[@]}")
That means more than one deployment lives here. Picking one is how a restore
lands in the wrong installation, so this refuses. Set the target explicitly:

  PGDATA_VOLUME=<name> OBJECTS_VOLUME=<name> restore.sh ...
EOF
    return 1
  fi

  printf '%s\n' "${matches[0]}"
}

# require_fresh_volume <name>
#
# A restore writes into an empty volume. One that already holds data belongs to
# a running installation, and the old script's `rm -rf /target/*` is how that
# installation stopped existing.
require_fresh_volume() {
  local name="${1:?usage: require_fresh_volume <name>}"
  local contents
  contents="$(docker run --rm -v "${name}:/target:ro" alpine:3 sh -c 'ls -A /target' 2>/dev/null || true)"
  if [[ -n "${contents}" ]]; then
    cat >&2 <<EOF
Volume "${name}" is not empty.

Restoring into it would destroy whatever installation it belongs to. If this
really is the volume to replace, remove it deliberately first:

  docker volume rm ${name}

and re-run. This step is refused rather than prompted because the prompt is the
part people answer without reading.
EOF
    return 1
  fi
}
