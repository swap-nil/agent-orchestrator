"""HTTP API for the orchestrator.

Routes and who may call them (``auth.route_callers``):

    POST   /v1/sessions               sessions   token service binds a user to a session
    DELETE /v1/sessions/{id}          sessions
    POST   /v1/turns                  turns      master agent, one call per user turn
    POST   /v1/approvals/{id}         approvals  client backend, with the user's step-up token
    GET    /v1/workflows/{id}         workflows  status of a transaction
    PUT    /admin/kill-switch         admin
    GET    /admin/audit/{chain}/verify admin
    GET    /healthz, /readyz          unauthenticated probes

Run: ``uvicorn orchestrator.api.app:create_app --factory --host 0.0.0.0 --port 8080``
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..audit import verify_chain
from ..bootstrap import build_service
from ..config import OrchestratorConfig, load_config
from ..models import TurnRequest, UserContext
from ..service import OrchestratorService
from .security import AuthError, Authenticator, JwtValidator

log = logging.getLogger("orchestrator.api")

_ID = r"^[A-Za-z0-9._:-]{1,128}$"


class SessionIn(BaseModel):
    session_id: str = Field(pattern=_ID)
    user_token: str | None = Field(default=None, max_length=16_000)
    channel: str = Field(default="voice", pattern=r"^(voice|chat)$")
    locale: str = Field(default="en-CH", max_length=16)
    dev_user: dict[str, Any] | None = None  # honoured only when auth.mode == none


class TurnIn(BaseModel):
    session_id: str = Field(pattern=_ID)
    turn_id: str = Field(pattern=_ID)
    text: str = Field(min_length=1, max_length=8000)
    channel: str = Field(default="voice", pattern=r"^(voice|chat)$")


class ApprovalIn(BaseModel):
    approve: bool
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dev_user: dict[str, Any] | None = None


class KillSwitchIn(BaseModel):
    disabled_agents: list[str] = []
    disabled_intents: list[str] = []
    disabled_risk_classes: list[str] = []


def _headers(request: Request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


def create_app(config: OrchestratorConfig | None = None, service: OrchestratorService | None = None) -> FastAPI:
    config = config or load_config()
    logging.basicConfig(level=config.service.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if config.telemetry.enabled:
        from ..adapters.otel import configure_telemetry

        configure_telemetry(config)
    service = service or build_service(config)
    service_jwt = JwtValidator(config.auth.jwt) if config.auth.mode == "jwt" else None
    user_jwt = JwtValidator(config.auth.user_jwt) if config.auth.user_jwt.jwks_url else None
    auth = Authenticator(config.auth, service_jwt, user_jwt)

    app = FastAPI(title="Agent Orchestrator", version="1.1.0", docs_url=None if config.profile == "prod" else "/docs",
                  redoc_url=None, openapi_url=None if config.profile == "prod" else "/openapi.json")
    console = None
    if config.command_center.enabled:
        from ..console.api import build_console

        console = build_console(service)

    @app.middleware("http")
    async def limits_and_errors(request: Request, call_next):  # type: ignore[no-untyped-def]
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.server.request_body_limit_bytes:
            return JSONResponse({"error": "request too large"}, status_code=413)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            return JSONResponse({"error": "internal error"}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/console"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            response.headers["X-Frame-Options"] = "DENY"
        log.info("%s %s %s %.1fms", request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
        return response

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    def require(request: Request, group: str) -> None:
        auth.caller(_headers(request), group)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ok = await service.ready()
        return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503)

    @app.post("/v1/sessions", status_code=201)
    async def open_session(body: SessionIn, request: Request, traceparent: str | None = Header(default=None)) -> dict[str, str]:
        require(request, "sessions")
        user = auth.end_user(body.user_token, fallback=body.dev_user)
        try:
            await service.open_session(
                session_id=body.session_id,
                user=UserContext(subject=user.subject, acr=user.acr, tenant=user.tenant, channel=body.channel, locale=body.locale),
                subject_token=body.user_token or "",
                token_expires_at=user.expires_at,
                traceparent=traceparent,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"session_id": body.session_id}

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, request: Request) -> None:
        # The token service or the master agent (on room end) may close a session.
        try:
            require(request, "sessions")
        except AuthError:
            require(request, "turns")
        await service.close_session(session_id)

    @app.post("/v1/turns")
    async def turns(body: TurnIn, request: Request, traceparent: str | None = Header(default=None)) -> dict[str, Any]:
        require(request, "turns")
        response = await service.handle_turn(TurnRequest(body.session_id, body.turn_id, body.text, body.channel, traceparent))
        return response.to_dict()

    @app.post("/v1/approvals/{approval_id}")
    async def approvals(
        approval_id: str, body: ApprovalIn, request: Request,
        x_user_token: str | None = Header(default=None), traceparent: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require(request, "approvals")
        user = auth.end_user(x_user_token, fallback=body.dev_user)
        result = await service.decide_approval(
            approval_id, approve=body.approve, subject=user.subject, acr=user.acr,
            presented_action_hash=body.action_hash, traceparent=traceparent,
            user_token=x_user_token, token_expires_at=user.expires_at,
        )
        if not result["accepted"] and not result.get("declined"):
            return JSONResponse({"accepted": False, "reasons": result["reasons"]}, status_code=409)  # type: ignore[return-value]
        return result

    @app.get("/v1/workflows/{workflow_id}")
    async def workflow(workflow_id: str, session_id: str, request: Request) -> dict[str, Any]:
        require(request, "workflows")
        return await service.workflow_status(workflow_id, session_id)

    @app.put("/admin/kill-switch")
    async def kill_switch(body: KillSwitchIn, request: Request) -> dict[str, Any]:
        caller = auth.caller(_headers(request), "admin")
        await service.set_runtime_flags(body.model_dump(), actor=caller.identity)
        current = await service.kill_switch()
        return {"disabled_agents": current.disabled_agents, "disabled_intents": current.disabled_intents,
                "disabled_risk_classes": current.disabled_risk_classes}

    @app.get("/admin/audit/{chain_id}/verify")
    async def verify(chain_id: str, request: Request) -> dict[str, Any]:
        require(request, "admin")
        chain = await service.c.audit.chain(chain_id)
        ok, bad = verify_chain(chain)
        return {"chain_id": chain_id, "records": len(chain), "valid": ok, "first_invalid_index": bad}

    if console is not None:
        import asyncio

        from ..console.api import console_page, heartbeat_merge

        console_html = console_page()

        @app.on_event("startup")
        async def _alert_loop() -> None:
            async def loop() -> None:
                while True:
                    await asyncio.sleep(5)
                    try:
                        await console.tick()
                    except Exception:  # noqa: BLE001
                        log.exception("alert evaluation failed")

            app.state.alert_task = asyncio.create_task(loop())

        @app.get("/console", response_class=HTMLResponse)
        async def console_page() -> HTMLResponse:
            return HTMLResponse(console_html)

        @app.get("/admin/cc/stream")
        async def console_stream(request: Request) -> StreamingResponse:
            try:
                _, gen = await console.stream(dict(request.query_params), _headers(request))
            except AuthError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)  # type: ignore[return-value]
            return StreamingResponse(heartbeat_merge(gen), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

        @app.api_route("/admin/cc/{path:path}", methods=["GET", "POST", "PUT"])
        async def console_api(path: str, request: Request) -> JSONResponse:
            body = None
            if request.method in ("POST", "PUT"):
                raw = await request.body()
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        return JSONResponse({"error": "invalid JSON"}, status_code=400)
            status, payload = await console.dispatch(request.method, "/" + path, dict(request.query_params), body, _headers(request))
            return JSONResponse(payload, status_code=status)

    return app
