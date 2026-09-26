"""Shared fakes for the test suite."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from orchestrator.bootstrap import build_service  # noqa: E402
from orchestrator.config import OrchestratorConfig, load_config  # noqa: E402
from orchestrator.models import UserContext  # noqa: E402
from orchestrator.service import OrchestratorService  # noqa: E402
from orchestrator.transport import FakeTransport, HttpResponse  # noqa: E402

APPROVAL_KEY = "k" * 48


def dev_config(**overrides: Any) -> OrchestratorConfig:
    config = load_config(os.path.join(ROOT, "config", "orchestrator.dev.yaml"), environ={}, validate=False)
    config.catalogue.intents_file = os.path.join(ROOT, "config", "intents.yaml")
    config.catalogue.registry_file = os.path.join(ROOT, "config", "agents.yaml")
    config.audit.sink = "memory"
    for path, value in overrides.items():
        node: Any = config
        parts = path.split(".")
        for part in parts[:-1]:
            node = getattr(node, part)
        setattr(node, parts[-1], value)
    return config


def task_result(text: str, sources: list[str] | None = None, data: dict[str, Any] | None = None,
                state: str = "TASK_STATE_COMPLETED", classification: str = "internal") -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"text": text}]
    if data:
        parts.append({"data": data})
    return {
        "task": {
            "id": "task-" + text[:6].replace(" ", "-"),
            "contextId": "ctx",
            "status": {"state": state},
            "artifacts": [{
                "artifactId": "a1",
                "parts": parts,
                "metadata": {"sources": sources if sources is not None else ["doc://kb/1"], "classification": classification},
            }],
        }
    }


DEFAULT_REPLIES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "faq.answer": lambda req: task_result("Branches open 9:00 to 17:00.", ["kb://opening-hours"], classification="public"),
    "portfolio.holdings": lambda req: task_result("You hold 3 positions worth CHF 120,000.", ["core://positions/2026-09-24"],
                                                  data={"positions": 3}, classification="client_confidential"),
    "market.quotes": lambda req: task_result("SMI is up 0.4% today.", ["mkt://smi"], classification="public"),
    "advice.rebalance": lambda req: task_result("Draft: shift 5% from equities to bonds.", ["model://alloc/v3"]),
    "compliance.suitability": lambda req: task_result("Moving about 5% from equities to bonds would bring you back to your agreed risk profile.", ["policy://suitability/v7"]),
    "trade.prepare": lambda req: task_result("Order prepared.", ["core://quote/1"], data={"action": {
        "instrument": "Tech ETF", "quantity": 50, "account_mask": "****1234", "estimated_amount": 6200, "currency": "CHF"}}),
    "trade.execute": lambda req: task_result("Order placed.", ["core://orders/9"], data={"reference": "ORD-9"}),
}


class FakeGateway:
    """Fake A2A gateway: routes JSON-RPC calls to per-skill reply functions."""

    def __init__(self, replies: dict[str, Any] | None = None, delay_s: float = 0.0) -> None:
        self.replies = dict(DEFAULT_REPLIES)
        if replies:
            self.replies.update(replies)
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []
        self.transport = FakeTransport(self._handle)

    async def _handle(self, method: str, url: str, request: dict[str, Any]) -> HttpResponse:
        if url.endswith("/token"):
            return HttpResponse(200, {"access_token": "delegated-" + request["form"].get("audience", request["form"].get("scope", "")), "expires_in": 300})
        body = request["json"]
        if body.get("method") != "SendMessage":
            return HttpResponse(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        skill = body["params"]["message"]["parts"][-1]["data"]["skill"]
        self.calls.append({"url": url, "skill": skill, "body": body, "headers": request["headers"]})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        reply = self.replies[skill]
        result = reply(body) if callable(reply) else reply
        if isinstance(result, HttpResponse):
            return result
        if isinstance(result, Exception):
            raise result
        return HttpResponse(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})


class FakeWorkflows:
    def __init__(self) -> None:
        self.started: dict[str, dict[str, Any]] = {}
        self.signals: dict[str, list[dict[str, Any]]] = {}

    async def start_transaction(self, workflow_id: str, payload: dict[str, Any]) -> None:
        if workflow_id in self.started:
            raise RuntimeError("workflow already started")
        self.started[workflow_id] = payload

    async def signal_approval(self, workflow_id: str, decision: dict[str, Any]) -> None:
        self.signals.setdefault(workflow_id, []).append(decision)

    async def status(self, workflow_id: str) -> dict[str, Any]:
        return {"status": "running" if workflow_id in self.started else "not_found"}


def make_service(config: OrchestratorConfig | None = None, gateway: FakeGateway | None = None,
                 workflows: FakeWorkflows | None = None) -> tuple[OrchestratorService, FakeGateway, FakeWorkflows]:
    config = config or dev_config()
    gateway = gateway or FakeGateway()
    workflows = workflows or FakeWorkflows()
    environ = {config.workflows.approval_signing_key_env: APPROVAL_KEY, config.identity.client_secret_env: "test-secret"}
    service = build_service(config, transport=gateway.transport, workflows=workflows, environ=environ)
    return service, gateway, workflows


async def open_session(service: OrchestratorService, session_id: str = "s-1", acr: str = "standard", subject: str = "u-1") -> None:
    await service.open_session(
        session_id=session_id,
        user=UserContext(subject=subject, acr=acr, tenant="ch-retail", channel="voice"),
        subject_token="user-token",
        token_expires_at=0,
    )
