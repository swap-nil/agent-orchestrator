#!/usr/bin/env bash
# Deploy (or update) the complete Azure test environment on one VM. Idempotent:
# re-run it to roll out code or configuration changes. Runs in Azure Cloud Shell
# (bash) as-is.
#
#   cp deploy/azure/test.env.example deploy/azure/test.env   # edit it
#   deploy/azure/deploy.sh [deploy/azure/test.env]
#
# Steps: preflight (quota, leftovers) -> resource group -> secrets (generated once,
# then reused from Key Vault) -> Azure resources (Bicep) -> container images (ACR
# build, pinned by digest) -> Entra ID apps -> VM files -> install on the VM (Run
# Command: Docker Compose) -> smoke checks. See docs/AZURE_TEST_ENV.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${1:-$ROOT/deploy/azure/test.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy deploy/azure/test.env.example)"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${PREFIX:?set PREFIX in $ENV_FILE}"
: "${ACME_EMAIL:?set ACME_EMAIL in $ENV_FILE (certificate account email)}"
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{2,11}$ ]] || { echo "PREFIX must be 3-12 lowercase letters or digits, starting with a letter"; exit 1; }
LOCATION="${LOCATION:-switzerlandnorth}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-$PREFIX}"
VM_SIZE="${VM_SIZE:-Standard_D4s_v5}"
OUT="$ROOT/deploy/azure/.out"
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$ROOT"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1"; exit 1; }; }
for tool in az openssl python3 ssh-keygen tar gzip base64 curl; do need "$tool"; done
py() { python3 -c "$@"; }

[ -n "${SUBSCRIPTION_ID:-}" ] && az account set --subscription "$SUBSCRIPTION_ID"
SUB_ID=$(az account show --query id -o tsv)
SUFFIX=$(printf '%s' "$SUB_ID/$RESOURCE_GROUP" | openssl dgst -sha256 | awk '{print $NF}' | cut -c1-6)
DNS_LABEL="${DNS_LABEL:-${APP_DNS_LABEL:-$PREFIX-app-$SUFFIX}}"   # APP_DNS_LABEL: name in older test.env files
echo "subscription $SUB_ID, resource group $RESOURCE_GROUP, region $LOCATION, VM $VM_SIZE"

log "Preflight"
# Resource providers (registration is idempotent and quick when already registered).
for provider in Microsoft.Compute Microsoft.Network Microsoft.KeyVault Microsoft.ContainerRegistry \
    Microsoft.CognitiveServices Microsoft.OperationalInsights Microsoft.Insights Microsoft.ManagedIdentity; do
  state=$(az provider show -n "$provider" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)
  if [ "$state" != Registered ]; then
    echo "  registering $provider"
    az provider register -n "$provider" --wait -o none
  fi
done

# vCPU quota and size availability: fail fast with a clear message instead of a Bicep error.
az vm list-usage -l "$LOCATION" -o json > "$OUT/usage.json"
az vm list-skus -l "$LOCATION" --resource-type virtualMachines --size "$VM_SIZE" -o json > "$OUT/skus.json"
py 'import json, sys
usage = {u["name"]["value"]: u for u in json.load(open(sys.argv[1]))}
size = sys.argv[3]
sku = next((s for s in json.load(open(sys.argv[2])) if s["name"] == size), None)
problems = []
if sku is None:
    problems.append(f"{size} is not offered in this region")
else:
    blocked = [r.get("reasonCode") for r in sku.get("restrictions", []) if r.get("type") == "Location"]
    if blocked:
        problems.append(f"{size} is restricted for this subscription: {blocked}")
    vcpus = int(next(c["value"] for c in sku["capabilities"] if c["name"] == "vCPUs"))
    print("  %-34s %7s %7s %7s" % ("quota", "needed", "in use", "limit"))
    for family, label in ((sku["family"], sku["family"]), ("cores", "Total Regional vCPUs")):
        u = usage.get(family, {"currentValue": 0, "limit": 0})
        free = int(u["limit"]) - int(u["currentValue"])
        print("  %-34s %7s %7s %7s" % (label, vcpus, u["currentValue"], u["limit"]))
        if vcpus > free:
            problems.append(f"{label}: need {vcpus} vCPUs, only {free} free")
if problems:
    print("\nCannot create the VM:\n  - " + "\n  - ".join(problems))
    print("\nFix: request more quota (Portal > Quotas > Compute, this region), or set VM_SIZE in test.env"
          " to a size of a family that has quota (docs/AZURE_TEST_ENV.md, section 2).")
    sys.exit(1)' "$OUT/usage.json" "$OUT/skus.json" "$VM_SIZE"

# The earlier AKS-based test environment used the same resource group name.
if [ "$(az group exists -n "$RESOURCE_GROUP")" = true ]; then
  legacy=$(az resource list -g "$RESOURCE_GROUP" --query "[?type=='Microsoft.ContainerService/managedClusters' || type=='Microsoft.Cache/redisEnterprise' || type=='Microsoft.DBforPostgreSQL/flexibleServers'].name" -o tsv)
  if [ -n "$legacy" ]; then
    echo "Resource group $RESOURCE_GROUP still contains resources of the earlier AKS-based environment:"
    while read -r name; do echo "  - $name"; done <<< "$legacy"
    echo "They are no longer used and keep costing money. Remove them first with deploy/azure/destroy.sh,"
    echo "or set RESOURCE_GROUP in $ENV_FILE to a new resource group."
    exit 1
  fi
fi

# Soft-deleted leftovers of a deleted resource group block names that are reused:
# the Key Vault is recovered, the Speech account purged.
deleted_in_group() {  # json file, name prefix -> name of the soft-deleted resource from this resource group
  py 'import json, sys
group = "/resourcegroups/" + sys.argv[3].lower() + "/"
for r in json.load(open(sys.argv[1])):
    rid = (r.get("properties") or {}).get("vaultId") or r.get("id") or ""
    if r["name"].startswith(sys.argv[2]) and group in rid.lower():
        print(r["name"])
        break' "$1" "$2" "$RESOURCE_GROUP"
}
az keyvault list-deleted --resource-type vault -o json > "$OUT/deleted-kv.json" 2>/dev/null || echo '[]' > "$OUT/deleted-kv.json"
az cognitiveservices account list-deleted -o json > "$OUT/deleted-speech.json" 2>/dev/null || echo '[]' > "$OUT/deleted-speech.json"
DELETED_KV=$(deleted_in_group "$OUT/deleted-kv.json" "kv-$PREFIX-")
DELETED_SPEECH=$(deleted_in_group "$OUT/deleted-speech.json" "speech-$PREFIX-")

if [ "$(az account show --query user.type -o tsv)" = "servicePrincipal" ]; then
  DEPLOYER_ID=$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv); DEPLOYER_TYPE=ServicePrincipal
else
  DEPLOYER_ID=$(az ad signed-in-user show --query id -o tsv); DEPLOYER_TYPE=User
fi

log "Resource group"
az group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none
if [ -n "$DELETED_KV" ]; then
  echo "Recovering soft-deleted Key Vault $DELETED_KV (keeps the generated secrets)"
  az keyvault recover --name "$DELETED_KV" -o none
fi
if [ -n "$DELETED_SPEECH" ]; then
  echo "Purging soft-deleted Speech account $DELETED_SPEECH"
  az cognitiveservices account purge -l "$LOCATION" -g "$RESOURCE_GROUP" -n "$DELETED_SPEECH"
fi

log "Secrets (generated on the first run, then read back from Key Vault)"
KV=$(az keyvault list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true)
existing() { if [ -n "$KV" ]; then az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv 2>/dev/null || true; fi; }
b64url() { openssl rand "$1" | base64 | tr '+/' '-_' | tr -d '\n'; }   # keeps '=' padding (Fernet keys need it)
keep() { local v; v=$(existing "$1"); if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi; }
PG_PASSWORD=$(keep pg-admin-password "$(openssl rand -hex 24)")
REDIS_PASSWORD=$(keep redis-password "$(openssl rand -hex 24)")
SESSION_KEY=$(keep orch-session-key "$(b64url 32)")
APPROVAL_KEY=$(keep orch-approval-key "$(b64url 48 | tr -d '=')")
DISPATCH_KEY=$(keep ma-dispatch-key "$(b64url 48 | tr -d '=')")
TEMPORAL_KEY=$(keep orch-temporal-payload-key "$(b64url 32)")
LIVEKIT_KEY=$(keep livekit-api-key "API$(openssl rand -hex 6)")
LIVEKIT_SECRET=$(keep livekit-api-secret "$(b64url 32 | tr -d '=')")
COOKIE_SECRET=$(keep console-cookie-secret "$(b64url 32)")
[ -f "$OUT/vm_ssh" ] || ssh-keygen -t rsa -b 4096 -N "" -C "vm-$PREFIX" -f "$OUT/vm_ssh" -q

log "Azure resources (Bicep; about 5 minutes on the first run)"
PARAMS=$(umask 077; mktemp "$OUT/params.XXXXXX.json")
trap 'rm -f "$PARAMS"' EXIT
SSH_PUB=$(cat "$OUT/vm_ssh.pub")
export PREFIX DNS_LABEL DEPLOYER_ID DEPLOYER_TYPE SSH_PUB VM_SIZE PG_PASSWORD REDIS_PASSWORD SESSION_KEY APPROVAL_KEY \
  DISPATCH_KEY TEMPORAL_KEY LIVEKIT_KEY LIVEKIT_SECRET COOKIE_SECRET
export SSH_SOURCE_CIDR="${SSH_SOURCE_CIDR:-}"
# shellcheck disable=SC2016  # Python source: "$schema" is a JSON key, not a shell variable
py 'import json, os, sys
e = os.environ
p = {"prefix": e["PREFIX"], "dnsLabel": e["DNS_LABEL"], "deployerPrincipalId": e["DEPLOYER_ID"],
     "deployerPrincipalType": e["DEPLOYER_TYPE"], "sshPublicKey": e["SSH_PUB"], "sshSourceCidr": e["SSH_SOURCE_CIDR"],
     "vmSize": e["VM_SIZE"], "pgPassword": e["PG_PASSWORD"], "redisPassword": e["REDIS_PASSWORD"],
     "sessionKey": e["SESSION_KEY"], "approvalKey": e["APPROVAL_KEY"], "dispatchKey": e["DISPATCH_KEY"],
     "temporalPayloadKey": e["TEMPORAL_KEY"], "livekitApiKey": e["LIVEKIT_KEY"], "livekitApiSecret": e["LIVEKIT_SECRET"],
     "consoleCookieSecret": e["COOKIE_SECRET"]}
json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in p.items()}}, open(sys.argv[1], "w"))' "$PARAMS"
az deployment group create -g "$RESOURCE_GROUP" -n "$PREFIX-$(date -u +%Y%m%d%H%M%S)" \
  -f deploy/azure/main.bicep -p @"$PARAMS" --query properties.outputs -o json > "$OUT/outputs.json"
rm -f "$PARAMS"
out() { py 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["value"])' "$OUT/outputs.json" "$1"; }
VM=$(out vmName); ACR=$(out acrName); ACR_SERVER=$(out acrLoginServer); KV=$(out keyVaultName); APP_HOST=$(out appHost)

log "Container images (built in ACR, pinned by digest)"
if [ "${SKIP_BUILD:-0}" = "1" ] && [ -f "$OUT/images.env" ]; then
  echo "SKIP_BUILD=1: reusing images from $OUT/images.env"
else
  TAG=$(date -u +%Y%m%d%H%M%S)
  build() {  # repository extras -> repository@digest
    az acr build -r "$ACR" -t "$1:$TAG" -f deploy/docker/Dockerfile --build-arg EXTRAS="$2" . -o none >&2
    printf '%s/%s@%s' "$ACR_SERVER" "$1" "$(az acr repository show -n "$ACR" --image "$1:$TAG" --query digest -o tsv)"
  }
  {
    echo "IMG_ORCHESTRATOR=$(build orchestrator orchestrator)"
    echo "IMG_AGENT=$(build master-agent agent)"
    echo "IMG_TOKEN_SERVICE=$(build token-service token-service)"
    echo "IMG_MOCK=$(build mock mock)"
  } > "$OUT/images.env.tmp" && mv "$OUT/images.env.tmp" "$OUT/images.env"
fi

log "Entra ID app registrations"
python3 deploy/azure/scripts/entra_setup.py --prefix "$PREFIX" --host "$APP_HOST" \
  --key-vault "$KV" --identities "$OUT/outputs.json" --agents config/agents.yaml \
  --test-users "${TEST_USERS:-}" --out "$OUT/entra.json"

log "VM files"
# Public key of the approval signer, derived like orchestrator.approvals.ApprovalSigner
# (Ed25519 seed = SHA-256 of the key), for the trade agent to verify approvals.
py 'import sys,hashlib; sys.stdout.buffer.write(bytes.fromhex("302e020100300506032b657004220420") + hashlib.sha256(sys.argv[1].encode()).digest())' "$APPROVAL_KEY" \
  | openssl pkey -inform DER -pubout > "$OUT/approval_public_key.pem"
python3 deploy/azure/scripts/render_vm_bundle.py --outputs "$OUT/outputs.json" --entra "$OUT/entra.json" \
  --images "$OUT/images.env" --approval-public-key "$OUT/approval_public_key.pem" --acme-email "$ACME_EMAIL" \
  --allowed-source-ranges "${ALLOWED_SOURCE_RANGES:-}" --agents config/agents.yaml --policy policies/orchestrator.rego \
  --vm-dir deploy/azure/vm --out "$OUT/vm"

# Run Command runs one script on the VM: unpack the files (secrets stay on the VM), then bootstrap.sh.
SCRIPT="$OUT/vm-install.sh"
{
  echo '#!/bin/bash'
  echo 'set -euo pipefail'
  echo 'mkdir -p /opt/agent-orchestrator && cd /opt/agent-orchestrator'
  echo 'rm -rf env config'
  echo "base64 -d <<'BUNDLE' | tar -xzf - --no-same-owner"
  tar -czf - -C "$OUT/vm" . | base64
  echo 'BUNDLE'
  echo 'chmod 0755 bootstrap.sh smoke.sh'
  echo 'bash ./bootstrap.sh'
} > "$SCRIPT"
echo "  $(wc -c < "$SCRIPT") bytes"

log "Install on the VM $VM (Docker Compose; first run: about 10-15 minutes)"
az vm run-command invoke -g "$RESOURCE_GROUP" -n "$VM" --command-id RunShellScript --scripts @"$SCRIPT" \
  --query "value[0].message" -o tsv > "$OUT/vm-install.log" || true
cat "$OUT/vm-install.log"
if ! grep -q 'DEPLOY_OK' "$OUT/vm-install.log"; then
  echo
  echo "Installing on the VM failed (above: the last lines of its output)."
  echo "Full log on the VM: /var/log/agent-orchestrator-deploy.log (docs/AZURE_TEST_ENV.md, troubleshooting)."
  exit 1
fi

log "Smoke checks"
deploy/azure/scripts/smoke.sh "$APP_HOST" "$RESOURCE_GROUP" "$VM" || true

cat <<EOF

Test environment ready.
  Test client (sign in, talk or chat):  https://$APP_HOST/
  Command center:                        https://$APP_HOST/console
  LiveKit:                               wss://$APP_HOST
  SSH (needs SSH_SOURCE_CIDR):           ssh -i $OUT/vm_ssh azureuser@$APP_HOST
  Temporal UI and fake bank API: SSH tunnel, see docs/AZURE_TEST_ENV.md
Generated files (keep private): $OUT
EOF
