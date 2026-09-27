#!/usr/bin/env bash
# Smoke checks for the Azure test environment: public endpoints and TLS from here,
# then the checks inside the VM (services, orchestrator readiness, configuration,
# golden evals) through Run Command.
#   deploy/azure/scripts/smoke.sh <app-host> <resource-group> <vm-name>
set -uo pipefail
APP_HOST="${1:?app host}"
RESOURCE_GROUP="${2:?resource group}"
VM="${3:?vm name}"
fail=0
check() {  # description, command...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then printf '  ok    %s\n' "$what"; else printf '  FAIL  %s\n' "$what"; fail=1; fi
}
status() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
# The certificate is requested when Caddy starts; allow a few minutes on the first run.
# shellcheck disable=SC2317  # called through check()
wait_tls() {
  for _ in $(seq 1 30); do
    curl -sf --max-time 10 -o /dev/null "https://$APP_HOST/healthz" && return 0
    sleep 10
  done
  return 1
}

echo "Public endpoints"
check "TLS certificate issued, test client https://$APP_HOST/" wait_tls
check "token service rejects anonymous calls" test "$(status -X POST "https://$APP_HOST/v1/voice-sessions")" = 401
check "console requires sign-in" sh -c "case \$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://$APP_HOST/console) in 302|401|403) exit 0;; *) exit 1;; esac"
# LiveKit answers /rtc/validate without a token with 401; Caddy would answer 502 if LiveKit were down.
check "LiveKit signalling https://$APP_HOST/rtc" test "$(status "https://$APP_HOST/rtc/validate")" = 401

echo "Inside the VM"
message=$(az vm run-command invoke -g "$RESOURCE_GROUP" -n "$VM" --command-id RunShellScript \
  --scripts "bash /opt/agent-orchestrator/smoke.sh" --query "value[0].message" -o tsv 2>&1)
printf '%s\n' "$message" | grep -E '^\s+(ok|FAIL) ' || printf '%s\n' "$message" | tail -20
printf '%s\n' "$message" | grep -q SMOKE_OK || fail=1

[ "$fail" = 0 ] && echo "All smoke checks passed." || echo "Some checks failed; see docs/AZURE_TEST_ENV.md (troubleshooting)."
exit "$fail"
