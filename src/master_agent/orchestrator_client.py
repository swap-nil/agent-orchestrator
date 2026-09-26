"""HTTP client from the master agent to the orchestrator."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from orchestrator.service_auth import ServiceTokenError, WorkloadIdentityToken
from orchestrator.tls import client_ssl_context

from .config import OrchestratorClientConfig


class OrchestratorUnavailable(Exception):
    pass


class OrchestratorClient:
    def __init__(self, config: OrchestratorClientConfig) -> None:
        context = client_ssl_context(
            verify=config.verify_tls, ca_bundle=config.ca_bundle,
            cert_file=config.client_cert_file, key_file=config.client_key_file,
        )
        self._client = httpx.AsyncClient(
            base_url=config.url.rstrip("/"),
            http2=True,
            verify=context,
            timeout=config.timeout_ms / 1000,
        )
        self._token = WorkloadIdentityToken(config.auth_scope)

    async def turn(self, session_id: str, text: str, traceparent: str, channel: str = "voice",
                   turn_id: str | None = None) -> dict[str, Any]:
        body = {"session_id": session_id, "turn_id": turn_id or uuid.uuid4().hex, "text": text, "channel": channel}
        # One retry on connection failure only, with the SAME turn_id: the orchestrator
        # replays the stored response instead of executing the turn twice.
        try:
            headers = {"traceparent": traceparent, **await self._token.headers()}
        except ServiceTokenError as exc:
            raise OrchestratorUnavailable(str(exc)) from exc
        for attempt in (1, 2):
            try:
                response = await self._client.post("/v1/turns", json=body, headers=headers)
            except httpx.ConnectError as exc:
                if attempt == 2:
                    raise OrchestratorUnavailable(str(exc)) from exc
                continue
            except httpx.HTTPError as exc:
                raise OrchestratorUnavailable(str(exc)) from exc
            if response.status_code != 200:
                raise OrchestratorUnavailable(f"HTTP {response.status_code}")
            return response.json()
        raise OrchestratorUnavailable("unreachable")

    async def workflow_status(self, workflow_id: str, session_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"/v1/workflows/{workflow_id}", params={"session_id": session_id}, headers=await self._token.headers()
        )
        response.raise_for_status()
        return response.json()

    async def close_session(self, session_id: str) -> None:
        try:
            await self._client.delete(f"/v1/sessions/{session_id}", headers=await self._token.headers())
        except (httpx.HTTPError, ServiceTokenError):
            pass  # the session TTL reclaims it

    async def aclose(self) -> None:
        await self._client.aclose()
