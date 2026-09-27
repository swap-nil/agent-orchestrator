#!/bin/bash
# Install or update the test environment on the VM. Run as root from
# /opt/agent-orchestrator; deploy/azure/deploy.sh ships this directory and runs it
# through Run Command on every deployment. Idempotent.
#
# 1. Read the secrets from Key Vault with the VM's system-assigned identity and
#    write them to secrets/*.env (root only) and livekit/livekit.yaml.
# 2. Log in to the container registry with the same identity.
# 3. docker compose pull + up; containers whose configuration changed are recreated.
# 4. Wait until the services are healthy. The last line is DEPLOY_OK on success.
set -euo pipefail
# Run Command starts scripts with a minimal environment.
export HOME="${HOME:-/root}" PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
cd "$(dirname "$0")"
LOG=/var/log/agent-orchestrator-deploy.log
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date -u +%FT%TZ) deploy"

# shellcheck disable=SC1091
source ./settings.env
: "${KEY_VAULT:?}" "${ACR_SERVER:?}" "${TENANT_ID:?}"

echo "Waiting for first-boot setup (cloud-init)"
cloud-init status --wait >/dev/null || true
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker or Compose missing: first-boot setup failed, see /var/log/cloud-init-output.log"
  exit 1
fi
systemctl is-active --quiet docker || systemctl start docker

imds_token() {  # resource -> access token of the VM's system-assigned identity
  curl -sf --max-time 10 -H Metadata:true -G "http://169.254.169.254/metadata/identity/oauth2/token" \
    --data-urlencode "api-version=2018-02-01" --data-urlencode "resource=$1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

# Read a Key Vault secret; retry while the role assignment propagates (first run).
secret() {
  local name=$1 token value
  for _ in $(seq 1 60); do
    if token=$(imds_token https://vault.azure.net) && value=$(curl -sf --max-time 10 \
        -H "Authorization: Bearer $token" "https://$KEY_VAULT.vault.azure.net/secrets/$name?api-version=7.4" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])'); then
      printf '%s' "$value"
      return 0
    fi
    sleep 10
  done
  echo "could not read secret $name from Key Vault $KEY_VAULT" >&2
  return 1
}

echo "Reading secrets from Key Vault $KEY_VAULT"
SESSION_KEY=$(secret orch-session-key)
APPROVAL_KEY=$(secret orch-approval-key)
TEMPORAL_KEY=$(secret orch-temporal-payload-key)
DISPATCH_KEY=$(secret ma-dispatch-key)
LIVEKIT_KEY=$(secret livekit-api-key)
LIVEKIT_SECRET=$(secret livekit-api-secret)
COOKIE_SECRET=$(secret console-cookie-secret)
CONSOLE_SECRET=$(secret console-client-secret)
PG_PASSWORD=$(secret pg-admin-password)
REDIS_PASSWORD=$(secret redis-password)
SPEECH_KEY=$(secret speech-key)
APPINSIGHTS=$(secret appinsights-connection-string)
REDIS_URL="redis://:$REDIS_PASSWORD@redis:6379/0"

umask 077
mkdir -p secrets livekit
# Values are single-quoted (literal) for Compose; none of them contains a quote.
envfile() {  # file KEY=value...
  local file=$1; shift
  printf "%s\n" "$@" | sed -E "s/^([A-Z0-9_]+)=(.*)$/\1='\2'/" > "$file.tmp" && mv "$file.tmp" "$file"
}
envfile secrets/orchestrator.env "ORCH_SESSION_KEY=$SESSION_KEY" "ORCH_APPROVAL_KEY=$APPROVAL_KEY" \
  "ORCH_TEMPORAL_PAYLOAD_KEY=$TEMPORAL_KEY" "ORCH_REDIS_URL=$REDIS_URL" \
  "ORCH_AUDIT_DSN=postgresql://orch:$PG_PASSWORD@postgres:5432/orch"
envfile secrets/token-service.env "MA_DISPATCH_KEY=$DISPATCH_KEY" "LIVEKIT_API_KEY=$LIVEKIT_KEY" \
  "LIVEKIT_API_SECRET=$LIVEKIT_SECRET" "TS_REDIS_URL=$REDIS_URL"
envfile secrets/master-agent.env "MA_DISPATCH_KEY=$DISPATCH_KEY" "LIVEKIT_API_KEY=$LIVEKIT_KEY" \
  "LIVEKIT_API_SECRET=$LIVEKIT_SECRET" "AZURE_SPEECH_KEY=$SPEECH_KEY"
envfile secrets/console-proxy.env "OAUTH2_PROXY_CLIENT_SECRET=$CONSOLE_SECRET" "OAUTH2_PROXY_COOKIE_SECRET=$COOKIE_SECRET"
envfile secrets/postgres.env "POSTGRES_PASSWORD=$PG_PASSWORD"
envfile secrets/redis.env "REDIS_PASSWORD=$REDIS_PASSWORD"
envfile secrets/otel.env "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPINSIGHTS"

# LiveKit: the public IP (found with STUN) for browsers, plus the private IP so the
# master agent on this VM reaches the media ports directly.
cat > livekit/livekit.yaml.tmp <<EOF
port: 7880
logging:
  level: info
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
  advertise_internal_ip: true
keys:
  "$LIVEKIT_KEY": "$LIVEKIT_SECRET"
EOF
mv livekit/livekit.yaml.tmp livekit/livekit.yaml
chmod 0644 livekit/livekit.yaml   # read by the container user; the directory stays root-only
chmod 0711 livekit
umask 022

# Temporal's SQLite database lives on the VM disk; the image runs as uid 1000.
install -d -o 1000 -g 1000 data/temporal

# Compose settings: images and ids (settings.env) plus checksums of the mounted
# configuration files, so a changed file recreates the container that reads it.
hash() { cat "$@" | sha256sum | cut -c1-16; }
{
  cat settings.env
  echo "ORCHESTRATOR_CONFIG_HASH=$(hash config/agents.yaml)"
  echo "OPA_CONFIG_HASH=$(hash config/orchestrator.rego)"
  echo "ENVOY_CONFIG_HASH=$(hash config/envoy.yaml)"
  echo "CADDY_CONFIG_HASH=$(hash Caddyfile)"
  echo "OTEL_CONFIG_HASH=$(hash otel-collector.yaml)"
  echo "LIVEKIT_CONFIG_HASH=$(hash livekit/livekit.yaml)"
} > .env

echo "Logging in to $ACR_SERVER"
login_acr() {
  local aad refresh
  aad=$(imds_token https://management.azure.com/) || return 1
  refresh=$(curl -sf --max-time 20 -X POST "https://$ACR_SERVER/oauth2/exchange" \
      --data-urlencode grant_type=access_token --data-urlencode "service=$ACR_SERVER" \
      --data-urlencode "tenant=$TENANT_ID" --data-urlencode "access_token=$aad" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["refresh_token"])') || return 1
  printf '%s' "$refresh" | docker login "$ACR_SERVER" -u 00000000-0000-0000-0000-000000000000 --password-stdin >/dev/null
}

# Pull, retrying while the AcrPull role assignment propagates (first run).
for attempt in $(seq 1 20); do
  if login_acr && docker compose pull --quiet; then
    break
  fi
  [ "$attempt" = 20 ] && { echo "could not pull the images"; exit 1; }
  echo "pull failed (attempt $attempt), retrying in 30s"
  sleep 30
done

diagnose() {  # services that are not running or not healthy, with their last log lines
  docker compose ps -a
  for service in "$@"; do
    echo "--- logs $service"
    docker compose logs --tail 20 --no-log-prefix "$service" 2>&1 | cut -c1-300
  done
}

echo "Starting services"
if ! docker compose up -d --remove-orphans; then
  diagnose orchestrator-migrate orchestrator
  exit 1
fi

# Wait until every service with a health check is healthy and the rest are running.
echo "Waiting for services to become healthy (up to 10 minutes)"
deadline=$(( $(date +%s) + 600 ))
while :; do
  pending=()
  for service in $(docker compose config --services); do
    [ "$service" = orchestrator-migrate ] && continue
    id=$(docker compose ps -a -q "$service")
    state=$( [ -n "$id" ] && docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" || echo missing/none)
    case "$state" in running/healthy|running/none) ;; *) pending+=("$service=$state") ;; esac
  done
  [ ${#pending[@]} -eq 0 ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Not ready: ${pending[*]}"
    diagnose "${pending[@]%%=*}"
    exit 1
  fi
  sleep 10
done
docker image prune -f >/dev/null || true
docker compose ps
echo DEPLOY_OK
