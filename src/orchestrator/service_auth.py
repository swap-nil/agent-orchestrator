"""Service tokens for callers of the orchestrator (``auth.mode: jwt``).

The token service, the master agent and the client backend authenticate to
the orchestrator with an Entra ID app-only token obtained through AKS Workload
Identity: the projected service-account token is the client assertion of a
client-credentials grant, so no secret exists anywhere. The orchestrator
checks the token's ``azp`` against ``auth.route_callers``.

The workload identity webhook injects ``AZURE_CLIENT_ID``, ``AZURE_TENANT_ID``,
``AZURE_AUTHORITY_HOST`` and ``AZURE_FEDERATED_TOKEN_FILE``. With an empty
scope the source is disabled and adds no header (mesh or local development).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Mapping

from .identity import CLIENT_ASSERTION_TYPE

# (url, form) -> (status, json body)
TokenPost = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]]


class ServiceTokenError(Exception):
    pass


async def _httpx_post(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(url, data=form)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


class WorkloadIdentityToken:
    def __init__(
        self,
        scope: str,
        *,
        environ: Mapping[str, str] | None = None,
        post: TokenPost | None = None,
        clock: Callable[[], float] = time.time,
        refresh_skew_s: int = 120,
    ) -> None:
        self._scope = scope.strip()
        self._environ = os.environ if environ is None else environ
        self._post = post or _httpx_post
        self._clock = clock
        self._skew = refresh_skew_s
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._scope)

    def _form(self) -> tuple[str, dict[str, str]]:
        env = self._environ
        client_id, tenant = env.get("AZURE_CLIENT_ID", ""), env.get("AZURE_TENANT_ID", "")
        path = env.get("AZURE_FEDERATED_TOKEN_FILE", "")
        if not (client_id and tenant and path):
            raise ServiceTokenError(
                "workload identity is not configured (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE)"
            )
        try:
            with open(path, encoding="utf-8") as handle:  # re-read each time: the projected token rotates
                assertion = handle.read().strip()
        except OSError as exc:
            raise ServiceTokenError("workload identity token unreadable") from exc
        authority = env.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/").rstrip("/")
        url = f"{authority}/{tenant}/oauth2/v2.0/token"
        return url, {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "scope": self._scope,
            "client_assertion_type": CLIENT_ASSERTION_TYPE,
            "client_assertion": assertion,
        }

    async def token(self) -> str:
        if not self.enabled:
            return ""
        async with self._lock:
            if self._token and self._expires_at - self._skew > self._clock():
                return self._token
            url, form = self._form()
            try:
                status, body = await self._post(url, form)
            except Exception as exc:  # noqa: BLE001 - network errors of any client library
                raise ServiceTokenError(f"token endpoint unreachable: {type(exc).__name__}") from exc
            if status != 200 or "access_token" not in body:
                raise ServiceTokenError(f"token request refused: {body.get('error', f'HTTP {status}')}")
            self._token = str(body["access_token"])
            self._expires_at = self._clock() + int(body.get("expires_in", 300))
            return self._token

    async def headers(self) -> dict[str, str]:
        """Authorization header for one call, or nothing when disabled."""
        token = await self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}
