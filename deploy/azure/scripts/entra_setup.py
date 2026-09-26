#!/usr/bin/env python3
"""Entra ID app registrations for the Azure test environment (idempotent).

Creates or updates, all prefixed with --prefix:

* one app per domain agent (config/agents.yaml): API ``api://<app-id>``, one
  delegated scope per skill, v2 tokens;
* the orchestrator app: API for user tokens (scope ``access_as_user``), app
  roles ``low``/``standard``/``stepup`` carrying the assurance level, app role
  ``Orchestrator.Call`` for the calling workloads, delegated permissions on
  every agent scope (on-behalf-of), and a federated credential for the
  orchestrator's Kubernetes service account (no client secret);
* the console app (oauth2-proxy sign-in) with app roles ``CC.*`` and a client
  secret stored in Key Vault as ``console-client-secret``;
* the test client SPA with permission to call the orchestrator.

Then grants tenant-wide consent, assigns the roles to the signed-in user and
--test-users, and writes the app ids to --out. Uses only the standard library
and ``az`` (Azure Cloud Shell has both). Requires an Entra role that can create
applications and grant consent (Application Administrator or Cloud Application
Administrator); without consent rights the script prints what to approve.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_SCOPES = {  # well-known delegated permission ids of Microsoft Graph
    "openid": "37f7f235-527c-4136-accd-4a02d197296e",
    "profile": "14dad69e-099b-42c9-810b-d002981feec1",
    "email": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0",
    "offline_access": "7427e0e9-2fba-42fe-b0c0-848c9e6a8182",
    "User.Read": "e1fe6dd8-ba31-4d61-89e7-88639da4683d",
}
# Stable ids for scopes and roles, so re-running never changes them.
NAMESPACE = uuid.UUID("0f6f7a4e-9b1d-4d55-8a57-3c1f2b8e6d21")
ASSURANCE_ROLES = ("low", "standard", "stepup")
CONSOLE_ROLES = ("CC.Viewer", "CC.Operator", "CC.Investigator", "CC.ChangeApprover")
AZ = shutil.which("az") or "az"


class GraphError(Exception):
    pass


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


def az(*args: str) -> str:
    result = subprocess.run([AZ, *args], capture_output=True, text=True)  # noqa: S603 - fixed az CLI arguments
    if result.returncode != 0:
        raise GraphError(result.stderr.strip() or f"az {' '.join(args[:3])} failed")
    return result.stdout


def graph(method: str, path: str, body: dict | None = None, retries: int = 6) -> dict:
    """Call Microsoft Graph through ``az rest``; retries while new objects replicate."""
    args = ["rest", "--method", method, "--url", GRAPH + path, "--headers", "Content-Type=application/json"]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(body, tmp)
        tmp.close()
        args += ["--body", "@" + tmp.name]
    try:
        for attempt in range(retries):
            try:
                out = az(*args)
                return json.loads(out) if out.strip() else {}
            except GraphError as exc:
                transient = any(s in str(exc) for s in ("does not exist", "NotFound", "Request_ResourceNotFound", "429", "503"))
                if not transient or attempt == retries - 1:
                    raise
                time.sleep(5 * (attempt + 1))
    finally:
        if tmp:
            os.unlink(tmp.name)
    return {}


def one(path: str) -> dict | None:
    values = graph("GET", path).get("value", [])
    return values[0] if values else None


# ---------------------------------------------------------------- building blocks

def ensure_app(name: str, patch: dict) -> dict:
    app = one(f"/applications?$filter=displayName eq '{name}'")
    if app is None:
        app = graph("POST", "/applications", {"displayName": name, "signInAudience": "AzureADMyOrg"})
        print(f"  created app {name} ({app['appId']})")
    graph("PATCH", f"/applications/{app['id']}", patch(app) if callable(patch) else patch)
    return graph("GET", f"/applications/{app['id']}")


def ensure_sp(app_id: str) -> dict:
    sp = one(f"/servicePrincipals?$filter=appId eq '{app_id}'")
    return sp or graph("POST", "/servicePrincipals", {"appId": app_id})


def scope(app: str, value: str, description: str) -> dict:
    return {"id": stable_id(app, "scope", value), "value": value, "type": "User", "isEnabled": True,
            "adminConsentDisplayName": description, "adminConsentDescription": description,
            "userConsentDisplayName": description, "userConsentDescription": description}


def role(app: str, value: str, description: str, member_types: list[str]) -> dict:
    return {"id": stable_id(app, "role", value), "value": value, "displayName": value, "description": description,
            "allowedMemberTypes": member_types, "isEnabled": True}


def api_patch(scopes: list[dict]) -> callable:
    def build(app: dict) -> dict:
        return {"identifierUris": [f"api://{app['appId']}"],
                "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes}}
    return build


def ensure_grant(client_sp: str, resource_sp: str, scopes: list[str]) -> None:
    """Tenant-wide consent for delegated permissions (what "Grant admin consent" does)."""
    existing = one(f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp}' and resourceId eq '{resource_sp}'"
                   " and consentType eq 'AllPrincipals'")
    wanted = " ".join(sorted(set(scopes)))
    if existing is None:
        graph("POST", "/oauth2PermissionGrants",
              {"clientId": client_sp, "consentType": "AllPrincipals", "resourceId": resource_sp, "scope": wanted})
    elif set(existing.get("scope", "").split()) != set(scopes):
        graph("PATCH", f"/oauth2PermissionGrants/{existing['id']}", {"scope": wanted})


def ensure_assignment(resource_sp: str, principal_id: str, role_id: str) -> None:
    assigned = graph("GET", f"/servicePrincipals/{resource_sp}/appRoleAssignedTo?$top=999").get("value", [])
    if any(a["principalId"] == principal_id and a["appRoleId"] == role_id for a in assigned):
        return
    graph("POST", f"/servicePrincipals/{resource_sp}/appRoleAssignedTo",
          {"principalId": principal_id, "resourceId": resource_sp, "appRoleId": role_id})


def ensure_federation(app_object_id: str, name: str, issuer: str, subject: str) -> None:
    body = {"name": name, "issuer": issuer, "subject": subject, "audiences": ["api://AzureADTokenExchange"],
            "description": "AKS workload identity (no client secret)"}
    existing = graph("GET", f"/applications/{app_object_id}/federatedIdentityCredentials").get("value", [])
    match = next((f for f in existing if f["name"] == name), None)
    if match is None:
        graph("POST", f"/applications/{app_object_id}/federatedIdentityCredentials", body)
    elif match["issuer"] != issuer or match["subject"] != subject:
        graph("PATCH", f"/applications/{app_object_id}/federatedIdentityCredentials/{match['id']}",
              {k: body[k] for k in ("issuer", "subject", "audiences")})


def read_agents(path: str) -> dict[str, list[str]]:
    """Agent name -> skills from the registry (tiny parser: the file is flat, no PyYAML needed)."""
    agents: dict[str, list[str]] = {}
    current = None
    for line in open(path, encoding="utf-8"):
        if m := re.match(r"\s*-\s*name:\s*([\w.-]+)", line):
            current = m.group(1)
            agents[current] = []
        elif (m := re.match(r"\s*skills:\s*\[(.*)\]", line)) and current:
            agents[current] = [s.strip() for s in m.group(1).split(",") if s.strip()]
    if not agents or not all(agents.values()):
        raise SystemExit(f"could not read agents and skills from {path}")
    return agents


def principal_of(user: str) -> str:
    return graph("GET", f"/users/{user}")["id"]


def signed_in_principal() -> str | None:
    account = json.loads(az("account", "show", "-o", "json"))
    if account.get("user", {}).get("type") == "user":
        return json.loads(az("ad", "signed-in-user", "show", "-o", "json"))["id"]
    return None


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--host", required=True, help="public host of the test environment")
    ap.add_argument("--oidc-issuer", required=True, help="AKS OIDC issuer URL")
    ap.add_argument("--key-vault", required=True)
    ap.add_argument("--identities", required=True, help="Bicep outputs JSON (identities: key -> clientId)")
    ap.add_argument("--agents", required=True, help="agent registry (config/agents.yaml)")
    ap.add_argument("--test-users", default="", help="comma-separated UPNs to receive all test roles")
    ap.add_argument("--rotate-console-secret", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    p = args.prefix
    tenant = json.loads(az("account", "show", "-o", "json"))["tenantId"]

    print("Agent apps")
    agents = read_agents(args.agents)
    agent_apps: dict[str, dict] = {}
    for name, skills in agents.items():
        app_name = f"{p}-{name}"
        scopes = [scope(app_name, s, f"Call {s} on behalf of the user") for s in skills]
        agent_apps[name] = ensure_app(app_name, api_patch(scopes))
        agent_apps[name]["sp"] = ensure_sp(agent_apps[name]["appId"])

    print("Orchestrator app")
    orch_name = f"{p}-orchestrator"
    orch_roles = [role(orch_name, r, f"Assurance level {r} (test environment stand-in for acr)", ["User"])
                  for r in ASSURANCE_ROLES]
    orch_roles.append(role(orch_name, "Orchestrator.Call", "Workloads allowed to call the orchestrator API", ["Application"]))

    def orch_patch(app: dict) -> dict:
        patch = api_patch([scope(orch_name, "access_as_user", "Use the assistant as the signed-in user")])(app)
        patch["appRoles"] = orch_roles
        patch["requiredResourceAccess"] = [
            {"resourceAppId": a["appId"],
             "resourceAccess": [{"id": stable_id(f"{p}-{n}", "scope", s), "type": "Scope"} for s in agents[n]]}
            for n, a in agent_apps.items()
        ]
        return patch

    orch = ensure_app(orch_name, orch_patch)
    orch_sp = ensure_sp(orch["appId"])
    ensure_federation(orch["id"], "aks-orchestrator", args.oidc_issuer, "system:serviceaccount:orchestrator:orchestrator")

    print("Console app")
    console_name = f"{p}-console"
    console = ensure_app(console_name, {
        "web": {"redirectUris": [f"https://{args.host}/oauth2/callback"],
                "implicitGrantSettings": {"enableIdTokenIssuance": False, "enableAccessTokenIssuance": False}},
        "appRoles": [role(console_name, r, f"Command center role {r}", ["User"]) for r in CONSOLE_ROLES],
        "requiredResourceAccess": [{"resourceAppId": GRAPH_APP_ID, "resourceAccess": [
            {"id": GRAPH_SCOPES[s], "type": "Scope"} for s in ("openid", "profile", "email", "offline_access", "User.Read")]}],
    })
    console_sp = ensure_sp(console["appId"])
    has_secret = subprocess.run([AZ, "keyvault", "secret", "show", "--vault-name", args.key_vault, "-n",  # noqa: S603
                                 "console-client-secret", "-o", "none"], capture_output=True).returncode == 0
    if args.rotate_console_secret or not has_secret:
        now = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 - Cloud Shell may run Python < 3.11
        end = (now + dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        secret = graph("POST", f"/applications/{console['id']}/addPassword",
                       {"passwordCredential": {"displayName": "console-proxy", "endDateTime": end}})["secretText"]
        az("keyvault", "secret", "set", "--vault-name", args.key_vault, "-n", "console-client-secret",
           "--value", secret, "-o", "none")
        print("  console client secret stored in Key Vault")

    print("Test client SPA")
    spa = ensure_app(f"{p}-test-client", {
        "spa": {"redirectUris": [f"https://{args.host}/redirect.html"]},
        "requiredResourceAccess": [
            {"resourceAppId": orch["appId"],
             "resourceAccess": [{"id": stable_id(orch_name, "scope", "access_as_user"), "type": "Scope"}]},
            {"resourceAppId": GRAPH_APP_ID, "resourceAccess": [
                {"id": GRAPH_SCOPES[s], "type": "Scope"} for s in ("openid", "profile", "offline_access")]},
        ],
    })
    spa_sp = ensure_sp(spa["appId"])

    print("Consent")
    graph_sp = one(f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'")
    try:
        for name, a in agent_apps.items():
            ensure_grant(orch_sp["id"], a["sp"]["id"], agents[name])
        ensure_grant(console_sp["id"], graph_sp["id"], ["openid", "profile", "email", "offline_access", "User.Read"])
        ensure_grant(spa_sp["id"], orch_sp["id"], ["access_as_user"])
        ensure_grant(spa_sp["id"], graph_sp["id"], ["openid", "profile", "offline_access"])
    except GraphError as exc:
        print(f"  WARNING: could not grant consent ({exc.args[0][:160]}).\n  Ask an administrator to "
              f"'Grant admin consent' on the apps {orch_name}, {console_name} and {p}-test-client.", file=sys.stderr)

    print("Role assignments")
    outputs = json.load(open(args.identities, encoding="utf-8"))
    identities = {i["key"]: i["clientId"] for i in outputs["identities"]["value"]}
    call_role = stable_id(orch_name, "role", "Orchestrator.Call")
    for key in ("tokenService", "clientBackend", "masterAgent"):
        mi_sp = one(f"/servicePrincipals?$filter=appId eq '{identities[key]}'")
        if mi_sp:
            ensure_assignment(orch_sp["id"], mi_sp["id"], call_role)
    people = [u for u in [signed_in_principal()] if u]
    people += [principal_of(u.strip()) for u in args.test_users.split(",") if u.strip()]
    for person in people:
        for r in ("standard", "stepup"):
            ensure_assignment(orch_sp["id"], person, stable_id(orch_name, "role", r))
        for r in CONSOLE_ROLES:
            ensure_assignment(console_sp["id"], person, stable_id(console_name, "role", r))
    print(f"  {len(people)} people have the test roles")

    result = {"tenantId": tenant, "apps": {
        "orchestrator": orch["appId"], "console": console["appId"], "spa": spa["appId"],
        "agents": {n: a["appId"] for n, a in agent_apps.items()}}}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
