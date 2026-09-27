"""Loads the intent catalogue and the agent registry, and checks they agree.

Both files are versioned configuration. They are validated together at start-up
so a broken catalogue never reaches production: every step must reference a
registered agent and one of its declared skills, writes are only allowed on
agents registered for writes, and step dependencies must form a DAG.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError
from .models import AgentRecord, Intent, RiskClass, StepMode, StepSpec
from .slots import SLOT_KINDS, SlotSpec


@dataclass
class Catalogue:
    intents: dict[str, Intent]
    agents: dict[str, AgentRecord]
    # The parsed YAML, kept so governed runtime changes can be applied and re-validated.
    source: dict[str, Any] = field(default_factory=dict)

    def intent(self, intent_id: str) -> Intent | None:
        return self.intents.get(intent_id)

    def agent(self, name: str) -> AgentRecord | None:
        return self.agents.get(name)


def _load_yaml(path: str) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"catalogue file not found: {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{file}: expected a mapping at the top level")
    return data


def parse_agents(data: dict[str, Any]) -> dict[str, AgentRecord]:
    agents: dict[str, AgentRecord] = {}
    for i, raw in enumerate(data.get("agents") or []):
        where = f"agents[{i}]"
        try:
            record = AgentRecord(
                name=str(raw["name"]),
                audience=str(raw["audience"]),
                skills=tuple(str(s) for s in raw.get("skills") or ()),
                certified_in=tuple(str(e) for e in raw.get("certified_in") or ()),
                clearance=tuple(str(c) for c in raw.get("clearance") or ("public",)),
                writes_allowed=bool(raw.get("writes_allowed", False)),
                card_sha256=str(raw.get("card_sha256", "")),
                allowed_callees=tuple(str(c) for c in raw.get("allowed_callees") or ()),
                cost_units=int(raw.get("cost_units", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: invalid agent record ({exc})") from exc
        if record.name in agents:
            raise ConfigError(f"{where}: duplicate agent {record.name!r}")
        if not record.skills:
            raise ConfigError(f"{where}: agent {record.name!r} declares no skills")
        agents[record.name] = record
    return agents


def _parse_step(raw: dict[str, Any], where: str, agents: dict[str, AgentRecord]) -> StepSpec:
    try:
        agent_name = str(raw["agent"])
        step = StepSpec(
            id=str(raw["id"]),
            agent=agent_name,
            skill=str(raw["skill"]),
            mode=StepMode(str(raw.get("mode", "read"))),
            depends_on=tuple(str(d) for d in raw.get("depends_on") or ()),
            optional=bool(raw.get("optional", False)),
            timeout_ms=int(raw["timeout_ms"]) if raw.get("timeout_ms") is not None else None,
            data_classes=tuple(str(c) for c in raw.get("data_classes") or ("internal",)),
            cost_units=int(raw.get("cost_units", agents[agent_name].cost_units if agent_name in agents else 1)),
            instruction=str(raw.get("instruction", "")),
            include_query=bool(raw.get("include_query", False)),
            skip_if_slot=str(raw.get("skip_if_slot") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: invalid step ({exc})") from exc
    return step


def _parse_slots(raw: Any, where: str) -> tuple[SlotSpec, ...]:
    slots: list[SlotSpec] = []
    for j, item in enumerate(raw or []):
        try:
            spec = SlotSpec(name=str(item["name"]), kind=str(item["kind"]), required=bool(item.get("required", False)),
                            prompt=str(item.get("prompt", "")))
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"{where}.slots[{j}]: invalid slot ({exc})") from exc
        if spec.kind not in SLOT_KINDS:
            raise ConfigError(f"{where}.slots[{j}]: kind must be one of {list(SLOT_KINDS)}")
        if spec.required and not spec.prompt:
            raise ConfigError(f"{where}.slots[{j}]: required slot {spec.name!r} needs a prompt")
        if spec.name in {s.name for s in slots}:
            raise ConfigError(f"{where}.slots[{j}]: duplicate slot {spec.name!r}")
        slots.append(spec)
    return tuple(slots)


def _check_patterns(where: str, patterns: tuple[str, ...]) -> None:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            raise ConfigError(f"{where}: invalid pattern {p!r} ({exc})") from exc


def _check_dag(intent_id: str, steps: list[StepSpec]) -> None:
    ids = {s.id for s in steps}
    if len(ids) != len(steps):
        raise ConfigError(f"intent {intent_id!r}: duplicate step ids")
    for s in steps:
        missing = set(s.depends_on) - ids
        if missing:
            raise ConfigError(f"intent {intent_id!r}: step {s.id!r} depends on unknown steps {sorted(missing)}")
    graph = {s.id: set(s.depends_on) for s in steps}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise ConfigError(f"intent {intent_id!r}: step dependencies contain a cycle at {node!r}")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node)


def parse_intents(data: dict[str, Any], agents: dict[str, AgentRecord], acr_levels: list[str]) -> dict[str, Intent]:
    intents: dict[str, Intent] = {}
    for i, raw in enumerate(data.get("intents") or []):
        where = f"intents[{i}]"
        try:
            intent_id = str(raw["id"])
            risk = RiskClass(str(raw["risk"]))
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"{where}: invalid id or risk ({exc})") from exc
        if intent_id in intents:
            raise ConfigError(f"{where}: duplicate intent {intent_id!r}")
        required_acr = str(raw.get("required_acr", acr_levels[0]))
        if required_acr not in acr_levels:
            raise ConfigError(f"{where}: required_acr {required_acr!r} is not in auth.acr_levels")
        steps = [_parse_step(s, f"{where}.steps[{j}]", agents) for j, s in enumerate(raw.get("steps") or [])]
        if not steps:
            raise ConfigError(f"{where}: intent {intent_id!r} has no steps")
        _check_dag(intent_id, steps)
        for s in steps:
            agent = agents.get(s.agent)
            if agent is None:
                raise ConfigError(f"{where}: step {s.id!r} references unregistered agent {s.agent!r}")
            if s.skill not in agent.skills:
                raise ConfigError(f"{where}: agent {s.agent!r} does not declare skill {s.skill!r}")
            if s.mode is StepMode.WRITE and not agent.writes_allowed:
                raise ConfigError(f"{where}: agent {s.agent!r} is not registered for write steps")
            if s.include_query and (risk is not RiskClass.R0 or set(s.data_classes) - {"public"}):
                # The user's words only ever go to public information steps.
                raise ConfigError(f"{where}: step {s.id!r} may only use include_query in an R0 intent with public data")
        slots = _parse_slots(raw.get("slots"), where)
        for s in steps:
            if s.skip_if_slot and (not s.optional or s.skip_if_slot not in {sp.name for sp in slots}):
                raise ConfigError(f"{where}: step {s.id!r} may only use skip_if_slot on an optional step with a declared slot")
        has_writes = any(s.mode is StepMode.WRITE for s in steps)
        if has_writes and risk is not RiskClass.R3:
            raise ConfigError(f"{where}: intent {intent_id!r} has write steps and must be risk R3")
        if risk is RiskClass.R3 and not has_writes:
            raise ConfigError(f"{where}: R3 intent {intent_id!r} must contain a write step")
        if sum(1 for s in steps if s.mode is StepMode.WRITE) > 1:
            # Multi-write transactions need compensation (saga) steps, which this release does not model.
            raise ConfigError(f"{where}: intent {intent_id!r} has more than one write step; split it or add saga support")
        patterns = tuple(str(p) for p in raw.get("patterns") or ())
        if not patterns:
            raise ConfigError(f"{where}: intent {intent_id!r} needs at least one routing pattern")
        quorum = raw.get("quorum")
        if quorum is not None and quorum not in ("all", "majority", "any"):
            raise ConfigError(f"{where}: quorum must be all, majority or any")
        if risk is RiskClass.R3 and not raw.get("readback_template"):
            raise ConfigError(f"{where}: R3 intent {intent_id!r} needs a readback_template")
        exclude = tuple(str(p) for p in raw.get("exclude_patterns") or ())
        _check_patterns(where, patterns + exclude)
        intents[intent_id] = Intent(
            id=intent_id,
            risk=risk,
            description=str(raw.get("description", "")),
            required_acr=required_acr,
            patterns=patterns,
            steps=tuple(steps),
            quorum=quorum,
            clarification_prompt=str(raw.get("clarification_prompt", "")),
            readback_template=str(raw.get("readback_template", "")),
            label=str(raw.get("label", "")),
            exclude_patterns=exclude,
            slots=slots,
        )
    if not intents:
        raise ConfigError("intent catalogue is empty")
    return intents


def load_catalogue(intents_file: str, registry_file: str, acr_levels: list[str]) -> Catalogue:
    raw_agents = _load_yaml(registry_file)
    raw_intents = _load_yaml(intents_file)
    return catalogue_from_source({"intents": raw_intents, "agents": raw_agents}, acr_levels)


def catalogue_from_source(source: dict[str, Any], acr_levels: list[str]) -> Catalogue:
    """Parse and validate a catalogue from its YAML structure (used at start-up and for runtime changes)."""
    agents = parse_agents(source["agents"])
    intents = parse_intents(source["intents"], agents, acr_levels)
    return Catalogue(intents=intents, agents=agents, source=source)
