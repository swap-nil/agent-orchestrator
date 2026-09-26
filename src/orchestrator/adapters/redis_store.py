"""Redis session store (Azure Managed Redis / Redis 7+).

* Sessions are JSON documents with a sliding TTL.
* The per-session lock is a SET NX PX token lock with a safe compare-and-delete
  release (Lua), so a slow replica cannot release another replica's lock.
* Approval consumption is atomic (SET NX), which makes approvals one-time-use
  across all replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from typing import Any, AsyncIterator

import redis.asyncio as redis

from ..models import ApprovalTicket
from ..state import SessionState, StateError

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


class RedisSessionStore:
    def __init__(self, url: str, ttl_s: int, lock_timeout_ms: int, prefix: str = "orch") -> None:
        self._r = redis.from_url(url, decode_responses=True, health_check_interval=15)
        self._ttl = ttl_s
        self._lock_ms = lock_timeout_ms
        self._p = prefix
        self._release = self._r.register_script(_RELEASE)

    def _k(self, *parts: str) -> str:
        return ":".join((self._p, *parts))

    async def get(self, session_id: str) -> SessionState | None:
        raw = await self._r.get(self._k("session", session_id))
        return SessionState.from_dict(json.loads(raw)) if raw else None

    async def put(self, state: SessionState) -> None:
        await self._r.set(self._k("session", state.session_id), json.dumps(state.to_dict()), ex=self._ttl)

    async def delete(self, session_id: str) -> None:
        await self._r.delete(self._k("session", session_id))

    @contextlib.asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        key = self._k("lock", session_id)
        token = secrets.token_hex(16)
        waited = 0.0
        while not await self._r.set(key, token, nx=True, px=self._lock_ms):
            if waited * 1000 >= self._lock_ms:
                raise StateError("session is busy")
            await asyncio.sleep(0.02)
            waited += 0.02
        try:
            yield
        finally:
            await self._release(keys=[key], args=[token])

    async def put_approval(self, ticket: ApprovalTicket) -> None:
        ttl = max(1, int(ticket.expires_at - time.time()) + 60)
        await self._r.set(self._k("approval", ticket.approval_id), json.dumps(ticket.__dict__), ex=ttl)

    async def get_approval(self, approval_id: str) -> ApprovalTicket | None:
        raw = await self._r.get(self._k("approval", approval_id))
        if not raw:
            return None
        ticket = ApprovalTicket(**json.loads(raw))
        ticket.used = bool(await self._r.exists(self._k("approval-used", approval_id)))
        return ticket

    async def consume_approval(self, approval_id: str) -> bool:
        if not await self._r.exists(self._k("approval", approval_id)):
            return False
        return bool(await self._r.set(self._k("approval-used", approval_id), "1", nx=True, ex=86_400))

    async def get_flags(self) -> dict[str, list[str]]:
        raw = await self._r.get(self._k("flags"))
        return json.loads(raw) if raw else {}

    async def put_flags(self, flags: dict[str, list[str]]) -> None:
        await self._r.set(self._k("flags"), json.dumps(flags))

    async def get_control(self, key: str) -> Any:
        raw = await self._r.get(self._k("control", key))
        return json.loads(raw) if raw else None

    async def put_control(self, key: str, value: Any) -> None:
        await self._r.set(self._k("control", key), json.dumps(value))

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except redis.RedisError:
            return False
