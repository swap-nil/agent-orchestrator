"""Session state and approval tickets.

Session state is externalised so any orchestrator replica in the cell can
serve any turn. The user's access token is bound to the session by the token
service, encrypted at rest, and never leaves the backend: the master agent
only ever sees the opaque session id.

Turns for one session are serialised with a lock, so budgets, clarification
counts and approvals cannot race.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Protocol

from .models import ApprovalTicket


class StateError(Exception):
    pass


class TokenCipher:
    """Fernet (AES-128-CBC + HMAC-SHA256) encryption for tokens at rest."""

    def __init__(self, key: str | None) -> None:
        self._fernet = None
        if key:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, token: str) -> str:
        if not self._fernet:
            return "plain:" + token
        return "fernet:" + self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, stored: str) -> str:
        if stored.startswith("plain:"):
            if self._fernet:
                raise StateError("refusing plaintext token while encryption is enabled")
            return stored[len("plain:"):]
        if stored.startswith("fernet:"):
            if not self._fernet:
                raise StateError("token is encrypted but no key is configured")
            return self._fernet.decrypt(stored[len("fernet:"):].encode()).decode()
        raise StateError("unrecognised token encoding")


@dataclass
class SessionState:
    session_id: str
    subject: str
    tenant: str
    acr: str
    channel: str = "voice"
    locale: str = "en-CH"
    entitlements: list[str] = field(default_factory=list)
    token_blob: str = ""
    token_expires_at: float = 0.0
    clarification_rounds: int = 0
    # An open question the next turn may answer: {"kind": "slot", "intent", "slots", "asking"}
    # or {"kind": "choice", "candidates", "text"}. Cleared when answered or when the user moves on.
    pending: dict[str, Any] = field(default_factory=dict)
    last_clarified_text: str = ""
    cost_used: int = 0
    turns: int = 0
    pending_workflows: list[str] = field(default_factory=list)
    # turn_id -> response, so a retried turn is replayed, never executed twice.
    recent_turns: dict[str, dict[str, Any]] = field(default_factory=dict)
    # workflow_id -> encrypted step-up token presented with the approval.
    approval_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        return cls(**data)


class SessionStore(Protocol):
    async def get(self, session_id: str) -> SessionState | None: ...
    async def put(self, state: SessionState) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    def lock(self, session_id: str) -> contextlib.AbstractAsyncContextManager[None]: ...
    async def put_approval(self, ticket: ApprovalTicket) -> None: ...
    async def get_approval(self, approval_id: str) -> ApprovalTicket | None: ...
    async def consume_approval(self, approval_id: str) -> bool: ...
    async def get_flags(self) -> dict[str, list[str]]: ...
    async def put_flags(self, flags: dict[str, list[str]]) -> None: ...
    async def get_control(self, key: str) -> Any: ...
    async def put_control(self, key: str, value: Any) -> None: ...
    async def ping(self) -> bool: ...


class InMemorySessionStore:
    """Single-process store for development and tests."""

    def __init__(self, ttl_s: int = 3600, clock: Any = time.time) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._sessions: dict[str, tuple[float, dict[str, Any]]] = {}
        self._approvals: dict[str, ApprovalTicket] = {}
        self._consumed: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._flags: dict[str, list[str]] = {}
        self._control: dict[str, Any] = {}

    async def get(self, session_id: str) -> SessionState | None:
        item = self._sessions.get(session_id)
        if item is None or item[0] < self._clock():
            self._sessions.pop(session_id, None)
            return None
        return SessionState.from_dict(dict(item[1]))

    async def put(self, state: SessionState) -> None:
        self._sessions[state.session_id] = (self._clock() + self._ttl, state.to_dict())

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    @contextlib.asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield

    async def put_approval(self, ticket: ApprovalTicket) -> None:
        self._approvals[ticket.approval_id] = ticket

    async def get_approval(self, approval_id: str) -> ApprovalTicket | None:
        ticket = self._approvals.get(approval_id)
        if ticket is None:
            return None
        return ApprovalTicket(**{**ticket.__dict__, "used": approval_id in self._consumed})

    async def consume_approval(self, approval_id: str) -> bool:
        if approval_id in self._consumed or approval_id not in self._approvals:
            return False
        self._consumed.add(approval_id)
        return True

    async def get_flags(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._flags.items()}

    async def put_flags(self, flags: dict[str, list[str]]) -> None:
        self._flags = {k: list(v) for k, v in flags.items()}

    async def get_control(self, key: str) -> Any:
        import copy

        return copy.deepcopy(self._control.get(key))

    async def put_control(self, key: str, value: Any) -> None:
        import copy

        self._control[key] = copy.deepcopy(value)

    async def ping(self) -> bool:
        return True
