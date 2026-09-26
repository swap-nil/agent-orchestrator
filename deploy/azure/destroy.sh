#!/usr/bin/env bash
# Remove the Azure test environment: the resource group (and everything in it),
# the purged Key Vault, and the Entra ID app registrations with the prefix.
#   deploy/azure/destroy.sh [deploy/azure/test.env]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1090
source "${1:-$ROOT/deploy/azure/test.env}"
: "${PREFIX:?}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-$PREFIX}"
[ -n "${SUBSCRIPTION_ID:-}" ] && az account set --subscription "$SUBSCRIPTION_ID"

read -r -p "Delete resource group $RESOURCE_GROUP and Entra apps '$PREFIX-*'? Type the prefix to confirm: " answer
[ "$answer" = "$PREFIX" ] || { echo "aborted"; exit 1; }

KV=$(az keyvault list -g "$RESOURCE_GROUP" --query "[0].name" -o tsv 2>/dev/null || true)
echo "Deleting resource group $RESOURCE_GROUP (takes a while)..."
az group delete -n "$RESOURCE_GROUP" --yes
if [ -n "$KV" ]; then
  echo "Purging Key Vault $KV"
  az keyvault purge --name "$KV" || true
fi
for id in $(az ad app list --filter "startswith(displayName,'$PREFIX-')" --query "[].id" -o tsv); do
  echo "Deleting Entra app $(az ad app show --id "$id" --query displayName -o tsv)"
  az ad app delete --id "$id"
done
rm -rf "$ROOT/deploy/azure/.out"
echo "Done."
