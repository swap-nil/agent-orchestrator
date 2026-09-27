#!/bin/bash
# Smoke checks inside the VM: every service up, orchestrator ready, configuration
# valid, golden evals pass the change gate. Run by deploy/azure/scripts/smoke.sh
# through Run Command; the last line is SMOKE_OK or SMOKE_FAILED.
# shellcheck disable=SC2317  # the helper functions are called through check()
set -uo pipefail
export HOME="${HOME:-/root}" PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
cd "$(dirname "$0")" || exit 1
fail=0
check() {  # description, command...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then printf '  ok    %s\n' "$what"; else printf '  FAIL  %s\n' "$what"; fail=1; fi
}
healthy() {  # service -> running, and healthy when it has a health check
  local id state
  id=$(docker compose ps -q "$1") && [ -n "$id" ] || return 1
  state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
  [ "$state" = running/healthy ] || [ "$state" = running/none ]
}
orch() { docker compose exec -T orchestrator "$@"; }

echo "Services"
for service in $(docker compose config --services); do
  [ "$service" = orchestrator-migrate ] && continue
  check "$service" healthy "$service"
done
echo "Orchestrator"
check "readiness (Redis reachable)" curl -sf --max-time 5 http://127.0.0.1:8080/readyz
check "configuration and catalogue valid" orch python -m orchestrator.cli validate-config
check "golden evals pass the change gate" orch python -m orchestrator.cli run-evals
echo "LiveKit"
check "server answers on 7880" curl -sf --max-time 5 http://127.0.0.1:7880/

[ "$fail" = 0 ] && echo SMOKE_OK || echo SMOKE_FAILED
exit "$fail"
