"""Authentication of callers and end users.

Callers (master agent, token service, client backend, ops console) are
identified either by the SPIFFE ID in Envoy's ``x-forwarded-client-cert``
header (``auth.mode: mesh_xfcc``) or by a validated service JWT
(``auth.mode: jwt``). Each route group has its own allowlist.

IMPORTANT for mesh_xfcc: the sidecar must be configured to SANITIZE_SET the
XFCC header so an external client cannot inject it. See the user guide.

End-user tokens (used to bind a session and to approve transactions) are
always validated as JWTs against the IdP's keys.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jwt

from ..config import AuthConfig, JwtConfig


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Caller:
    identity: str
    kind: str  # spiffe | jwt | anonymous
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class EndUser:
    subject: str
    acr: str
    tenant: str
    expires_at: float
    claims: dict[str, Any]


def parse_xfcc(header: str | None) -> list[dict[str, str]]:
    """Parse Envoy's x-forwarded-client-cert header into a list of elements."""
    if not header:
        return []
    elements: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key, buf, in_quotes = "", "", False
    i = 0

    def flush_pair() -> None:
        nonlocal key, buf
        if key:
            current.setdefault(key.strip(), buf.strip().strip('"'))
        key, buf = "", ""

    while i < len(header):
        ch = header[i]
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "\\" and in_quotes and i + 1 < len(header):
            buf += header[i + 1]
            i += 2
            continue
        elif ch == "=" and not in_quotes and not key:
            key, buf = buf, ""
            i += 1
            continue
        elif ch == ";" and not in_quotes:
            flush_pair()
            i += 1
            continue
        elif ch == "," and not in_quotes:
            flush_pair()
            elements.append(current)
            current = {}
            i += 1
            continue
        buf += ch
        i += 1
    flush_pair()
    if current:
        elements.append(current)
    return elements


def spiffe_from_xfcc(header: str | None) -> str | None:
    """Return the SPIFFE URI of the immediate client (the last XFCC element)."""
    elements = parse_xfcc(header)
    if not elements:
        return None
    uri = elements[-1].get("URI", "")
    return uri if uri.startswith("spiffe://") else None


class JwtValidator:
    def __init__(self, config: JwtConfig, key_resolver: Any = None) -> None:
        self._config = config
        # key_resolver(token) -> key; defaults to fetching the IdP JWKS (cached by PyJWT).
        self._resolver = key_resolver or jwt.PyJWKClient(config.jwks_url, cache_keys=True, lifespan=3600).get_signing_key_from_jwt

    def validate(self, token: str) -> dict[str, Any]:
        try:
            key = self._resolver(token)
            key = getattr(key, "key", key)
            return jwt.decode(
                token,
                key=key,
                algorithms=self._config.algorithms,
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=self._config.leeway_s,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid token: {type(exc).__name__}") from exc


def resolve_acr(raw: Any, levels: list[str]) -> str:
    """Map an acr/acrs claim (string or list) to the strongest configured level present."""
    values = raw if isinstance(raw, list) else [raw]
    known = [v for v in values if isinstance(v, str) and v in levels]
    if not known:
        return ""
    return max(known, key=levels.index)


def end_user_from_claims(claims: dict[str, Any], config: AuthConfig) -> EndUser:
    subject = str(claims.get(config.user_subject_claim) or "")
    tenant = str(claims.get(config.user_tenant_claim) or "")
    acr = resolve_acr(claims.get(config.user_acr_claim), config.acr_levels)
    if not subject or not tenant or not acr:
        raise AuthError("user token lacks subject, tenant or a recognised acr", 403)
    return EndUser(subject, acr, tenant, float(claims.get("exp", time.time())), claims)


class Authenticator:
    def __init__(self, config: AuthConfig, service_jwt: JwtValidator | None = None, user_jwt: JwtValidator | None = None) -> None:
        self._config = config
        self._service_jwt = service_jwt
        self._user_jwt = user_jwt

    def caller(self, headers: dict[str, str], group: str) -> Caller:
        mode = self._config.mode
        if mode == "none":
            return Caller("anonymous", "anonymous")
        if mode == "mesh_xfcc":
            spiffe = spiffe_from_xfcc(headers.get("x-forwarded-client-cert"))
            if not spiffe:
                raise AuthError("missing client identity")
            caller = Caller(spiffe, "spiffe")
        else:
            auth = headers.get("authorization", "")
            if not auth.lower().startswith("bearer ") or self._service_jwt is None:
                raise AuthError("missing bearer token")
            claims = self._service_jwt.validate(auth[7:].strip())
            ident = str(claims.get("azp") or claims.get("appid") or claims.get("sub"))
            caller = Caller(ident, "jwt", claims)
        allowed = self._config.route_callers.get(group, [])
        if caller.identity not in allowed:
            raise AuthError(f"caller not permitted for {group}", 403)
        return caller

    def end_user(self, token: str | None, *, fallback: dict[str, Any] | None = None) -> EndUser:
        """Validate a user token. In auth mode 'none' (dev only) a fallback dict may stand in."""
        if token and self._user_jwt is not None:
            return end_user_from_claims(self._user_jwt.validate(token), self._config)
        if self._config.mode == "none" and fallback:
            return EndUser(
                str(fallback.get("subject", "dev-user")), str(fallback.get("acr", self._config.acr_levels[0])),
                str(fallback.get("tenant", "dev")), time.time() + 3600, {},
            )
        raise AuthError("user token required")
