"""Test client for the test environment: a browser app plus its "client backend".

The page (static/index.html) signs the tester in with Entra ID, starts a voice
or chat session through the token service, joins the LiveKit room and shows
the approval screen when the master agent asks for one. This backend is the
client backend of the architecture: it validates the tester's step-up token,
recomputes the action hash from the action the page displayed, and calls
``POST /v1/approvals/{id}`` with its own workload identity. It also shows the
tester their fake bank data so answers can be checked against it.

Configuration from the environment:

    TENANT_ID, SPA_CLIENT_ID, ORCHESTRATOR_APP_ID   Entra ids; empty TENANT_ID = local dev mode (no sign-in)
    ORCHESTRATOR_URL          orchestrator base URL (in-cluster)
    ORCHESTRATOR_AUTH_SCOPE   e.g. api://<orchestrator-app-id>/.default (workload identity token)
    MOCK_BACKEND_URL          mock core-banking API
    TOKEN_SERVICE_PATH        public path of the token service (same host), default /v1/voice-sessions

Run: ``uvicorn test_client.app:create_app --factory --port 8080``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from orchestrator.api.security import AuthError, JwtValidator
from orchestrator.approvals import action_hash
from orchestrator.config import JwtConfig
from orchestrator.service_auth import ServiceTokenError, WorkloadIdentityToken

STATIC = Path(__file__).parent / "static"
DEV_USER = {"subject": "local-dev-user", "acr": "stepup", "tenant": "dev"}


class ApprovalIn(BaseModel):
    approve: bool
    action: dict[str, Any]


def create_app(environ: Any = None, *, validator: JwtValidator | None = None,
               orchestrator: httpx.AsyncClient | None = None, backend: httpx.AsyncClient | None = None,
               service_token: WorkloadIdentityToken | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    tenant, spa_client, orch_app = env.get("TENANT_ID", ""), env.get("SPA_CLIENT_ID", ""), env.get("ORCHESTRATOR_APP_ID", "")
    dev_mode = not tenant
    if not dev_mode and validator is None:
        authority = env.get("AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/")
        validator = JwtValidator(JwtConfig(issuer=f"{authority}/{tenant}/v2.0", audience=orch_app,
                                           jwks_url=f"{authority}/{tenant}/discovery/v2.0/keys"))
    orch = orchestrator or httpx.AsyncClient(base_url=env.get("ORCHESTRATOR_URL", "http://localhost:8080"), timeout=5.0)
    bank = backend or httpx.AsyncClient(base_url=env.get("MOCK_BACKEND_URL", "http://localhost:8081"), timeout=3.0)
    caller = service_token or WorkloadIdentityToken(env.get("ORCHESTRATOR_AUTH_SCOPE", ""))

    app = FastAPI(title="Agent orchestrator test client", docs_url=None, redoc_url=None, openapi_url=None)

    def user(authorization: str) -> dict[str, Any]:
        """Validated claims of the tester's token (aud = orchestrator app)."""
        if dev_mode:
            return {"oid": DEV_USER["subject"], "name": "Local developer", "roles": ["stepup"]}
        if not authorization.lower().startswith("bearer ") or validator is None:
            raise HTTPException(401, "sign-in required")
        try:
            return validator.validate(authorization[7:].strip())
        except AuthError as exc:
            raise HTTPException(401, "invalid token") from exc

    @app.middleware("http")
    async def headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/redirect.html")
    async def redirect() -> FileResponse:
        return FileResponse(STATIC / "redirect.html")

    @app.get("/config.json")
    async def config() -> dict[str, Any]:
        return {
            "devMode": dev_mode, "tenantId": tenant, "clientId": spa_client,
            "authority": f"{env.get('AUTHORITY_HOST', 'https://login.microsoftonline.com').rstrip('/')}/{tenant}",
            "scopes": [f"api://{orch_app}/access_as_user"] if orch_app else [],
            "tokenServicePath": env.get("TOKEN_SERVICE_PATH", "/v1/voice-sessions"),
        }

    @app.get("/api/me")
    async def me(authorization: str = Header(default="")) -> dict[str, Any]:
        claims = user(authorization)
        subject = str(claims.get("oid") or claims.get("sub"))
        h = {"X-User-Subject": subject}
        try:
            portfolio = (await bank.get("/v1/portfolio", headers=h)).json()
            orders = (await bank.get("/v1/orders", headers=h)).json().get("orders", [])
        except httpx.HTTPError as exc:
            raise HTTPException(502, "mock backend unavailable") from exc
        roles = claims.get("roles") or []
        return {"name": claims.get("name") or claims.get("preferred_username") or subject, "subject": subject,
                "assurance": roles, "portfolio": portfolio, "orders": orders}

    @app.post("/api/approvals/{approval_id}")
    async def approve(approval_id: str, body: ApprovalIn, authorization: str = Header(default="")) -> JSONResponse:
        user(authorization)  # the orchestrator re-validates it and checks subject and step-up level
        # Hash what the tester was shown, never a hash handed to the page.
        payload: dict[str, Any] = {"approve": body.approve, "action_hash": action_hash(body.action)}
        headers: dict[str, str] = {}
        if dev_mode:
            payload["dev_user"] = DEV_USER
        else:
            headers["X-User-Token"] = authorization[7:].strip()
        try:
            headers.update(await caller.headers())
            response = await orch.post(f"/v1/approvals/{approval_id}", json=payload, headers=headers)
        except (httpx.HTTPError, ServiceTokenError) as exc:
            raise HTTPException(502, "orchestrator unavailable") from exc
        try:
            content = response.json()
        except ValueError:
            content = {"error": f"HTTP {response.status_code}"}
        return JSONResponse(content, status_code=response.status_code)

    return app
