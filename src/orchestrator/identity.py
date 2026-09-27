"""Delegated identity for agent calls.

For every delegation the orchestrator exchanges the user's session token for a
short-lived token whose audience is the target agent and whose scope is the
skill being called. The user stays the subject; the orchestrator is recorded
as the actor. Tokens are never broadened and are cached only until shortly
before expiry.

Two exchange styles are supported:

* ``rfc8693``: OAuth 2.0 Token Exchange (RFC 8693), for IdPs that support it.
* ``entra_obo``: the Microsoft Entra ID on-behalf-of flow (jwt-bearer grant).

The orchestrator proves its own identity to the IdP without a secret, with a
federated client assertion: the projected service-account token of AKS
Workload Identity (``client_auth: workload_identity``) or, on an Azure VM, a
managed identity token from the instance metadata service
(``client_auth: managed_identity``; the app registration trusts that managed
identity as a federated credential).

Sender constraint (mTLS-bound or DPoP) is applied by the transport and the IdP
configuration; see the user guide.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import IdentityConfig
from .transport import HttpTransport, TransportError


class TokenExchangeError(Exception):
    pass


@dataclass
class DelegatedToken:
    access_token: str
    expires_at: float
    token_type: str = "Bearer"


CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# Audience of a managed identity token used as a federated client assertion.
FEDERATED_ASSERTION_RESOURCE = "api://AzureADTokenExchange"
IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"  # noqa: S105 - a URL, not a secret

# (url, query parameters) -> (status, json body); the request carries "Metadata: true".
ImdsGet = Callable[[str, dict[str, str]], Awaitable[tuple[int, dict[str, Any]]]]


class ManagedIdentityError(Exception):
    pass


async def _httpx_imds_get(url: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
    import httpx

    # trust_env=False: metadata requests must never go through an HTTP proxy.
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        response = await client.get(url, params=params, headers={"Metadata": "true"})
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def resource_of(scope: str) -> str:
    """IMDS takes a v1 resource: ``api://<app>/.default`` -> ``api://<app>``."""
    scope = scope.strip()
    return scope[: -len("/.default")] if scope.endswith("/.default") else scope


class ManagedIdentityCredential:
    """Tokens of an Azure managed identity from the instance metadata service (IMDS).

    On an Azure VM there is no projected service-account token; IMDS issues
    tokens for the identities attached to the VM. ``client_id`` selects one of
    several user-assigned identities. Tokens are cached per resource until
    shortly before expiry.
    """

    def __init__(
        self, client_id: str, *, get: ImdsGet | None = None, clock: Callable[[], float] = time.time,
        refresh_skew_s: int = 300,
    ) -> None:
        self._client_id = client_id
        self._get = get or _httpx_imds_get
        self._clock = clock
        self._skew = refresh_skew_s
        self._cache: dict[str, tuple[str, float]] = {}

    async def token(self, resource: str) -> tuple[str, float]:
        """(access token, expiry as epoch seconds) for ``resource``."""
        cached = self._cache.get(resource)
        if cached and cached[1] - self._skew > self._clock():
            return cached
        params = {"api-version": "2018-02-01", "resource": resource}
        if self._client_id:
            params["client_id"] = self._client_id
        try:
            status, body = await self._get(IMDS_TOKEN_URL, params)
        except Exception as exc:  # noqa: BLE001 - network errors of any client library
            raise ManagedIdentityError(f"instance metadata service unreachable: {type(exc).__name__}") from exc
        if status != 200 or "access_token" not in body:
            detail = body.get("error_description") or body.get("error") or f"HTTP {status}"
            raise ManagedIdentityError(f"managed identity token refused: {str(detail)[:200]}")
        token = (str(body["access_token"]), self._clock() + int(body.get("expires_in", 300)))
        self._cache[resource] = token
        return token


def build_exchange_form(
    config: IdentityConfig, client_credential: dict[str, str], subject_token: str, audience: str, scope: str
) -> dict[str, str]:
    if config.mode == "rfc8693":
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
            "scope": scope,
            "client_id": config.client_id,
        }
    elif config.mode == "entra_obo":
        # Entra expects scope as "<resource>/.default" or "<resource>/<scope>".
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": subject_token,
            "requested_token_use": "on_behalf_of",
            "scope": f"{audience.rstrip('/')}/{scope}",
            "client_id": config.client_id,
        }
    else:
        raise TokenExchangeError(f"unsupported identity mode {config.mode!r}")
    form.update(client_credential)
    return form


class TokenExchanger:
    def __init__(
        self, config: IdentityConfig, transport: HttpTransport, client_secret: str = "",
        clock: Any = time.time, environ: Any = None, imds_get: ImdsGet | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._secret = client_secret
        self._clock = clock
        self._environ = os.environ if environ is None else environ
        self._cache: dict[tuple[str, str, str], DelegatedToken] = {}
        self._imds_get = imds_get
        self._managed_identity: ManagedIdentityCredential | None = None

    async def _client_credential(self) -> dict[str, str]:
        if self._config.client_auth == "managed_identity":
            if self._managed_identity is None:
                client_id = self._config.managed_identity_client_id or self._environ.get("AZURE_CLIENT_ID", "")
                if not client_id:
                    raise TokenExchangeError("managed identity client id not configured")
                self._managed_identity = ManagedIdentityCredential(client_id, get=self._imds_get, clock=self._clock)
            try:
                assertion, _ = await self._managed_identity.token(FEDERATED_ASSERTION_RESOURCE)
            except ManagedIdentityError as exc:
                raise TokenExchangeError(str(exc)) from exc
            return {"client_assertion_type": CLIENT_ASSERTION_TYPE, "client_assertion": assertion}
        return self._static_credential()

    def _static_credential(self) -> dict[str, str]:
        if self._config.client_auth == "secret":
            return {"client_secret": self._secret} if self._secret else {}
        path = self._config.federated_token_file or self._environ.get("AZURE_FEDERATED_TOKEN_FILE", "")
        if not path:
            raise TokenExchangeError("workload identity token file not configured")
        try:
            with open(path, encoding="utf-8") as handle:  # re-read each time: the projected token rotates
                assertion = handle.read().strip()
        except OSError as exc:
            raise TokenExchangeError("workload identity token unreadable") from exc
        return {"client_assertion_type": CLIENT_ASSERTION_TYPE, "client_assertion": assertion}

    @property
    def enabled(self) -> bool:
        return self._config.mode != "disabled"

    async def token_for(self, session_id: str, subject_token: str, audience: str, scope: str) -> DelegatedToken | None:
        if not self.enabled:
            return None
        if not subject_token:
            raise TokenExchangeError("no user token bound to the session")
        key = (session_id, audience, scope)
        cached = self._cache.get(key)
        now = self._clock()
        if cached and cached.expires_at - self._config.refresh_skew_s > now:
            return cached
        form = build_exchange_form(self._config, await self._client_credential(), subject_token, audience, scope)
        try:
            response = await self._transport.post(
                self._config.token_endpoint, form=form, timeout_s=self._config.timeout_ms / 1000
            )
        except (TransportError, TimeoutError) as exc:
            raise TokenExchangeError(f"token endpoint unreachable: {type(exc).__name__}") from exc
        body = response.body if isinstance(response.body, dict) else {}
        if response.status != 200 or "access_token" not in body:
            error = body.get("error", f"HTTP {response.status}")
            raise TokenExchangeError(f"token exchange refused: {error}")
        expires_in = int(body.get("expires_in", 300))
        token = DelegatedToken(str(body["access_token"]), now + expires_in, str(body.get("token_type", "Bearer")))
        self._cache[key] = token
        return token

    def forget_session(self, session_id: str) -> None:
        for key in [k for k in self._cache if k[0] == session_id]:
            del self._cache[key]
