"""A small kit for building domain agents that meet the orchestrator's contract.

The contract (see the user guide, "Adding a domain agent"):

* Speak A2A v1.0 JSON-RPC ``SendMessage``; require the ``A2A-Version`` header.
* Honour ``metadata.idempotencyKey``: the same key returns the same result and
  never repeats a side effect.
* Honour ``metadata.deadlineEpochMs``: do not start work after the deadline.
* Verify the delegated bearer token's audience is this agent (when enabled).
* Return artifacts whose metadata lists ``sources`` and ``classification``.
* Prepare steps return the proposed action in ``data.action``; write steps
  require ``metadata.approvalToken`` and must be re-verified by the tool gateway.

``handle`` is framework-free and unit-tested; ``asgi_app`` wraps it in FastAPI.
"""

# No "from __future__ import annotations" here: asgi_app imports FastAPI lazily, and
# FastAPI must see the real Request class on its endpoints, not an unresolvable string.
import asyncio
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

SUPPORTED_VERSIONS = ("1.0",)


@dataclass
class SkillRequest:
    skill: str
    instruction: str
    data: dict[str, Any]
    metadata: dict[str, Any]
    context_id: str
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    text: str
    sources: list[str]
    classification: str = "internal"
    data: dict[str, Any] = field(default_factory=dict)
    state: str = "TASK_STATE_COMPLETED"


SkillHandler = Callable[[SkillRequest], Awaitable[SkillResult]]
TokenVerifier = Callable[[str], dict[str, Any]]


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class DomainAgent:
    def __init__(
        self,
        name: str,
        skills: dict[str, SkillHandler],
        *,
        write_skills: set[str] | None = None,
        token_verifier: TokenVerifier | None = None,
        idempotency_capacity: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self._skills = skills
        self._writes = write_skills or set()
        self._verify = token_verifier
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._capacity = idempotency_capacity
        self._clock = clock
        self._in_flight: dict[str, asyncio.Lock] = {}

    async def handle(self, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        req_id = body.get("id") if isinstance(body, dict) else None
        if headers.get("a2a-version") not in SUPPORTED_VERSIONS:
            return _rpc_error(req_id, -32009, "unsupported A2A version")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or body.get("method") != "SendMessage":
            return _rpc_error(req_id, -32601, "method not found")

        claims: dict[str, Any] = {}
        if self._verify is not None:
            auth = headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return _rpc_error(req_id, -32001, "unauthenticated")
            try:
                claims = self._verify(auth[7:].strip())
            except Exception:  # noqa: BLE001
                return _rpc_error(req_id, -32001, "invalid token")

        params = body.get("params") or {}
        message = params.get("message") or {}
        metadata = message.get("metadata") or params.get("metadata") or {}
        parts = message.get("parts") or []
        instruction = " ".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
        data: dict[str, Any] = {}
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("data"), dict):
                data.update(p["data"])
        skill = str(data.get("skill") or metadata.get("skill") or "")
        if skill not in self._skills:
            return _rpc_error(req_id, -32602, f"unknown skill {skill!r}")

        key = str(metadata.get("idempotencyKey") or "")
        if not key:
            return await self._execute(req_id, skill, instruction, data, metadata, message, claims, key)
        # Serialise duplicates of the same key so concurrent retries never execute twice.
        lock = self._in_flight.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                if key in self._cache:
                    return {"jsonrpc": "2.0", "id": req_id, "result": self._cache[key]}
                return await self._execute(req_id, skill, instruction, data, metadata, message, claims, key)
        finally:
            if not lock.locked() and self._in_flight.get(key) is lock:
                self._in_flight.pop(key, None)

    async def _execute(
        self, req_id: Any, skill: str, instruction: str, data: dict[str, Any], metadata: dict[str, Any],
        message: dict[str, Any], claims: dict[str, Any], key: str,
    ) -> dict[str, Any]:
        deadline_ms = metadata.get("deadlineEpochMs")
        if isinstance(deadline_ms, (int, float)) and self._clock() * 1000 > deadline_ms:
            return _rpc_error(req_id, -32010, "deadline exceeded before start")
        if skill in self._writes and not metadata.get("approvalToken"):
            return _rpc_error(req_id, -32011, "write requires an approval token")

        result = await self._skills[skill](SkillRequest(skill, instruction, data, metadata, str(message.get("contextId", "")), claims))
        task = {
            "id": uuid.uuid4().hex,
            "contextId": message.get("contextId"),
            "status": {"state": result.state},
            "artifacts": [{
                "artifactId": uuid.uuid4().hex,
                "name": skill,
                "parts": [{"text": result.text}] + ([{"data": result.data}] if result.data else []),
                "metadata": {"sources": result.sources, "classification": result.classification, "agent": self.name},
            }],
        }
        payload = {"task": task}
        if key:
            self._cache[key] = payload
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
        return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def asgi_app(agents: dict[str, DomainAgent], path_template: str = "/agents/{agent}"):  # type: ignore[no-untyped-def]
    """FastAPI app hosting one or more agents at /agents/{name}."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="domain agents", docs_url=None, redoc_url=None)
    route = path_template.replace("{agent}", "{name}")

    @app.post(route)
    async def rpc(name: str, request: Request) -> JSONResponse:
        agent = agents.get(name)
        if agent is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        headers = {k.lower(): v for k, v in request.headers.items()}
        return JSONResponse(await agent.handle(await request.json(), headers))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
