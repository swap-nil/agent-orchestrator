"""In-process A2A transport over the reference agent kit.

Lets the orchestrator talk to ``DomainAgent`` instances without a network:
used by the evals harness (end-to-end cases), the contract tests and the
local command-center dev server. Fault injection (latency, failures) per
agent makes the dev server's dashboards show realistic behaviour and lets
operators rehearse incidents.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

from orchestrator.transport import HttpResponse, TransportError

from .kit import DomainAgent


@dataclass
class Fault:
    latency_ms: tuple[int, int] = (40, 180)
    failure_rate: float = 0.0  # probability of an HTTP 503
    timeout_rate: float = 0.0  # probability of hanging past any deadline


@dataclass
class LocalAgentTransport:
    agents: dict[str, DomainAgent]
    faults: dict[str, Fault] = field(default_factory=dict)
    realistic: bool = False  # apply latency and faults (dev server); evals and tests run instantly
    rng: random.Random = field(default_factory=random.Random)

    async def post(self, url: str, *, json_body: Any = None, form: dict[str, str] | None = None,
                   headers: dict[str, str] | None = None, timeout_s: float) -> HttpResponse:
        if url.rstrip("/").endswith("/token"):
            audience = (form or {}).get("audience") or (form or {}).get("scope", "")
            return HttpResponse(200, {"access_token": f"local-{audience}", "expires_in": 300})
        name = url.rstrip("/").rsplit("/", 1)[-1]
        agent = self.agents.get(name)
        if agent is None:
            return HttpResponse(404, {"error": "unknown agent"})
        if self.realistic:
            fault = self.faults.get(name, Fault())
            await asyncio.sleep(self.rng.uniform(*fault.latency_ms) / 1000)
            roll = self.rng.random()
            if roll < fault.timeout_rate:
                await asyncio.sleep(timeout_s + 1)
                raise TransportError("timeout")
            if roll < fault.timeout_rate + fault.failure_rate:
                return HttpResponse(503, {"error": "unavailable"})
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        return HttpResponse(200, await agent.handle(json_body, lowered))

    async def get(self, url: str, *, headers: dict[str, str] | None = None, timeout_s: float) -> HttpResponse:
        return HttpResponse(404, {})
