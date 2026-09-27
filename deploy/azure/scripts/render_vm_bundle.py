#!/usr/bin/env python3
"""Render the files the test VM runs from (called by deploy/azure/deploy.sh).

Copies the static files of deploy/azure/vm and generates, from the Bicep
outputs, the Entra ID app ids and the built images:

* ``settings.env``: images, host, Key Vault, registry and ids for Compose and bootstrap.sh;
* ``env/<service>.env``: non-secret settings per service, the same values the Helm
  charts set: Entra issuer, audiences and JWKS, the caller allowlist
  (managed identity client ids) and each service's managed identity;
* ``config/agents.yaml``: the agent registry with each agent's Entra app as audience;
* ``config/envoy.yaml``: the A2A gateway (a route, cluster and JWT provider per agent);
* ``config/orchestrator.rego``: the policy for OPA.

Secrets are not rendered here: bootstrap.sh reads them from Key Vault on the VM.
Standard library only (Azure Cloud Shell has no PyYAML); Envoy reads JSON as YAML.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

AUTHORITY = "https://login.microsoftonline.com"
ENVOY_API = "type.googleapis.com/envoy.extensions"
STATIC_FILES = ("docker-compose.yaml", "Caddyfile", "otel-collector.yaml", "bootstrap.sh", "smoke.sh")
AGENT_TIMEOUTS = {"trade-agent": "8s"}  # the rest: 5s (as in deploy/helm/domain-agents)
RATE_LIMIT_PER_SECOND = 200


def env_line(key: str, value: str) -> str:
    """One Compose env-file line. Single quotes are literal; multi-line values use \\n in double quotes."""
    if "\n" in value:
        if '"' in value or "\\" in value or "$" in value:
            raise ValueError(f"{key}: multi-line value must not contain quotes, backslashes or $")
        return key + '="' + value.replace("\n", "\\n") + '"'
    if "'" in value:
        raise ValueError(f"{key}: value must not contain a single quote")
    return f"{key}='{value}'"


def env_file(values: dict[str, str]) -> str:
    return "".join(env_line(k, v) + "\n" for k, v in values.items())


def read_agents(text: str) -> dict[str, list[str]]:
    """Agent name -> skills from the registry (the file is flat; no YAML parser needed)."""
    agents: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if m := re.match(r"\s*-\s*name:\s*([\w.-]+)", line):
            current = m.group(1)
            agents[current] = []
        elif (m := re.match(r"\s*skills:\s*\[(.*)\]", line)) and current:
            agents[current] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    if not agents or not all(agents.values()):
        raise SystemExit("could not read agents and skills from the registry")
    return agents


def rewrite_audiences(text: str, apps: dict[str, str]) -> str:
    """Set each agent's audience to api://<its app id>, so on-behalf-of tokens target that app."""
    out, current, done = [], None, set()
    for line in text.splitlines(keepends=True):
        if m := re.match(r"\s*-\s*name:\s*([\w.-]+)", line):
            current = m.group(1)
        elif (m := re.match(r"(\s*audience:\s*).*?(\r?\n?)$", line)) and current:
            if current not in apps:
                raise SystemExit(f"no Entra app for agent {current}")
            line = f"{m.group(1)}api://{apps[current]}{m.group(2)}"
            done.add(current)
        out.append(line)
    missing = set(read_agents(text)) - done
    if missing:
        raise SystemExit(f"agents without an audience line: {sorted(missing)}")
    return "".join(out)


def envoy_config(agents: list[str], apps: dict[str, str], tenant: str) -> dict:
    """A2A gateway: per-agent route requiring a delegated Entra token for that agent's app.

    Plain HTTP on the VM's private Docker network: only the orchestrator can reach it.
    Same filters as deploy/helm/domain-agents/templates/gateway-config.yaml.
    """
    issuer = f"{AUTHORITY}/{tenant}/v2.0"
    jwks = f"{AUTHORITY}/{tenant}/discovery/v2.0/keys"
    entra_host = AUTHORITY.removeprefix("https://")
    routes = [{"match": {"path": "/healthz"}, "direct_response": {"status": 200, "body": {"inline_string": "ok"}}}]
    for name in agents:
        route = {"cluster": name, "timeout": AGENT_TIMEOUTS.get(name, "5s")}
        if name == "trade-agent":
            route["retry_policy"] = {"num_retries": 0}  # writes are never retried by the gateway
        routes.append({
            "match": {"path": f"/agents/{name}"},
            "route": route,
            "typed_per_filter_config": {"envoy.filters.http.jwt_authn": {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.jwt_authn.v3.PerRouteConfig",
                "requirement_name": name}},
        })
    routes.append({"match": {"prefix": "/"}, "direct_response": {"status": 404, "body": {"inline_string": "unknown agent"}}})
    providers = {name: {
        "issuer": issuer,
        "audiences": [apps[name]],
        "remote_jwks": {"http_uri": {"uri": jwks, "cluster": "entra", "timeout": "5s"},
                        "cache_duration": "3600s", "async_fetch": {}},
        "forward": True,
    } for name in agents}
    clusters = [{
        "name": name,
        "type": "STRICT_DNS",
        "connect_timeout": "1s",
        "load_assignment": {"cluster_name": name, "endpoints": [{"lb_endpoints": [
            {"endpoint": {"address": {"socket_address": {"address": name, "port_value": 8080}}}}]}]},
        "circuit_breakers": {"thresholds": [{"max_connections": 200, "max_pending_requests": 100, "max_requests": 400}]},
        "outlier_detection": {"consecutive_5xx": 5, "interval": "10s", "base_ejection_time": "30s"},
    } for name in agents]
    clusters.append({
        "name": "entra",
        "type": "LOGICAL_DNS",
        "dns_lookup_family": "V4_ONLY",
        "connect_timeout": "2s",
        "load_assignment": {"cluster_name": "entra", "endpoints": [{"lb_endpoints": [
            {"endpoint": {"address": {"socket_address": {"address": entra_host, "port_value": 443}}}}]}]},
        "transport_socket": {"name": "envoy.transport_sockets.tls", "typed_config": {
            "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext",
            "sni": entra_host}},
    })
    return {
        "static_resources": {
            "listeners": [{
                "name": "a2a",
                # All interfaces of the gateway's container; only the Compose network reaches it.
                "address": {"socket_address": {"address": "0.0.0.0", "port_value": 8080}},  # noqa: S104
                "filter_chains": [{"filters": [{
                    "name": "envoy.filters.network.http_connection_manager",
                    "typed_config": {
                        "@type": f"{ENVOY_API}.filters.network.http_connection_manager.v3.HttpConnectionManager",
                        "stat_prefix": "a2a",
                        "request_timeout": "10s",
                        "max_request_headers_kb": 32,
                        "use_remote_address": True,
                        "route_config": {"name": "agents",
                                         "virtual_hosts": [{"name": "agents", "domains": ["*"], "routes": routes}]},
                        "http_filters": [
                            {"name": "envoy.filters.http.local_ratelimit", "typed_config": {
                                "@type": "type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit",
                                "stat_prefix": "a2a_rl",
                                "token_bucket": {"max_tokens": RATE_LIMIT_PER_SECOND, "tokens_per_fill": RATE_LIMIT_PER_SECOND,
                                                 "fill_interval": "1s"},
                                "filter_enabled": {"default_value": {"numerator": 100, "denominator": "HUNDRED"}},
                                "filter_enforced": {"default_value": {"numerator": 100, "denominator": "HUNDRED"}}}},
                            {"name": "envoy.filters.http.jwt_authn", "typed_config": {
                                "@type": "type.googleapis.com/envoy.extensions.filters.http.jwt_authn.v3.JwtAuthentication",
                                "providers": providers,
                                "requirement_map": {name: {"provider_name": name} for name in agents}}},
                            {"name": "envoy.filters.http.router", "typed_config": {
                                "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}},
                        ],
                    },
                }]}],
            }],
            "clusters": clusters,
        },
        "admin": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": 9901}}},
    }


def service_envs(*, tenant: str, host: str, speech_region: str, apps: dict, identities: dict[str, str],
                 agents: list[str], approval_public_key: str) -> dict[str, dict[str, str]]:
    """Non-secret environment per service (file name -> variables)."""
    issuer = f"{AUTHORITY}/{tenant}/v2.0"
    jwks = f"{AUTHORITY}/{tenant}/discovery/v2.0/keys"
    orch_app = apps["orchestrator"]
    orch_scope = f"api://{orch_app}/.default"
    ids = identities
    route_callers = {"turns": [ids["masterAgent"]], "sessions": [ids["tokenService"]],
                     "approvals": [ids["clientBackend"]], "workflows": [ids["masterAgent"], ids["clientBackend"]],
                     "admin": []}

    def caller(identity: str) -> dict[str, str]:  # calls the orchestrator with its managed identity
        return {"AZURE_TOKEN_SOURCE": "managed_identity", "AZURE_CLIENT_ID": ids[identity], "AZURE_TENANT_ID": tenant}

    envs = {
        "orchestrator": {
            "ORCH_CONFIG_FILE": "/app/config/orchestrator.test.yaml",
            # On-behalf-of: the orchestrator app trusts this managed identity (federated credential).
            "AZURE_CLIENT_ID": ids["orchestrator"],
            "AZURE_TENANT_ID": tenant,
            "ORCH__AUTH__JWT__ISSUER": issuer,
            "ORCH__AUTH__JWT__AUDIENCE": orch_app,
            "ORCH__AUTH__JWT__JWKS_URL": jwks,
            "ORCH__AUTH__USER_JWT__ISSUER": issuer,
            "ORCH__AUTH__USER_JWT__AUDIENCE": orch_app,
            "ORCH__AUTH__USER_JWT__JWKS_URL": jwks,
            "ORCH__AUTH__ROUTE_CALLERS": json.dumps(route_callers, separators=(",", ":")),
            "ORCH__IDENTITY__TOKEN_ENDPOINT": f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
            "ORCH__IDENTITY__CLIENT_ID": orch_app,
            "ORCH__COMMAND_CENTER__OPERATOR_JWT__ISSUER": issuer,
            "ORCH__COMMAND_CENTER__OPERATOR_JWT__AUDIENCE": apps["console"],
            "ORCH__COMMAND_CENTER__OPERATOR_JWT__JWKS_URL": jwks,
        },
        "token-service": {
            "TS_CONFIG_FILE": "/app/config/token_service.test.yaml",
            "TS__USER_JWT__ISSUER": issuer,
            "TS__USER_JWT__AUDIENCE": orch_app,
            "TS__USER_JWT__JWKS_URL": jwks,
            # Browsers join LiveKit through the public host (Caddy routes /rtc to LiveKit).
            "TS__LIVEKIT__URL": f"wss://{host}",
            "TS__ORCHESTRATOR_AUTH_SCOPE": orch_scope,
            **caller("tokenService"),
        },
        "master-agent": {
            "MA_CONFIG_FILE": "/app/config/master_agent.test.yaml",
            # Same VM, host network: LiveKit directly, without TLS.
            "LIVEKIT_URL": "ws://127.0.0.1:7880",
            "AZURE_SPEECH_REGION": speech_region,
            "XDG_CACHE_HOME": "/tmp/cache",  # noqa: S108 - inside the container
            "MA__ORCHESTRATOR__AUTH_SCOPE": orch_scope,
            **caller("masterAgent"),
        },
        "test-client": {
            "TENANT_ID": tenant,
            "AUTHORITY_HOST": AUTHORITY,
            "SPA_CLIENT_ID": apps["spa"],
            "ORCHESTRATOR_APP_ID": orch_app,
            "ORCHESTRATOR_URL": "http://orchestrator:8080",
            "ORCHESTRATOR_AUTH_SCOPE": orch_scope,
            "MOCK_BACKEND_URL": "http://mock-backend:8080",
            "TOKEN_SERVICE_PATH": "/v1/voice-sessions",
            **caller("clientBackend"),
        },
    }
    for name in agents:
        env = {"AGENT_NAMES": name, "MOCK_BACKEND_URL": "http://mock-backend:8080",
               "AGENT_AUDIENCE": apps["agents"][name], "AGENT_TOKEN_ISSUER": issuer, "AGENT_JWKS_URL": jwks}
        if name == "trade-agent":
            env["APPROVAL_PUBLIC_KEY"] = approval_public_key.strip() + "\n"
        envs[f"agent-{name}"] = env
    return envs


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def render(args: argparse.Namespace) -> None:
    outputs = {k: v["value"] for k, v in json.loads(Path(args.outputs).read_text(encoding="utf-8")).items()}
    entra = json.loads(Path(args.entra).read_text(encoding="utf-8"))
    images = read_env(Path(args.images))
    identities = {i["key"]: i["clientId"] for i in outputs["identities"]}
    tenant, host, apps = outputs["tenantId"], outputs["appHost"], entra["apps"]
    registry = Path(args.agents).read_text(encoding="utf-8")
    agents = list(read_agents(registry))
    missing = [a for a in agents if a not in apps["agents"]]
    if missing:
        raise SystemExit(f"no Entra app for agents {missing}; re-run entra_setup.py")

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "env").mkdir(parents=True)
    (out / "config").mkdir()
    for name in STATIC_FILES:
        shutil.copyfile(Path(args.vm_dir) / name, out / name)
    shutil.copyfile(args.policy, out / "config" / "orchestrator.rego")

    ranges = " ".join(r.strip() for r in args.allowed_source_ranges.split(",") if r.strip()) or "0.0.0.0/0 ::/0"
    settings = {
        "KEY_VAULT": outputs["keyVaultName"],
        "ACR_SERVER": outputs["acrLoginServer"],
        "TENANT_ID": tenant,
        "APP_HOST": host,
        "ACME_EMAIL": args.acme_email,
        "ALLOWED_SOURCE_RANGES": ranges,
        "CONSOLE_CLIENT_ID": apps["console"],
        **{k: images[k] for k in ("IMG_ORCHESTRATOR", "IMG_AGENT", "IMG_TOKEN_SERVICE", "IMG_MOCK")},
    }
    (out / "settings.env").write_text(env_file(settings), encoding="utf-8", newline="\n")
    envs = service_envs(tenant=tenant, host=host, speech_region=outputs["speechRegion"], apps=apps,
                        identities=identities, agents=agents,
                        approval_public_key=Path(args.approval_public_key).read_text(encoding="utf-8"))
    for name, values in envs.items():
        (out / "env" / f"{name}.env").write_text(env_file(values), encoding="utf-8", newline="\n")
    (out / "config" / "agents.yaml").write_text(rewrite_audiences(registry, apps["agents"]), encoding="utf-8", newline="\n")
    (out / "config" / "envoy.yaml").write_text(json.dumps(envoy_config(agents, apps["agents"], tenant), indent=1),
                                               encoding="utf-8", newline="\n")
    print(f"Rendered {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--outputs", required=True, help="Bicep outputs JSON")
    ap.add_argument("--entra", required=True, help="entra_setup.py output JSON")
    ap.add_argument("--images", required=True, help="images.env (IMG_*=repo@digest)")
    ap.add_argument("--approval-public-key", required=True, help="PEM file")
    ap.add_argument("--acme-email", required=True)
    ap.add_argument("--allowed-source-ranges", default="", help="comma-separated CIDRs; empty: everyone")
    ap.add_argument("--agents", required=True, help="agent registry (config/agents.yaml)")
    ap.add_argument("--policy", required=True, help="OPA policy (policies/orchestrator.rego)")
    ap.add_argument("--vm-dir", required=True, help="static VM files (deploy/azure/vm)")
    ap.add_argument("--out", required=True)
    render(ap.parse_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
