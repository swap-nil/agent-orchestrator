"""Policy enforcement point (PEP).

Every delegation is checked against the policy decision point before it runs.
In production the PDP is OPA (``policies/orchestrator.rego``). The local
engine implements the same rules in Python for development and tests; the
production profile refuses to start with it.

The PEP fails closed: timeouts, errors and malformed responses are denials.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import KillSwitchConfig, PolicyConfig
from .models import AgentRecord, Intent, RiskClass, StepMode, StepSpec, UserContext
from .planner import acr_rank
from .transport import HttpTransport, TransportError


@dataclass
class PolicyDecision:
    allow: bool
    reasons: list[str] = field(default_factory=list)
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    engine: str = "local"
    cached: bool = False


def build_policy_input(
    *,
    environment: str,
    cell_id: str,
    user: UserContext,
    intent: Intent,
    step: StepSpec,
    agent: AgentRecord | None,
    acr_levels: list[str],
    kill: KillSwitchConfig,
    allowed_channels: list[str],
    phase: str,
    approval_valid: bool,
) -> dict[str, Any]:
    return {
        "environment": environment,
        "cell": cell_id,
        "phase": phase,
        "user": {
            "subject": user.subject,
            "acr": user.acr,
            "acr_rank": acr_rank(user.acr, acr_levels),
            "tenant": user.tenant,
            "entitlements": list(user.entitlements),
            "channel": user.channel,
        },
        "intent": {
            "id": intent.id,
            "risk": intent.risk.value,
            "required_acr": intent.required_acr,
            "required_acr_rank": acr_rank(intent.required_acr, acr_levels),
        },
        "step": {
            "id": step.id,
            "agent": step.agent,
            "skill": step.skill,
            "mode": step.mode.value,
            "data_classes": list(step.data_classes),
        },
        "agent": None
        if agent is None
        else {
            "name": agent.name,
            "certified_in": list(agent.certified_in),
            "clearance": list(agent.clearance),
            "writes_allowed": agent.writes_allowed,
            "skills": list(agent.skills),
        },
        "approval": {"valid": approval_valid},
        "kill_switch": {
            "disabled_agents": list(kill.disabled_agents),
            "disabled_intents": list(kill.disabled_intents),
            "disabled_risk_classes": list(kill.disabled_risk_classes),
        },
        "allowed_channels": list(allowed_channels),
    }


class PolicyEngine(Protocol):
    async def decide(self, policy_input: dict[str, Any]) -> PolicyDecision: ...


class LocalPolicyEngine:
    """Python mirror of policies/orchestrator.rego. Keep the two in sync (tests check both)."""

    async def decide(self, policy_input: dict[str, Any]) -> PolicyDecision:
        return PolicyDecision(allow=not (reasons := local_deny_reasons(policy_input)), reasons=reasons, engine="local")


def local_deny_reasons(inp: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    user, intent, step, agent = inp["user"], inp["intent"], inp["step"], inp["agent"]
    kill = inp["kill_switch"]
    if agent is None:
        return ["agent not registered"]
    if inp["environment"] not in agent["certified_in"]:
        reasons.append("agent not certified for environment")
    if step["skill"] not in agent["skills"]:
        reasons.append("skill not offered by agent")
    if user["acr_rank"] < 0 or user["acr_rank"] < intent["required_acr_rank"]:
        reasons.append("insufficient authentication level")
    if not user["tenant"]:
        reasons.append("missing tenant")
    if user["channel"] not in inp["allowed_channels"]:
        reasons.append("channel not allowed")
    if not set(step["data_classes"]) <= set(agent["clearance"]):
        reasons.append("agent not cleared for data class")
    if step["mode"] == "write" and not agent["writes_allowed"]:
        reasons.append("agent not permitted to write")
    if step["mode"] == "write" and intent["risk"] != "R3":
        reasons.append("write outside a transaction intent")
    if step["mode"] == "write" and inp["phase"] == "execute" and not inp["approval"]["valid"]:
        reasons.append("write requires a valid approval")
    if agent["name"] in kill["disabled_agents"]:
        reasons.append("agent disabled by kill switch")
    if intent["id"] in kill["disabled_intents"]:
        reasons.append("intent disabled by kill switch")
    if intent["risk"] in kill["disabled_risk_classes"]:
        reasons.append("risk class disabled by kill switch")
    return reasons


class OpaPolicyEngine:
    def __init__(self, config: PolicyConfig, transport: HttpTransport) -> None:
        self._url = f"{config.opa_url.rstrip('/')}/v1/data/{config.decision_path.strip('/')}"
        self._timeout_s = config.timeout_ms / 1000
        self._transport = transport

    async def decide(self, policy_input: dict[str, Any]) -> PolicyDecision:
        try:
            response = await self._transport.post(self._url, json_body={"input": policy_input}, timeout_s=self._timeout_s)
        except (TransportError, TimeoutError) as exc:
            return PolicyDecision(False, [f"policy engine unavailable: {type(exc).__name__}"], engine="opa")
        if response.status != 200 or not isinstance(response.body, dict):
            return PolicyDecision(False, [f"policy engine error: HTTP {response.status}"], engine="opa")
        result = response.body.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("allow"), bool):
            return PolicyDecision(False, ["policy engine returned no decision"], engine="opa")
        reasons = [str(r) for r in result.get("reasons") or []]
        return PolicyDecision(result["allow"] and not reasons, reasons, engine="opa")


class PolicyEnforcementPoint:
    """Caches allow decisions for low-risk reads only; never caches denials or writes."""

    def __init__(self, engine: PolicyEngine, config: PolicyConfig, clock: Any = time.monotonic) -> None:
        self._engine = engine
        self._ttl = config.cache_ttl_s
        self._cacheable = {RiskClass(r) for r in config.cache_risk_classes}
        self._cache: dict[tuple[Any, ...], tuple[float, PolicyDecision]] = {}
        self._clock = clock
        self._max_entries = 10_000

    def _key(self, inp: dict[str, Any]) -> tuple[Any, ...]:
        return (
            inp["user"]["subject"], inp["user"]["acr"], inp["user"]["tenant"], inp["user"]["channel"],
            inp["intent"]["id"], inp["step"]["id"], inp["phase"],
            tuple(inp["kill_switch"]["disabled_agents"]), tuple(inp["kill_switch"]["disabled_intents"]),
            tuple(inp["kill_switch"]["disabled_risk_classes"]),
        )

    async def check(self, inp: dict[str, Any]) -> PolicyDecision:
        risk = RiskClass(inp["intent"]["risk"])
        cacheable = risk in self._cacheable and inp["step"]["mode"] == StepMode.READ.value and self._ttl > 0
        now = self._clock()
        if cacheable:
            key = self._key(inp)
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                decision = hit[1]
                return PolicyDecision(decision.allow, list(decision.reasons), uuid.uuid4().hex, decision.engine, True)
        try:
            decision = await self._engine.decide(inp)
        except Exception as exc:  # noqa: BLE001 - fail closed on any engine fault
            decision = PolicyDecision(False, [f"policy evaluation failed: {type(exc).__name__}"])
        if cacheable and decision.allow:
            if len(self._cache) >= self._max_entries:
                self._cache.clear()
            self._cache[self._key(inp)] = (now + self._ttl, decision)
        return decision
