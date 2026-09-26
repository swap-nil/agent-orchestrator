"""Token service: the only public entry point for starting a voice session.

    POST /v1/voice-sessions   (Authorization: Bearer <user access token>)

1. Validate the user's access token (issuer, audience, expiry, signature).
2. Cell admission: refuse new sessions when the cell is full.
3. Create an opaque session id, a room and a root trace.
4. Bind the user to the session in the orchestrator (the user token goes
   there and nowhere else).
5. Mint a short-lived LiveKit token for that one room, with explicit dispatch
   of the master agent carrying signed metadata.

Run: ``uvicorn token_service.app:create_app --factory --port 8090``
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from livekit import api
from pydantic import BaseModel, Field

from orchestrator.api.security import AuthError, JwtValidator, resolve_acr
from orchestrator.service_auth import ServiceTokenError, WorkloadIdentityToken
from orchestrator.tls import client_ssl_context

from .config import TokenServiceConfig, load_token_service_config
from .logic import participant_grants, plan_session

log = logging.getLogger("token_service")


class VoiceSessionIn(BaseModel):
    channel: str = Field(default="voice", pattern=r"^(voice|chat)$")
    locale: str = Field(default="en-CH", max_length=16)


class Admission:
    def __init__(self, config: TokenServiceConfig) -> None:
        self._cfg = config.admission
        self._key = f"ts:{config.cell_id}:sessions"
        self._redis = None
        if self._cfg.enabled:
            import redis.asyncio as redis

            self._redis = redis.from_url(os.environ[self._cfg.redis_url_env], decode_responses=True)

    async def try_admit(self, session_id: str) -> bool:
        if self._redis is None:
            return True
        # Sorted set of active sessions scored by expiry; stale entries are trimmed first.
        import time

        now = time.time()
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(self._key, 0, now)
        pipe.zcard(self._key)
        _, active = await pipe.execute()
        if active >= self._cfg.max_sessions_per_cell:
            return False
        await self._redis.zadd(self._key, {session_id: now + self._cfg.session_ttl_s})
        return True

    async def release(self, session_id: str) -> None:
        if self._redis is not None:
            await self._redis.zrem(self._key, session_id)


def create_app(config: TokenServiceConfig | None = None) -> FastAPI:
    config = config or load_token_service_config()
    validator = JwtValidator(config.user_jwt) if config.user_jwt.jwks_url else None
    dispatch_key = os.environ[config.dispatch_key_env].encode()
    lk_key = os.environ[config.livekit.api_key_env]
    lk_secret = os.environ[config.livekit.api_secret_env]
    admission = Admission(config)
    context = client_ssl_context(
        ca_bundle=config.orchestrator_ca_bundle, cert_file=config.client_cert_file, key_file=config.client_key_file,
    )
    orch = httpx.AsyncClient(base_url=config.orchestrator_url, verify=context, timeout=3.0)
    service_token = WorkloadIdentityToken(config.orchestrator_auth_scope)
    app = FastAPI(title="Voice token service", docs_url=None if config.profile == "prod" else "/docs", redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/voice-sessions", status_code=201)
    async def create_session(body: VoiceSessionIn, authorization: str = Header(default="")) -> dict[str, Any]:
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if validator is not None:
            if not token:
                raise HTTPException(401, "missing bearer token")
            try:
                claims = validator.validate(token)
            except AuthError as exc:
                raise HTTPException(401, "invalid token") from exc
            if not resolve_acr(claims.get(config.acr_claim), config.acr_levels) and config.profile == "prod":
                raise HTTPException(403, "insufficient authentication")
        elif config.profile == "prod":
            raise HTTPException(500, "user token validation not configured")
        if body.channel not in config.allowed_channels:
            raise HTTPException(400, "channel not allowed")

        plan = plan_session(room_prefix=config.livekit.room_prefix, dispatch_key=dispatch_key, channel=body.channel)
        if not await admission.try_admit(plan.session_id):
            raise HTTPException(503, "voice service is at capacity, please try again shortly")

        payload: dict[str, Any] = {"session_id": plan.session_id, "user_token": token or None,
                                   "channel": body.channel, "locale": body.locale}
        if validator is None:
            payload["dev_user"] = {"subject": "dev-user", "acr": "standard", "tenant": "dev"}
        try:
            headers = {"traceparent": plan.traceparent, **await service_token.headers()}
            response = await orch.post("/v1/sessions", json=payload, headers=headers)
        except (httpx.HTTPError, ServiceTokenError) as exc:
            await admission.release(plan.session_id)
            raise HTTPException(502, "could not start session") from exc
        if response.status_code != 201:
            await admission.release(plan.session_id)
            log.error("orchestrator refused session: %s", response.status_code)
            raise HTTPException(502, "could not start session")

        grants = participant_grants(plan.room)
        lk_token = (
            api.AccessToken(lk_key, lk_secret)
            .with_identity(plan.participant_identity)
            .with_ttl(timedelta(seconds=config.livekit.token_ttl_s))
            .with_grants(api.VideoGrants(**grants))
            .with_room_config(api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=config.livekit.agent_name, metadata=plan.dispatch_metadata)]
            ))
            .to_jwt()
        )
        return {"session_id": plan.session_id, "room": plan.room, "url": config.livekit.url, "token": lk_token,
                "traceparent": plan.traceparent, "expires_in": config.livekit.token_ttl_s}

    return app
