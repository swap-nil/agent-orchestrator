#!/usr/bin/env bash
# Smoke checks for the Azure test environment: workloads, TLS, public endpoints,
# orchestrator readiness and the golden eval suite inside the cluster.
#   deploy/azure/scripts/smoke.sh <app-host> <livekit-host>
set -uo pipefail
APP_HOST="${1:?app host}"
LIVEKIT_HOST="${2:?livekit host}"
fail=0
check() {  # description, command...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then printf '  ok    %s\n' "$what"; else printf '  FAIL  %s\n' "$what"; fail=1; fi
}
status() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }

echo "Workloads"
for ns in platform agents orchestrator edge voice; do
  for d in $(kubectl -n "$ns" get deploy -o name 2>/dev/null); do
    check "$ns/${d#*/} available" kubectl -n "$ns" wait --for=condition=available "$d" --timeout=5s
  done
done

echo "Public endpoints"
check "TLS certificate issued" kubectl -n edge wait --for=condition=ready certificate/edge-tls --timeout=5m
check "test client https://$APP_HOST/" test "$(status "https://$APP_HOST/healthz")" = 200
check "token service rejects anonymous calls" test "$(status -X POST "https://$APP_HOST/v1/voice-sessions")" = 401
check "console requires sign-in" sh -c "case \$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://$APP_HOST/console) in 302|401|403) exit 0;; *) exit 1;; esac"
check "LiveKit https://$LIVEKIT_HOST/" test "$(status "https://$LIVEKIT_HOST/")" = 200

echo "Orchestrator"
check "readiness (Redis reachable)" kubectl -n orchestrator exec deploy/orchestrator -c orchestrator -- \
  python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=5)"
check "configuration and catalogue valid" kubectl -n orchestrator exec deploy/orchestrator -c orchestrator -- \
  python -m orchestrator.cli validate-config
check "golden evals pass the change gate" kubectl -n orchestrator exec deploy/orchestrator -c orchestrator -- \
  python -m orchestrator.cli run-evals

[ "$fail" = 0 ] && echo "All smoke checks passed." || echo "Some checks failed; see docs/AZURE_TEST_ENV.md (troubleshooting)."
exit "$fail"
