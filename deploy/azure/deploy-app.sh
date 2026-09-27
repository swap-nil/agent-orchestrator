#!/usr/bin/env bash
# Build and roll out container images only, for code changes on an environment that
# deploy.sh already created. Skips Bicep, secrets, Entra ID and the VM files.
#
#   deploy/azure/deploy-app.sh [--env-file FILE] [NAME...]
#
# NAME is an image or a service using it (default: all images):
#   orchestrator   orchestrator, orchestrator-worker, orchestrator-migrate
#   master-agent   master-agent (alias: agent)
#   token-service  token-service
#   mock           test-client, mock-backend, the domain agents (faq-agent, ...)
#
# Each image is built in ACR and pinned by digest; the VM's settings.env gets the new
# digests and bootstrap.sh recreates only the containers whose image changed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/deploy/azure/test.env"
NAMES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?--env-file needs a file}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) NAMES+=("$1"); shift ;;
  esac
done
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy deploy/azure/test.env.example)"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${PREFIX:?set PREFIX in $ENV_FILE}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-$PREFIX}"
OUT="$ROOT/deploy/azure/.out"
cd "$ROOT"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# name -> "repository extras IMG_variable"
image_for() {
  case "$1" in
    orchestrator|orchestrator-worker|orchestrator-migrate) echo "orchestrator orchestrator IMG_ORCHESTRATOR" ;;
    master-agent|agent) echo "master-agent agent IMG_AGENT" ;;
    token-service) echo "token-service token-service IMG_TOKEN_SERVICE" ;;
    mock|test-client|mock-backend|*-agent) echo "mock mock IMG_MOCK" ;;
    *) return 1 ;;
  esac
}
[ ${#NAMES[@]} -gt 0 ] || NAMES=(orchestrator master-agent token-service mock)
IMAGES=()
for name in "${NAMES[@]}"; do
  spec=$(image_for "$name") || { echo "unknown image or service: $name (see --help)"; exit 1; }
  [[ " ${IMAGES[*]} " == *" $spec "* ]] || IMAGES+=("$spec")
done

command -v az >/dev/null 2>&1 || { echo "missing tool: az"; exit 1; }
[ -n "${SUBSCRIPTION_ID:-}" ] && az account set --subscription "$SUBSCRIPTION_ID"
ACR=$(az acr list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv)
VM=$(az vm list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv)
[ -n "$ACR" ] && [ -n "$VM" ] || { echo "no registry or VM in $RESOURCE_GROUP: run deploy/azure/deploy.sh first"; exit 1; }
ACR_SERVER=$(az acr show -n "$ACR" --query loginServer -o tsv)
echo "resource group $RESOURCE_GROUP, registry $ACR, VM $VM"

log "Container images (built in ACR, pinned by digest)"
TAG=$(date -u +%Y%m%d%H%M%S)
UPDATES=()   # IMG_variable=repository@digest
for spec in "${IMAGES[@]}"; do
  read -r repo extras var <<< "$spec"
  echo "  building $repo"
  az acr build -r "$ACR" -t "$repo:$TAG" -f deploy/docker/Dockerfile --build-arg EXTRAS="$extras" . -o none
  UPDATES+=("$var=$ACR_SERVER/$repo@$(az acr repository show -n "$ACR" --image "$repo:$TAG" --query digest -o tsv)")
done
printf '  %s\n' "${UPDATES[@]}"

# Keep the local record current, so deploy.sh with SKIP_BUILD=1 does not roll back.
if [ -f "$OUT/images.env" ]; then
  for update in "${UPDATES[@]}"; do
    sed -i "s|^${update%%=*}=.*|$update|" "$OUT/images.env"
  done
fi

log "Roll out on the VM $VM"
SCRIPT=$(mktemp)
trap 'rm -f "$SCRIPT"' EXIT
{
  echo '#!/bin/bash'
  echo 'set -euo pipefail'
  echo 'cd /opt/agent-orchestrator'
  for update in "${UPDATES[@]}"; do
    echo "sed -i 's|^${update%%=*}=.*|$update|' settings.env"
  done
  echo 'bash ./bootstrap.sh'
} > "$SCRIPT"
LOG_FILE="$OUT/deploy-app.log"
mkdir -p "$OUT"
az vm run-command invoke -g "$RESOURCE_GROUP" -n "$VM" --command-id RunShellScript --scripts @"$SCRIPT" \
  --query "value[0].message" -o tsv > "$LOG_FILE" || true
cat "$LOG_FILE"
if ! grep -q 'DEPLOY_OK' "$LOG_FILE"; then
  echo
  echo "Rolling out on the VM failed (above: the last lines of its output)."
  echo "Full log on the VM: /var/log/agent-orchestrator-deploy.log (docs/AZURE_TEST_ENV.md, troubleshooting)."
  exit 1
fi
echo
echo "Deployed: ${UPDATES[*]%%=*}"
