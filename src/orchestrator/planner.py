"""Builds and validates execution plans.

Plans come from the intent catalogue (deterministic templates), not from a
model. Every plan is validated before any step runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .catalogue import Catalogue
from .config import BudgetConfig, KillSwitchConfig
from .models import Intent, Plan, RiskClass, StepSpec, UserContext


@dataclass
class PlanCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def acr_rank(acr: str, levels: list[str]) -> int:
    try:
        return levels.index(acr)
    except ValueError:
        return -1


def build_layers(steps: list[StepSpec]) -> list[list[StepSpec]]:
    remaining = {s.id: s for s in steps}
    done: set[str] = set()
    layers: list[list[StepSpec]] = []
    while remaining:
        ready = [s for s in remaining.values() if set(s.depends_on) <= done]
        if not ready:  # cycle; the catalogue loader rejects these, this is defence in depth
            raise ValueError("plan contains a dependency cycle")
        ready.sort(key=lambda s: s.id)
        layers.append(ready)
        for s in ready:
            done.add(s.id)
            del remaining[s.id]
    return layers


class Planner:
    def __init__(
        self,
        catalogue: Catalogue,
        budgets: BudgetConfig,
        acr_levels: list[str],
        environment: str,
    ) -> None:
        self._catalogue = catalogue
        self._budgets = budgets
        self._acr_levels = acr_levels
        self._environment = environment

    def build(self, intent: Intent, skip_optional: bool = False, unavailable_agents: set[str] | None = None,
              filled_slots: set[str] | None = None) -> Plan:
        """Optional steps are dropped under load (skip_optional) or when their agent is switched off,
        so the customer still gets the required part of the answer, marked partial. Steps whose
        ``skip_if_slot`` is filled are not needed for this question and are left out quietly."""
        unavailable = unavailable_agents or set()
        filled = filled_slots or set()
        steps = [s for s in intent.steps if not (s.optional and (skip_optional or s.agent in unavailable))
                 and not (s.skip_if_slot and s.skip_if_slot in filled)]
        kept = {s.id for s in steps}
        # Drop dependencies on skipped optional steps.
        steps = [replace(s, depends_on=tuple(d for d in s.depends_on if d in kept)) for s in steps]
        return Plan(intent=intent, steps=steps, layers=build_layers(steps))

    def validate(
        self,
        plan: Plan,
        user: UserContext,
        kill: KillSwitchConfig,
        session_cost_used: int,
    ) -> PlanCheck:
        reasons: list[str] = []
        intent = plan.intent
        b = self._budgets

        if intent.id in kill.disabled_intents:
            reasons.append(f"intent {intent.id} is disabled")
        if intent.risk.value in kill.disabled_risk_classes:
            reasons.append(f"risk class {intent.risk.value} is disabled")
        if acr_rank(user.acr, self._acr_levels) < acr_rank(intent.required_acr, self._acr_levels):
            reasons.append("authentication level too low for this intent")
        if len(plan.steps) > b.max_steps:
            reasons.append(f"plan has {len(plan.steps)} steps, limit {b.max_steps}")
        if len(plan.layers) > b.max_depth:
            reasons.append(f"plan depth {len(plan.layers)} exceeds limit {b.max_depth}")
        widest = max((len(layer) for layer in plan.layers), default=0)
        if widest > b.max_fan_out:
            reasons.append(f"plan fan-out {widest} exceeds limit {b.max_fan_out}")
        if plan.cost_units > b.max_cost_units_per_turn:
            reasons.append("plan exceeds the per-turn cost budget")
        if session_cost_used + plan.cost_units > b.max_cost_units_per_session:
            reasons.append("session cost budget exhausted")
        if intent.risk is RiskClass.R3 and not plan.write_steps:
            reasons.append("transaction plan has no write step")

        for step in plan.steps:
            agent = self._catalogue.agent(step.agent)
            if agent is None:
                reasons.append(f"step {step.id}: agent {step.agent} is not registered")
                continue
            if agent.name in kill.disabled_agents:
                reasons.append(f"step {step.id}: agent {agent.name} is disabled")
            if self._environment not in agent.certified_in:
                reasons.append(f"step {step.id}: agent {agent.name} is not certified for {self._environment}")
            if step.skill not in agent.skills:
                reasons.append(f"step {step.id}: skill {step.skill} not offered by {agent.name}")
            uncleared = set(step.data_classes) - set(agent.clearance)
            if uncleared:
                reasons.append(f"step {step.id}: agent {agent.name} not cleared for {sorted(uncleared)}")
        return PlanCheck(ok=not reasons, reasons=reasons)
