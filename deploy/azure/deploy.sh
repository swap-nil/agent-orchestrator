#!/usr/bin/env bash
# Deploy (or update) the complete Azure test environment. Idempotent: re-run it to
# roll out code or configuration changes. Runs in Azure Cloud Shell (bash) as-is.
#
#   cp deploy/azure/test.env.example deploy/azure/test.env   # edit it
#   deploy/azure/deploy.sh [deploy/azure/test.env]
#
# Steps: resource group -> secrets (generated once, then reused from Key Vault) ->
# Azure resources (Bicep) -> container images (ACR build, pinned by digest) ->
# Entra ID apps -> cert-manager -> Helm releases (platform, domain-agents,
# orchestrator, voice, edge) -> smoke checks. See docs/AZURE_TEST_ENV.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${1:-$ROOT/deploy/azure/test.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy deploy/azure/test.env.example)"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${PREFIX:?set PREFIX in $ENV_FILE}"
: "${ACME_EMAIL:?set ACME_EMAIL in $ENV_FILE (certificate account email)}"
LOCATION="${LOCATION:-switzerlandnorth}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-$PREFIX}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.21.2}"
OUT="$ROOT/deploy/azure/.out"
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$ROOT"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1"; exit 1; }; }
for tool in az kubectl helm openssl python3 ssh-keygen; do need "$tool"; done
py() { python3 -c "$@"; }

[ -n "${SUBSCRIPTION_ID:-}" ] && az account set --subscription "$SUBSCRIPTION_ID"
SUB_ID=$(az account show --query id -o tsv)
SUFFIX=$(printf '%s' "$SUB_ID/$RESOURCE_GROUP" | openssl dgst -sha256 | awk '{print $NF}' | cut -c1-6)
APP_DNS_LABEL="${APP_DNS_LABEL:-$PREFIX-app-$SUFFIX}"
LIVEKIT_DNS_LABEL="${LIVEKIT_DNS_LABEL:-$PREFIX-lk-$SUFFIX}"
echo "subscription $SUB_ID, resource group $RESOURCE_GROUP, region $LOCATION"
AKS_NODE_SIZE="${AKS_NODE_SIZE:-Standard_D4s_v5}"
LIVEKIT_VM_SIZE="${LIVEKIT_VM_SIZE:-Standard_D2s_v5}"

log "vCPU quota check ($LOCATION)"
# Fail fast with a clear message instead of a Bicep preflight error: AKS needs its
# 2 initial nodes plus 1 surge node for upgrades; the LiveKit VM needs one more.
az vm list-usage -l "$LOCATION" -o json > "$OUT/usage.json"
az vm list-skus -l "$LOCATION" --resource-type virtualMachines -o json \
  --query "[?name=='$AKS_NODE_SIZE' || name=='$LIVEKIT_VM_SIZE']" > "$OUT/skus.json"
py 'import json, sys
usage = {u["name"]["value"]: u for u in json.load(open(sys.argv[1]))}
skus = {s["name"]: s for s in json.load(open(sys.argv[2]))}
need, problems = {}, []
for size, count, role in ((sys.argv[3], 3, "AKS nodes (2 + 1 upgrade surge)"), (sys.argv[4], 1, "LiveKit VM")):
    sku = skus.get(size)
    if sku is None:
        problems.append(f"{size} ({role}) is not offered in this region")
        continue
    blocked = [r.get("reasonCode") for r in sku.get("restrictions", []) if r.get("type") == "Location"]
    if blocked:
        problems.append(f"{size} ({role}) is restricted for this subscription: {blocked}")
    vcpus = int(next(c["value"] for c in sku["capabilities"] if c["name"] == "vCPUs"))
    need[sku["family"]] = need.get(sku["family"], 0) + count * vcpus
need["cores"] = sum(need.values())
print("  %-34s %7s %7s %7s" % ("quota", "needed", "in use", "limit"))
for family, n in need.items():
    u = usage.get(family, {"currentValue": 0, "limit": 0})
    free = int(u["limit"]) - int(u["currentValue"])
    label = "Total Regional vCPUs" if family == "cores" else family
    print("  %-34s %7s %7s %7s" % (label, n, u["currentValue"], u["limit"]))
    if n > free:
        problems.append(f"{label}: need {n} vCPUs, only {free} free")
if problems:
    print("\nNot enough quota or unavailable sizes:\n  - " + "\n  - ".join(problems))
    print("\nFix: request more quota (Portal > Quotas > Compute, this region), or set AKS_NODE_SIZE /"
          " LIVEKIT_VM_SIZE in test.env to sizes of a family that has quota (docs/AZURE_TEST_ENV.md, section 2).")
    sys.exit(1)' "$OUT/usage.json" "$OUT/skus.json" "$AKS_NODE_SIZE" "$LIVEKIT_VM_SIZE"

if [ "$(az account show --query user.type -o tsv)" = "servicePrincipal" ]; then
  DEPLOYER_ID=$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv); DEPLOYER_TYPE=ServicePrincipal
else
  DEPLOYER_ID=$(az ad signed-in-user show --query id -o tsv); DEPLOYER_TYPE=User
fi

log "Resource group"
az group create -n "$RESOURCE_GROUP" -l "$LOCATION" -o none

log "Secrets (generated on the first run, then read back from Key Vault)"
KV=$(az keyvault list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true)
existing() { [ -n "$KV" ] && az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv 2>/dev/null || true; }
b64url() { openssl rand "$1" | base64 | tr '+/' '-_' | tr -d '\n'; }   # keeps '=' padding (Fernet keys need it)
keep() { local v; v=$(existing "$1"); if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi; }
PG_PASSWORD=$(keep pg-admin-password "$(openssl rand -hex 24)")
SESSION_KEY=$(keep orch-session-key "$(b64url 32)")
APPROVAL_KEY=$(keep orch-approval-key "$(b64url 48 | tr -d '=')")
DISPATCH_KEY=$(keep ma-dispatch-key "$(b64url 48 | tr -d '=')")
TEMPORAL_KEY=$(keep orch-temporal-payload-key "$(b64url 32)")
LIVEKIT_KEY=$(keep livekit-api-key "API$(openssl rand -hex 6)")
LIVEKIT_SECRET=$(keep livekit-api-secret "$(b64url 32 | tr -d '=')")
COOKIE_SECRET=$(keep console-cookie-secret "$(b64url 32)")
[ -f "$OUT/livekit_ssh" ] || ssh-keygen -t rsa -b 4096 -N "" -C "livekit-$PREFIX" -f "$OUT/livekit_ssh" -q

log "Azure resources (Bicep; about 20 minutes on the first run)"
PARAMS=$(umask 077; mktemp "$OUT/params.XXXXXX.json")
trap 'rm -f "$PARAMS"' EXIT
export PREFIX APP_DNS_LABEL LIVEKIT_DNS_LABEL DEPLOYER_ID DEPLOYER_TYPE ACME_EMAIL PG_PASSWORD SESSION_KEY APPROVAL_KEY \
  DISPATCH_KEY TEMPORAL_KEY LIVEKIT_KEY LIVEKIT_SECRET COOKIE_SECRET
SSH_PUB=$(cat "$OUT/livekit_ssh.pub")
export SSH_PUB SSH_SOURCE_CIDR="${SSH_SOURCE_CIDR:-}"
export AKS_NODE_SIZE LIVEKIT_VM_SIZE
# shellcheck disable=SC2016  # Python source: "$schema" is a JSON key, not a shell variable
py 'import json, os, sys
e = os.environ
p = {"prefix": e["PREFIX"], "appDnsLabel": e["APP_DNS_LABEL"], "livekitDnsLabel": e["LIVEKIT_DNS_LABEL"],
     "deployerPrincipalId": e["DEPLOYER_ID"], "deployerPrincipalType": e["DEPLOYER_TYPE"], "acmeEmail": e["ACME_EMAIL"],
     "livekitSshPublicKey": e["SSH_PUB"], "sshSourceCidr": e["SSH_SOURCE_CIDR"], "aksNodeSize": e["AKS_NODE_SIZE"],
     "livekitVmSize": e["LIVEKIT_VM_SIZE"],
     "pgAdminPassword": e["PG_PASSWORD"], "sessionKey": e["SESSION_KEY"], "approvalKey": e["APPROVAL_KEY"],
     "dispatchKey": e["DISPATCH_KEY"], "temporalPayloadKey": e["TEMPORAL_KEY"], "livekitApiKey": e["LIVEKIT_KEY"],
     "livekitApiSecret": e["LIVEKIT_SECRET"], "consoleCookieSecret": e["COOKIE_SECRET"]}
json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in p.items()}}, open(sys.argv[1], "w"))' "$PARAMS"
az deployment group create -g "$RESOURCE_GROUP" -n "$PREFIX-$(date -u +%Y%m%d%H%M%S)" \
  -f deploy/azure/main.bicep -p @"$PARAMS" --query properties.outputs -o json > "$OUT/outputs.json"
rm -f "$PARAMS"
out() { py 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["value"])' "$OUT/outputs.json" "$1"; }
AKS=$(out aksName); ACR=$(out acrName); ACR_SERVER=$(out acrLoginServer); KV=$(out keyVaultName)
OIDC=$(out oidcIssuer); TENANT=$(out tenantId); APP_HOST=$(out appHost); LIVEKIT_HOST=$(out livekitHost)

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
# shellcheck disable=SC1091
source "$OUT/images.env"

log "Entra ID app registrations"
python3 deploy/azure/scripts/entra_setup.py --prefix "$PREFIX" --host "$APP_HOST" --oidc-issuer "$OIDC" \
  --key-vault "$KV" --identities "$OUT/outputs.json" --agents config/agents.yaml \
  --test-users "${TEST_USERS:-}" --out "$OUT/entra.json"

log "Shared Helm values"
# Public key of the approval signer, derived like orchestrator.approvals.ApprovalSigner
# (Ed25519 seed = SHA-256 of the key), for the trade agent to verify approvals.
APPROVAL_PUBLIC_KEY=$(py 'import sys,hashlib; sys.stdout.buffer.write(bytes.fromhex("302e020100300506032b657004220420") + hashlib.sha256(sys.argv[1].encode()).digest())' "$APPROVAL_KEY" \
  | openssl pkey -inform DER -pubout)
export TENANT APP_HOST APP_DNS_LABEL LIVEKIT_HOST KV APPROVAL_PUBLIC_KEY IMG_ORCHESTRATOR IMG_AGENT IMG_TOKEN_SERVICE IMG_MOCK LOCATION
py 'import json, os, sys
e = os.environ
outputs, entra = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
ids = {i["key"]: i["clientId"] for i in outputs["identities"]["value"]}
g = {"tenantId": e["TENANT"], "authorityHost": "https://login.microsoftonline.com", "host": e["APP_HOST"],
     "dnsLabel": e["APP_DNS_LABEL"], "livekitUrl": "wss://" + e["LIVEKIT_HOST"], "keyVaultName": e["KV"],
     "speechRegion": e["LOCATION"], "approvalPublicKey": e["APPROVAL_PUBLIC_KEY"] + "\n", "apps": entra["apps"], "identities": ids,
     "images": {"orchestrator": e["IMG_ORCHESTRATOR"], "agent": e["IMG_AGENT"], "tokenService": e["IMG_TOKEN_SERVICE"], "mock": e["IMG_MOCK"]}}
json.dump({"global": g}, open(sys.argv[3], "w"), indent=2)' "$OUT/outputs.json" "$OUT/entra.json" "$OUT/global.yaml"

log "Cluster access and namespaces"
az aks get-credentials -g "$RESOURCE_GROUP" -n "$AKS" --overwrite-existing -o none
for ns in platform agents orchestrator voice edge; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
kubectl -n platform create secret generic appinsights \
  --from-literal=connection-string="$(az keyvault secret show --vault-name "$KV" -n appinsights-connection-string --query value -o tsv)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

log "cert-manager $CERT_MANAGER_VERSION"
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager --version "$CERT_MANAGER_VERSION" \
  -n cert-manager --create-namespace --set crds.enabled=true --wait --timeout 10m
kubectl wait --for condition=established crd/nginxingresscontrollers.approuting.kubernetes.azure.com --timeout=300s

log "Helm releases"
G=(-f "$OUT/global.yaml")
helm upgrade --install platform deploy/helm/platform -n platform "${G[@]}" --set otel.azureMonitor=true --wait --timeout 10m
helm upgrade --install domain-agents deploy/helm/domain-agents -n agents "${G[@]}" --wait --timeout 10m
helm upgrade --install orchestrator deploy/helm/orchestrator -n orchestrator "${G[@]}" -f deploy/azure/values/orchestrator.yaml \
  --set-file config.orchestratorYaml=config/orchestrator.test.yaml --set-file config.intentsYaml=config/intents.yaml \
  --set-file config.agentsYaml=config/agents.yaml --set-file config.evalsYaml=config/evals.yaml --wait --timeout 15m
helm upgrade --install edge deploy/helm/edge -n edge "${G[@]}" --set-file config.tokenServiceYaml=config/token_service.test.yaml \
  --set certManager.email="$ACME_EMAIL" --set ingress.className="$PREFIX-public" \
  ${ALLOWED_SOURCE_RANGES:+--set "ingress.allowedSourceRanges={$ALLOWED_SOURCE_RANGES}"} --wait --timeout 10m
# The master agent becomes ready once the LiveKit VM has finished bootstrapping (first run: a few minutes).
helm upgrade --install voice deploy/helm/voice -n voice "${G[@]}" --set-file config.masterAgentYaml=config/master_agent.test.yaml
kubectl -n voice rollout status deploy/master-agent --timeout=15m || \
  echo "WARNING: master agent not ready yet; check the LiveKit VM (see docs/AZURE_TEST_ENV.md, troubleshooting)"

log "Smoke checks"
deploy/azure/scripts/smoke.sh "$APP_HOST" "$LIVEKIT_HOST" || true

cat <<EOF

Test environment ready.
  Test client (sign in, talk or chat):  https://$APP_HOST/
  Command center:                        https://$APP_HOST/console
  LiveKit:                               wss://$LIVEKIT_HOST
  Temporal UI:      kubectl -n platform port-forward svc/temporal 8233   -> http://localhost:8233
  Fake bank API:    kubectl -n agents port-forward svc/mock-backend 8081:8080 -> http://localhost:8081/docs
Generated files (keep private): $OUT
EOF
