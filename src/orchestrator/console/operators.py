"""Operator authentication and roles for the command center.

Roles (a person can hold several):

* viewer: dashboards, live decisions, turn traces (utterances hidden), evals, audit.
* operator: viewer + kill switches, session termination, breaker reset, alert
  acknowledgement, eval runs, proposing runtime changes, shadowing, rollback.
* investigator: viewer + sees PII-redacted utterances (when
  ``command_center.show_utterances`` is on). Every such view is audited.
* approver: viewer + approves or rejects runtime changes (never their own).

In production operators sign in with Entra ID; app roles in the token's
``roles`` claim map to console roles through ``command_center.role_members``.
In development (``operator_auth: none``) the ``X-Operator`` header names the
operator and ``X-Operator-Roles`` may narrow the roles, so four-eyes flows can
be rehearsed locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..api.security import AuthError, JwtValidator
from ..config import CommandCenterConfig

ROLES = ("viewer", "operator", "investigator", "approver")


@dataclass
class Operator:
    id: str
    name: str
    roles: set[str] = field(default_factory=set)

    def has(self, role: str) -> bool:
        return role in self.roles or (role == "viewer" and bool(self.roles))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "roles": sorted(self.roles)}


class OperatorAuth:
    def __init__(self, config: CommandCenterConfig, validator: JwtValidator | None = None) -> None:
        self._cfg = config
        self._validator = validator
        if config.operator_auth == "jwt" and validator is None:
            self._validator = JwtValidator(config.operator_jwt)

    def resolve(self, headers: dict[str, str]) -> Operator:
        headers = {k.lower(): v for k, v in headers.items()}
        if self._cfg.operator_auth == "none":
            name = (headers.get("x-operator") or "dev-operator").strip()[:64] or "dev-operator"
            requested = {r.strip() for r in (headers.get("x-operator-roles") or "").split(",") if r.strip()}
            roles = (requested & set(ROLES)) if requested else set(ROLES)
            return Operator(name, name, roles)
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or self._validator is None:
            raise AuthError("operator sign-in required")
        claims = self._validator.validate(auth[7:].strip())
        granted = claims.get(self._cfg.operator_roles_claim) or []
        if isinstance(granted, str):
            granted = [granted]
        roles = {role for role, members in self._cfg.role_members.items() if role in ROLES and set(members) & set(granted)}
        if not roles:
            raise AuthError("no command center role assigned", 403)
        ident = str(claims.get("oid") or claims.get("sub"))
        return Operator(ident, str(claims.get("name") or claims.get("preferred_username") or ident), roles)
