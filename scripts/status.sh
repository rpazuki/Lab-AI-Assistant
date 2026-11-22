#!/usr/bin/env bash
# Deployed commit, container states, health, queue depth, and disk.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

printf 'commit:      %s\n' "$(deployed_commit)"
printf 'branch:      %s\n' "$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
printf 'environment: %s\n' "$(site_value LAB_ENV)"
printf 'role:        %s\n\n' "$INSTALLATION_ROLE"

log "containers"
compose ps

log "health"
if health_check 1; then
  curl -fsS http://localhost:8000/health; echo
  curl -fsS http://localhost:8000/ready; echo
else
  warn "the API did not answer /health"
fi

log "cpu cap"
# Every container runs in one systemd slice that scripts/deploy/update.sh caps at
# 80% of the host's CPUs (--cpu-percent). A container outside it is uncapped.
quota=$(systemctl show labassistant.slice -p CPUQuotaPerSecUSec --value 2>/dev/null || true)
printf 'labassistant.slice: %s of CPU time per second, on %s CPUs\n' "${quota:-no quota}" "$(nproc)"
for id in $(compose ps -q); do
  [[ $(docker inspect -f '{{.HostConfig.CgroupParent}}' "$id") == labassistant.slice ]] \
    || warn "$(docker inspect -f '{{.Name}}' "$id") runs outside the CPU cap; redeploy or recreate it"
done

log "disk"
# An unbounded log or object store fills the disk and takes PostgreSQL with it.
df -h "$(dirname "$(site_value LAB_OBJECT_STORE_ROOT || echo /var/lib)")" 2>/dev/null || df -h /
docker system df 2>/dev/null || true

log "queue"
compose exec -T db psql -U "$(env_value POSTGRES_USER)" -d "$(env_value POSTGRES_DB)" -c \
  "select stage, state, count(*) from tasks group by 1,2 order by 1,2" 2>/dev/null \
  || warn "could not read the task table (is the database up?)"
