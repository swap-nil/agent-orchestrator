"""Delegated identity for agent calls.

For every delegation the orchestrator exchanges the user's session token for a
short-lived token whose audience is the target agent and whose scope is the
skill being called. The user stays the subject; the orchestrator is recorded
as the actor. Tokens are never broadened and are cached only until shortly
before expiry.

Two exchange styles are supported:

* ``rfc8693``: OAuth 2.0 Token Exchange (RFC 8693), for IdPs that support it.
* ``entra_obo``: the Microsoft Entra ID on-behalf-of flow (jwt-bearer grant).

Sender constraint (mTLS-bound or DPoP) is applied by the transport and the IdP
configuration; see the user guide.
"""

from __future__ import annotations

import os
import time
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
        clock: Any = time.time, environ: Any = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._secret = client_secret
        self._clock = clock
        self._environ = os.environ if environ is None else environ
        self._cache: dict[tuple[str, str, str], DelegatedToken] = {}

    def _client_credential(self) -> dict[str, str]:
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
        form = build_exchange_form(self._config, self._client_credential(), subject_token, audience, scope)
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
