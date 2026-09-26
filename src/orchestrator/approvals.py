"""Human approval for transactions (R3).

The approval is deliberately NOT routed through the master agent or any model.
The client app sends the decision to its own backend with a step-up
authenticated user token; the backend calls ``POST /v1/approvals/{id}``. The
orchestrator then checks:

* the ticket exists, has not expired and has not been used (one-time use);
* the approving user is the session's user;
* the user's authentication level meets ``auth.approval_min_acr``;
* the action hash the client displayed equals the hash of the prepared action.

On success it issues an approval token bound to the action hash, signed with Ed25519. The
workflow passes it to the write step, and the tool gateway re-verifies it with
the public key, so a changed amount or beneficiary is rejected downstream as
well, and no downstream service can forge an approval.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .audit import canonical_json
from .models import ApprovalTicket
from .planner import acr_rank
from .state import SessionStore


def action_hash(action: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(action).encode("utf-8")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _message(approval_id: str, a_hash: str, expires_at: int) -> bytes:
    return f"v2|{approval_id}|{a_hash}|{expires_at}".encode()


class ApprovalSigner:
    """Signs approval tokens with Ed25519.

    Only the orchestrator holds the private key. The tool gateway and domain
    agents verify with the public key (``python -m orchestrator.cli
    approval-public-key``), so no downstream service can mint approvals.
    """

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("approval signing key must be at least 32 bytes")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(secret).digest())
        self.public_key = self._private.public_key()

    def issue(self, approval_id: str, a_hash: str, expires_at: int) -> str:
        sig = self._private.sign(_message(approval_id, a_hash, expires_at))
        return f"v2.{approval_id}.{expires_at}.{_b64(sig)}"

    def public_key_pem(self) -> str:
        from cryptography.hazmat.primitives import serialization

        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()


def verify_approval_token(public_key: Any, token: str, a_hash: str, now: float | None = None) -> bool:
    """Verify an approval token against the action hash. ``public_key`` is an Ed25519 key or PEM text."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    if isinstance(public_key, (str, bytes)):
        public_key = serialization.load_pem_public_key(public_key.encode() if isinstance(public_key, str) else public_key)
    try:
        version, approval_id, expires_raw, sig = token.split(".")
        expires_at = int(expires_raw)
        signature = _unb64(sig)
    except ValueError:
        return False
    if version != "v2" or expires_at < (now if now is not None else time.time()):
        return False
    try:
        public_key.verify(signature, _message(approval_id, a_hash, expires_at))
    except InvalidSignature:
        return False
    return True


class WorkflowGateway(Protocol):
    async def start_transaction(self, workflow_id: str, payload: dict[str, Any]) -> None: ...
    async def signal_approval(self, workflow_id: str, decision: dict[str, Any]) -> None: ...
    async def status(self, workflow_id: str) -> dict[str, Any]: ...


@dataclass
class ApprovalOutcome:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    token: str | None = None
    ticket: ApprovalTicket | None = None
    declined: bool = False


class ApprovalService:
    def __init__(
        self,
        store: SessionStore,
        signer: ApprovalSigner,
        acr_levels: list[str],
        min_acr: str,
        timeout_s: int,
        clock: Any = time.time,
    ) -> None:
        self._store = store
        self._signer = signer
        self._levels = acr_levels
        self._min_acr = min_acr
        self._timeout = timeout_s
        self._clock = clock

    async def create(self, session_id: str, workflow_id: str, action: dict[str, Any], summary: str) -> ApprovalTicket:
        ticket = ApprovalTicket(
            approval_id=secrets.token_urlsafe(18),
            session_id=session_id,
            workflow_id=workflow_id,
            action_hash=action_hash(action),
            summary=summary,
            expires_at=self._clock() + self._timeout,
            required_acr=self._min_acr,
        )
        await self._store.put_approval(ticket)
        return ticket

    async def decide(
        self,
        approval_id: str,
        *,
        approve: bool,
        subject: str,
        acr: str,
        presented_action_hash: str,
    ) -> ApprovalOutcome:
        ticket = await self._store.get_approval(approval_id)
        if ticket is None:
            return ApprovalOutcome(False, ["unknown approval"])
        reasons: list[str] = []
        if ticket.used:
            reasons.append("approval already used")
        if ticket.expires_at < self._clock():
            reasons.append("approval expired")
        session = await self._store.get(ticket.session_id)
        if session is None or session.subject != subject:
            reasons.append("approver is not the session user")
        if approve and acr_rank(acr, self._levels) < acr_rank(ticket.required_acr, self._levels):
            reasons.append("step-up authentication required")
        if not hmac.compare_digest(presented_action_hash, ticket.action_hash):
            reasons.append("displayed action does not match prepared action")
        if reasons:
            return ApprovalOutcome(False, reasons, ticket=ticket)
        if not await self._store.consume_approval(approval_id):
            return ApprovalOutcome(False, ["approval already used"], ticket=ticket)
        if not approve:
            return ApprovalOutcome(False, ["declined by user"], ticket=ticket, declined=True)
        token = self._signer.issue(approval_id, ticket.action_hash, int(ticket.expires_at))
        return ApprovalOutcome(True, [], token, ticket)
