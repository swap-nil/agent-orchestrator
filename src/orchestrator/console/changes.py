"""Governed runtime changes to agent behaviour.

Operators can improve how the platform behaves while it runs: how intents are
recognised (routing patterns), what agents are asked to do (step
instructions), what the assistant says (clarification prompts, read-backs,
disclaimer), which inputs and outputs are blocked (guard lists) and how
confident routing must be (thresholds, never below the configured floor for
advice and transactions).

Structure never changes at runtime: risk classes, steps, agents, dependencies
and write permissions go through code review and deployment. That boundary is
what makes hot-swapping safe.

Every change follows the same path, and every step is audited on the
``control-plane`` chain:

    propose -> validate -> evaluate (golden suite vs live baseline)
      -> optional shadow on live traffic -> approve (a different person)
      -> apply (new version, all replicas) -> rollback to any version

A change is applied only if its evaluation shows no regressions and reaches
``command_center.change_min_pass_rate``, and only on top of the version it was
evaluated against.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from dataclasses import asdict
from typing import Any

from ..catalogue import Catalogue, catalogue_from_source
from ..config import ConfigError, GuardConfig, RoutingConfig
from ..router import Router
from ..service import OrchestratorService, render_readback
from .evals import EvalRunner, compare

CONTROL_KEY = "runtime_config"
RISKS = ("R0", "R1", "R2", "R3")

# (target, field) -> kind. Anything else is structural and refused.
ALLOWED: dict[tuple[str, str], str] = {
    ("intent", "patterns"): "regex_list",
    ("intent", "clarification_prompt"): "text",
    ("intent", "readback_template"): "template",
    ("intent", "description"): "text",
    ("step", "instruction"): "long_text",
    ("step", "timeout_ms"): "timeout",
    ("guards", "injection_patterns"): "regex_list",
    ("guards", "prohibited_phrases"): "phrase_list",
    ("guards", "pressure_phrases"): "phrase_list",
    ("guards", "r2_disclaimer"): "text",
    ("routing", "min_confidence"): "thresholds",
}


class ChangeError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _find_intent(source: dict[str, Any], intent_id: str) -> dict[str, Any]:
    for raw in source["intents"].get("intents") or []:
        if raw.get("id") == intent_id:
            return raw
    raise ChangeError(f"unknown intent {intent_id!r}")


def _find_step(source: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    intent_id, _, step_id = ref.partition("/")
    intent = _find_intent(source, intent_id)
    for step in intent.get("steps") or []:
        if step.get("id") == step_id:
            return intent, step
    raise ChangeError(f"unknown step {ref!r} (use intent_id/step_id)")


def current_value(source: dict[str, Any], guards: GuardConfig, routing: RoutingConfig, op: dict[str, Any]) -> Any:
    target, field = op["target"], op["field"]
    if target == "intent":
        return copy.deepcopy(_find_intent(source, op["id"]).get(field, "" if field != "patterns" else []))
    if target == "step":
        _, step = _find_step(source, op["id"])
        return copy.deepcopy(step.get(field, "" if field == "instruction" else None))
    if target == "guards":
        return copy.deepcopy(getattr(guards, field))
    if target == "routing":
        return copy.deepcopy(getattr(routing, field))
    raise ChangeError(f"unknown target {target!r}")


def apply_ops(source: dict[str, Any], guards: GuardConfig, routing: RoutingConfig, ops: list[dict[str, Any]]) -> tuple[dict[str, Any], GuardConfig, RoutingConfig]:
    source, guards, routing = copy.deepcopy(source), copy.deepcopy(guards), copy.deepcopy(routing)
    for op in ops:
        target, field, value = op["target"], op["field"], copy.deepcopy(op["value"])
        if target == "intent":
            _find_intent(source, op["id"])[field] = value
        elif target == "step":
            _find_step(source, op["id"])[1][field] = value
        elif target == "guards":
            setattr(guards, field, value)
        elif target == "routing":
            merged = dict(routing.min_confidence)
            merged.update({k: float(v) for k, v in value.items()})
            routing.min_confidence = merged
    return source, guards, routing


class RuntimeConfigManager:
    def __init__(self, service: OrchestratorService, evals: EvalRunner, audit_chain: str = "control-plane", clock: Any = time.time) -> None:
        self.service = service
        self.evals = evals
        self._chain = audit_chain
        self._clock = clock
        cfg = service.cfg
        self._cc = cfg.command_center
        self.base_source = copy.deepcopy(service.c.catalogue.source)
        self.base_guards = copy.deepcopy(cfg.guards)
        self.base_routing = copy.deepcopy(cfg.routing)
        self.applied: list[dict[str, Any]] = []  # linear history of applied changes
        self.active = 0  # number of applied changes in effect (0 = files as deployed)
        self.changes: dict[str, dict[str, Any]] = {}
        self._last_refresh = 0.0
        self._shadow_id: str | None = None

    # ------------------------------------------------------------------ building

    def _ops_upto(self, version: int) -> list[dict[str, Any]]:
        return [op for change in self.applied[:version] for op in change["ops"]]

    def effective(self, version: int | None = None) -> tuple[dict[str, Any], GuardConfig, RoutingConfig]:
        return apply_ops(self.base_source, self.base_guards, self.base_routing, self._ops_upto(self.active if version is None else version))

    def build(self, source: dict[str, Any], guards: GuardConfig, routing: RoutingConfig) -> Catalogue:
        try:
            return catalogue_from_source(source, self.service.cfg.auth.acr_levels)
        except ConfigError as exc:
            raise ChangeError(f"catalogue would be invalid: {exc}") from exc

    # ------------------------------------------------------------------ validation

    def _validate_op(self, op: dict[str, Any], guards: GuardConfig, routing: RoutingConfig, source: dict[str, Any]) -> list[str]:
        """Returns a list of 'weakens a safeguard' notes; raises ChangeError when invalid."""
        for key in ("target", "field", "value"):
            if key not in op:
                raise ChangeError(f"each change needs target, field and value (missing {key})")
        kind = ALLOWED.get((op["target"], op["field"]))
        if kind is None:
            raise ChangeError(
                f"{op['target']}.{op['field']} cannot be changed at runtime: structural changes "
                "(risk, steps, agents, writes) need code review and a deployment")
        if op["target"] in ("intent", "step") and not op.get("id"):
            raise ChangeError("intent and step changes need an id")
        value, weakens = op["value"], []
        previous = current_value(source, guards, routing, op)
        if kind in ("regex_list", "phrase_list"):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
                raise ChangeError(f"{op['field']} must be a non-empty list of strings")
            if len(value) > 100 or any(len(v) > 300 for v in value):
                raise ChangeError(f"{op['field']}: at most 100 entries of 300 characters")
            if kind == "regex_list":
                for pattern in value:
                    try:
                        re.compile(pattern, re.IGNORECASE)
                    except re.error as exc:
                        raise ChangeError(f"invalid regular expression {pattern!r}: {exc}") from exc
                    if re.fullmatch(r"\.?[*+]?", pattern.strip()) or pattern.strip() in (".*", ".+", "^", "$", ""):
                        raise ChangeError(f"pattern {pattern!r} matches everything")
            removed = [v for v in (previous or []) if v not in value]
            if op["target"] == "guards" and removed:
                weakens.append(f"removes {len(removed)} entr{'y' if len(removed) == 1 else 'ies'} from guards.{op['field']}")
            if op["target"] == "intent" and op["field"] == "patterns":
                risk = _find_intent(source, op["id"]).get("risk")
                if risk in ("R2", "R3"):
                    weakens.append(f"changes how a {risk} intent is recognised")
        elif kind in ("text", "long_text", "template"):
            limit = {"text": 400, "long_text": 1500, "template": 500}[kind]
            if not isinstance(value, str) or len(value) > limit:
                raise ChangeError(f"{op['field']} must be text of at most {limit} characters")
            if kind == "template":
                if not value.strip():
                    raise ChangeError("a read-back template cannot be empty")
                try:
                    render_readback(value, {})
                except ValueError as exc:
                    raise ChangeError(str(exc)) from exc
            if op["field"] == "r2_disclaimer" and not value.strip():
                raise ChangeError("the advice disclaimer cannot be empty")
        elif kind == "timeout":
            if not isinstance(value, int) or not 200 <= value <= 10_000:
                raise ChangeError("timeout_ms must be an integer between 200 and 10000")
            intent, step = _find_step(source, op["id"])
            if step.get("mode", "read") == "read" and value > self.service.cfg.budgets.turn_deadline_ms:
                raise ChangeError("a read step's timeout cannot exceed budgets.turn_deadline_ms")
        elif kind == "thresholds":
            if not isinstance(value, dict) or not value or not set(value) <= set(RISKS):
                raise ChangeError("min_confidence must map R0..R3 to numbers")
            for risk, v in value.items():
                if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                    raise ChangeError(f"min_confidence.{risk} must be between 0 and 1")
                floor = self.base_routing.min_confidence.get(risk, 0)
                if risk in ("R2", "R3") and v < floor:
                    raise ChangeError(f"min_confidence.{risk} cannot go below the deployed floor {floor}")
                if v < routing.min_confidence.get(risk, 0):
                    weakens.append(f"lowers routing confidence for {risk}")
        op["previous"] = previous
        return weakens

    # ------------------------------------------------------------------ lifecycle

    async def _audit(self, event: str, data: dict[str, Any]) -> None:
        await self.service.c.audit.record(self._chain, event, data)

    async def ensure_baseline(self) -> Any:
        run = self.evals.latest(self.active)
        if run is None:
            source, guards, routing = self.effective()
            run = await self.evals.run(self.service.c.catalogue if self.active == self.service.runtime_version else self.build(source, guards, routing),
                                       guards, routing, label=f"baseline v{self.active}", runtime_version=self.active)
        return run

    async def propose(self, proposer: str, ops: list[dict[str, Any]], reason: str, title: str = "") -> dict[str, Any]:
        if not self._cc.runtime_changes_enabled:
            raise ChangeError("runtime changes are disabled", 403)
        if not ops:
            raise ChangeError("a change needs at least one operation")
        if len(reason.strip()) < 5:
            raise ChangeError("explain the reason for the change")
        source, guards, routing = self.effective()
        ops = [dict(op) for op in ops]
        weakens: list[str] = []
        for op in ops:
            weakens += self._validate_op(op, guards, routing, source)
        cand_source, cand_guards, cand_routing = apply_ops(source, guards, routing, ops)
        cand_catalogue = self.build(cand_source, cand_guards, cand_routing)
        change_id = "chg-" + uuid.uuid4().hex[:8]
        baseline = await self.ensure_baseline()
        run = await self.evals.run(cand_catalogue, cand_guards, cand_routing, label=f"candidate {change_id}",
                                   runtime_version=self.active, change_id=change_id)
        comparison = compare(baseline, run)
        gate_ok = not comparison["regressions"] and run.pass_rate >= self._cc.change_min_pass_rate
        change = {
            "id": change_id, "title": title or self._summarise(ops), "created": self._clock(), "proposer": proposer,
            "reason": reason.strip(), "ops": ops, "base_version": self.active,
            "status": "evaluated" if gate_ok else "gate_failed", "weakens": weakens,
            "eval": run.summary(), "comparison": comparison,
            "gate": {"passed": gate_ok, "min_pass_rate": self._cc.change_min_pass_rate,
                     "reasons": ([f"{len(comparison['regressions'])} regression(s)"] if comparison["regressions"] else [])
                                + ([f"pass rate {run.pass_rate:.1%} below {self._cc.change_min_pass_rate:.0%}"] if run.pass_rate < self._cc.change_min_pass_rate else [])},
            "approver": None, "decided_at": None, "decision_note": "", "applied_version": None, "shadow": None,
        }
        self.changes[change_id] = change
        await self._audit("change_proposed", {"change_id": change_id, "proposer": proposer, "reason": change["reason"],
                                              "ops": [{k: op[k] for k in ("target", "id", "field", "value") if k in op} for op in ops],
                                              "weakens": weakens, "base_version": self.active})
        await self._audit("change_evaluated", {"change_id": change_id, "eval_run": run.id, "pass_rate": run.pass_rate,
                                               "regressions": comparison["regressions"], "fixes": comparison["fixes"], "gate_passed": gate_ok})
        return change

    def _get(self, change_id: str) -> dict[str, Any]:
        change = self.changes.get(change_id)
        if change is None:
            raise ChangeError("unknown change", 404)
        if change["status"] in ("evaluated", "gate_failed", "shadow") and self._clock() - change["created"] > self._cc.change_ttl_s:
            change["status"] = "expired"
        return change

    async def start_shadow(self, change_id: str, operator: str) -> dict[str, Any]:
        change = self._get(change_id)
        if change["status"] != "evaluated":
            raise ChangeError(f"only an evaluated change can run in shadow (status: {change['status']})", 409)
        if change["base_version"] != self.active:
            raise ChangeError("the live configuration changed since this was evaluated; propose it again", 409)
        source, guards, routing = apply_ops(*self.effective(), change["ops"])
        router = Router(self.build(source, guards, routing), routing, getattr(self.service.c.router, "_model", None))
        if self._shadow_id and self._shadow_id in self.changes:
            self.changes[self._shadow_id]["status"] = "evaluated"
        self.service.shadow = (change_id, router)
        self._shadow_id = change_id
        change["status"] = "shadow"
        change["shadow"] = {"started": self._clock(), "started_by": operator}
        await self._audit("change_shadow_started", {"change_id": change_id, "operator": operator})
        return change

    async def stop_shadow(self, operator: str) -> None:
        if self._shadow_id and self._shadow_id in self.changes and self.changes[self._shadow_id]["status"] == "shadow":
            self.changes[self._shadow_id]["status"] = "evaluated"
            await self._audit("change_shadow_stopped", {"change_id": self._shadow_id, "operator": operator})
        self.service.shadow = None
        self._shadow_id = None

    async def approve(self, change_id: str, approver: str, note: str) -> dict[str, Any]:
        change = self._get(change_id)
        if change["status"] not in ("evaluated", "shadow"):
            raise ChangeError(f"cannot approve a change with status {change['status']}", 409)
        if not change["gate"]["passed"]:
            raise ChangeError("the change did not pass its evaluation gate", 409)
        if self._cc.require_four_eyes and approver == change["proposer"]:
            raise ChangeError("four-eyes rule: a change must be approved by someone other than its proposer", 403)
        if change["base_version"] != self.active:
            change["status"] = "stale"
            raise ChangeError("the live configuration changed since this was evaluated; propose it again", 409)
        if self._shadow_id == change_id:
            self.service.shadow = None
            self._shadow_id = None
        self.applied = self.applied[: self.active] + [{"id": change_id, "ops": change["ops"], "title": change["title"],
                                                        "approver": approver, "proposer": change["proposer"], "at": self._clock()}]
        new_version = self.active + 1
        await self._activate(new_version)
        change.update(status="applied", approver=approver, decided_at=self._clock(), decision_note=note, applied_version=new_version)
        await self._audit("change_applied", {"change_id": change_id, "approver": approver, "proposer": change["proposer"],
                                             "note": note, "version": new_version})
        return change

    async def reject(self, change_id: str, operator: str, note: str) -> dict[str, Any]:
        change = self._get(change_id)
        if change["status"] in ("applied", "rejected"):
            raise ChangeError(f"change already {change['status']}", 409)
        if self._shadow_id == change_id:
            await self.stop_shadow(operator)
        change.update(status="rejected", approver=operator, decided_at=self._clock(), decision_note=note)
        await self._audit("change_rejected", {"change_id": change_id, "operator": operator, "note": note})
        return change

    async def rollback(self, version: int, operator: str, reason: str) -> dict[str, Any]:
        if not 0 <= version <= len(self.applied):
            raise ChangeError(f"version must be between 0 and {len(self.applied)}")
        if version == self.active:
            raise ChangeError("already at that version", 409)
        previous = self.active
        await self._activate(version)
        await self._audit("runtime_rolled_back", {"from_version": previous, "to_version": version, "operator": operator, "reason": reason})
        return self.versions()

    async def _activate(self, version: int) -> None:
        source, guards, routing = self.effective(version)
        catalogue = self.build(source, guards, routing)
        self.active = version
        self.service.apply_runtime(catalogue, guards, routing, version)
        await self.service.c.store.put_control(CONTROL_KEY, {
            "active": version, "applied": [{"id": c["id"], "ops": c["ops"], "title": c["title"], "approver": c["approver"],
                                            "proposer": c["proposer"], "at": c["at"]} for c in self.applied]})
        await self.evals.run(catalogue, guards, routing, label=f"baseline v{version}", runtime_version=version)

    async def refresh(self) -> None:
        """Pick up changes applied on another replica (called at most every 5 s from the turn path)."""
        now = time.monotonic()
        if now - self._last_refresh < 5:
            return
        self._last_refresh = now
        stored = await self.service.c.store.get_control(CONTROL_KEY)
        if not stored:
            return
        ids = [c["id"] for c in stored.get("applied", [])]
        if stored.get("active") == self.active and ids == [c["id"] for c in self.applied]:
            return
        self.applied = list(stored.get("applied", []))
        version = int(stored.get("active", 0))
        source, guards, routing = self.effective(version)
        self.active = version
        self.service.apply_runtime(self.build(source, guards, routing), guards, routing, version)

    async def preview(self, text: str, ops: list[dict[str, Any]] | None, disabled_intents: set[str]) -> dict[str, Any]:
        """Route one sentence with the live configuration and, optionally, with candidate operations (no eval, no state)."""
        source, guards, routing = self.effective()

        async def decide(router: Router) -> dict[str, Any]:
            d = await router.route(text, disabled_intents)
            return {"intent": d.intent.id if d.intent else None, "risk": d.intent.risk.value if d.intent else None,
                    "confidence": round(d.confidence, 3), "source": d.source, "clarify": d.needs_clarification,
                    "candidates": list(d.candidates)}

        live = await decide(self.service.c.router)
        result: dict[str, Any] = {"text": text, "live": live, "candidate": None}
        if ops:
            ops = [dict(op) for op in ops]
            for op in ops:
                self._validate_op(op, guards, routing, source)
            cand_source, cand_guards, cand_routing = apply_ops(source, guards, routing, ops)
            result["candidate"] = await decide(Router(self.build(cand_source, cand_guards, cand_routing), cand_routing, None))
        return result

    # ------------------------------------------------------------------ views

    @staticmethod
    def _summarise(ops: list[dict[str, Any]]) -> str:
        parts = [f"{op['target']}{'/' + op['id'] if op.get('id') else ''}.{op['field']}" for op in ops]
        return "Update " + ", ".join(parts[:3]) + (" …" if len(parts) > 3 else "")

    def versions(self) -> dict[str, Any]:
        rows = [{"version": 0, "title": "As deployed", "change_id": None, "approver": None, "proposer": None, "at": None}]
        for i, c in enumerate(self.applied, start=1):
            rows.append({"version": i, "title": c["title"], "change_id": c["id"], "approver": c["approver"], "proposer": c["proposer"], "at": c["at"]})
        return {"active": self.active, "versions": rows}

    def catalogue_view(self) -> dict[str, Any]:
        source, guards, routing = self.effective()
        intents = []
        for raw in source["intents"].get("intents") or []:
            intents.append({
                "id": raw["id"], "risk": raw["risk"], "description": raw.get("description", ""),
                "required_acr": raw.get("required_acr"), "patterns": raw.get("patterns", []),
                "clarification_prompt": raw.get("clarification_prompt", ""), "readback_template": raw.get("readback_template", ""),
                "steps": [{"id": s["id"], "agent": s["agent"], "skill": s["skill"], "mode": s.get("mode", "read"),
                           "optional": s.get("optional", False), "depends_on": s.get("depends_on", []),
                           "instruction": s.get("instruction", ""), "timeout_ms": s.get("timeout_ms")}
                          for s in raw.get("steps") or []],
            })
        agents = [{"name": a["name"], "skills": a.get("skills", []), "certified_in": a.get("certified_in", []),
                   "clearance": a.get("clearance", []), "writes_allowed": a.get("writes_allowed", False)}
                  for a in source["agents"].get("agents") or []]
        return {"version": self.active, "intents": intents, "agents": agents,
                "guards": {k: asdict(guards)[k] for k in ("injection_patterns", "prohibited_phrases", "pressure_phrases", "r2_disclaimer")},
                "routing": {"min_confidence": routing.min_confidence, "floors": {r: self.base_routing.min_confidence.get(r) for r in ("R2", "R3")}},
                "editable": [f"{t}.{f}" for (t, f) in ALLOWED]}

    def listing(self) -> list[dict[str, Any]]:
        for cid in list(self.changes):
            self._get(cid)
        return sorted(self.changes.values(), key=lambda c: -c["created"])
