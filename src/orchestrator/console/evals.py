"""Evals: a golden dataset run against a runtime configuration.

Four categories:

* routing: does the text reach the right intent (or a clarification)?
* safety: input guard, PII redaction and output guard behaviour.
* policy: allow/deny decisions from the local policy engine (kept in parity with the Rego policy).
* e2e: whole turns through an isolated orchestrator talking to the reference agents in-process.
  A case with ``turns`` replays a conversation in one session, checking every turn, so follow-up
  questions, clarifications and slot filling are covered as users experience them.

The runner takes the catalogue, guard and routing configuration to test, so
the same suite measures the live configuration and a proposed change. A change
may only be applied when its run has no regressions against the live baseline
and reaches ``command_center.change_min_pass_rate``.

End-to-end cases run offline against the reference (demo) agents, so they
test orchestration behaviour, not the quality of your production agents; add
live-agent evals in your integration environment.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..catalogue import Catalogue
from ..config import ConfigError, GuardConfig, KillSwitchConfig, OrchestratorConfig, RoutingConfig
from ..guards import InputGuard, OutputGuard, redact_pii
from ..models import RiskClass, TurnRequest, UserContext
from ..policy import LocalPolicyEngine, build_policy_input
from ..router import Router

CATEGORY = {"routing": "routing", "input_guard": "safety", "pii": "safety", "output_guard": "safety", "policy": "policy", "e2e": "e2e"}


@dataclass
class EvalResult:
    id: str
    kind: str
    category: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""
    text: str = ""


@dataclass
class EvalRun:
    id: str
    label: str
    started: float
    duration_ms: float
    runtime_version: int
    config_hash: str
    totals: dict[str, dict[str, int]]
    passed: int
    total: int
    results: list[EvalResult] = field(default_factory=list)
    change_id: str | None = None

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "started": self.started, "duration_ms": self.duration_ms,
                "runtime_version": self.runtime_version, "config_hash": self.config_hash, "totals": self.totals,
                "passed": self.passed, "total": self.total, "pass_rate": self.pass_rate, "change_id": self.change_id}

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "results": [asdict(r) for r in self.results]}


def compare(baseline: EvalRun | None, candidate: EvalRun) -> dict[str, Any]:
    if baseline is None:
        return {"regressions": [], "fixes": [], "baseline_run": None}
    base = {r.id: r.passed for r in baseline.results}
    regressions = [r.id for r in candidate.results if base.get(r.id) is True and not r.passed]
    fixes = [r.id for r in candidate.results if base.get(r.id) is False and r.passed]
    return {"regressions": regressions, "fixes": fixes, "baseline_run": baseline.id,
            "baseline_pass_rate": baseline.pass_rate, "candidate_pass_rate": candidate.pass_rate}


def config_hash(catalogue: Catalogue, guards: GuardConfig, routing: RoutingConfig) -> str:
    blob = json.dumps({"c": catalogue.source, "g": asdict(guards), "r": asdict(routing)}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_suite(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"evals file not found: {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") or []
    ids = [c.get("id") for c in cases]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ConfigError("every eval case needs a unique id")
    for c in cases:
        if c.get("kind") not in CATEGORY:
            raise ConfigError(f"eval case {c.get('id')}: unknown kind {c.get('kind')!r}")
        if c.get("turns") is not None and (c["kind"] != "e2e" or not all(isinstance(t, dict) and t.get("text") and
                                                                          t.get("expect_type") for t in c["turns"])):
            raise ConfigError(f"eval case {c.get('id')}: turns are for e2e cases and each needs text and expect_type")
    return cases


class EvalRunner:
    def __init__(self, config: OrchestratorConfig, cases: list[dict[str, Any]]) -> None:
        self._config = config
        self.cases = cases
        self.history: list[EvalRun] = []
        self.history_limit = 50

    # ------------------------------------------------------------------ public

    async def run(self, catalogue: Catalogue, guards: GuardConfig, routing: RoutingConfig, *, label: str,
                  runtime_version: int, change_id: str | None = None, record: bool = True) -> EvalRun:
        started = time.time()
        t0 = time.perf_counter()
        results: list[EvalResult] = []
        router = Router(catalogue, routing, None)
        in_guard = InputGuard(guards)
        out_guard = OutputGuard(guards)
        for case in self.cases:
            kind = case["kind"]
            try:
                if kind == "routing":
                    results.append(await self._routing(case, router))
                elif kind == "input_guard":
                    results.append(await self._input_guard(case, in_guard))
                elif kind == "pii":
                    results.append(self._pii(case))
                elif kind == "output_guard":
                    results.append(self._output_guard(case, out_guard))
                elif kind == "policy":
                    results.append(await self._policy(case, catalogue))
                elif kind == "e2e":
                    results.append(await self._e2e(case, catalogue, guards, routing))
            except Exception as exc:  # noqa: BLE001 - a broken case is a failed case, never a crashed run
                results.append(EvalResult(case["id"], kind, CATEGORY[kind], False, case.get("expect"), None, f"error: {type(exc).__name__}: {exc}", case.get("text", "")))
        totals: dict[str, dict[str, int]] = {}
        for r in results:
            t = totals.setdefault(r.category, {"passed": 0, "total": 0})
            t["total"] += 1
            t["passed"] += 1 if r.passed else 0
        run = EvalRun(
            id="ev-" + uuid.uuid4().hex[:8], label=label, started=started,
            duration_ms=round((time.perf_counter() - t0) * 1000, 1), runtime_version=runtime_version,
            config_hash=config_hash(catalogue, guards, routing), totals=totals,
            passed=sum(1 for r in results if r.passed), total=len(results), results=results, change_id=change_id,
        )
        if record:
            self.history.append(run)
            del self.history[:-self.history_limit]
        return run

    def latest(self, runtime_version: int | None = None, baseline_only: bool = True) -> EvalRun | None:
        for run in reversed(self.history):
            if baseline_only and run.change_id:
                continue
            if runtime_version is None or run.runtime_version == runtime_version:
                return run
        return None

    def get(self, run_id: str) -> EvalRun | None:
        return next((r for r in self.history if r.id == run_id), None)

    # ------------------------------------------------------------------ cases

    async def _routing(self, case: dict[str, Any], router: Router) -> EvalResult:
        decision = await router.route(case["text"], set(case.get("disabled_intents") or []))
        if decision.segments:
            actual = "+".join(intent.id for _, intent in decision.segments)
        elif decision.source == "disabled":
            actual = "disabled"
        elif decision.needs_clarification or decision.intent is None:
            actual = "clarify"
        else:
            actual = decision.intent.id
        expected = case["expect"]
        detail = f"source={decision.source} confidence={decision.confidence:.2f}"
        return EvalResult(case["id"], "routing", "routing", actual == expected, expected, actual, detail, case["text"])

    async def _input_guard(self, case: dict[str, Any], guard: InputGuard) -> EvalResult:
        verdict = await guard.check(case["text"])
        blocked = not verdict.allowed
        ok = blocked == bool(case["expect_blocked"])
        missing = [f for f in case.get("expect_flags") or [] if f not in verdict.flags]
        ok = ok and not missing
        return EvalResult(case["id"], "input_guard", "safety", ok, {"blocked": case["expect_blocked"], "flags": case.get("expect_flags", [])},
                          {"blocked": blocked, "flags": verdict.flags}, f"missing flags {missing}" if missing else "", case["text"])

    def _pii(self, case: dict[str, Any]) -> EvalResult:
        redacted, kinds = redact_pii(case["text"])
        missing = [k for k in case.get("expect_kinds") or [] if k not in kinds]
        leaked = [s for s in case.get("must_not_contain") or [] if s in redacted]
        unexpected = [k for k in kinds if k not in (case.get("expect_kinds") or [])] if case.get("exact", False) else []
        ok = not missing and not leaked and not unexpected
        detail = "; ".join(x for x in (f"missing {missing}" if missing else "", f"leaked {leaked}" if leaked else "",
                                         f"unexpected {unexpected}" if unexpected else "") if x)
        return EvalResult(case["id"], "pii", "safety", ok, case.get("expect_kinds", []), sorted(set(kinds)), detail, case["text"])

    def _output_guard(self, case: dict[str, Any], guard: OutputGuard) -> EvalResult:
        sources = ["src://eval"] if case.get("sources", True) else []
        verdict = guard.check(case["text"], RiskClass(case["risk"]), sources)
        ok = verdict.allowed == bool(case["expect_allowed"])
        return EvalResult(case["id"], "output_guard", "safety", ok, {"allowed": case["expect_allowed"]},
                          {"allowed": verdict.allowed, "flags": verdict.flags}, "", case["text"])

    async def _policy(self, case: dict[str, Any], catalogue: Catalogue) -> EvalResult:
        intent = catalogue.intents[case["intent"]]
        step = next(s for s in intent.steps if s.id == case["step"])
        inp = build_policy_input(
            environment=case.get("environment", self._config.service.environment), cell_id="eval",
            user=UserContext(subject="eval-user", acr=case.get("acr", "standard"), tenant="eval", channel=case.get("channel", "voice")),
            intent=intent, step=step, agent=catalogue.agent(step.agent), acr_levels=self._config.auth.acr_levels,
            kill=KillSwitchConfig(disabled_agents=list(case.get("disabled_agents") or [])),
            allowed_channels=self._config.policy.allowed_channels, phase=case.get("phase", "plan"),
            approval_valid=bool(case.get("approval", False)),
        )
        decision = await LocalPolicyEngine().decide(inp)
        ok = decision.allow == bool(case["expect_allow"])
        if ok and case.get("expect_reason"):
            ok = case["expect_reason"] in decision.reasons
        return EvalResult(case["id"], "policy", "policy", ok, {"allow": case["expect_allow"], "reason": case.get("expect_reason")},
                          {"allow": decision.allow, "reasons": decision.reasons})

    async def _e2e(self, case: dict[str, Any], catalogue: Catalogue, guards: GuardConfig, routing: RoutingConfig) -> EvalResult:
        from domain_agents.demo_agents import build_agents
        from domain_agents.local_transport import LocalAgentTransport

        from ..audit import InMemoryAuditSink
        from ..bootstrap import build_service
        from ..state import InMemorySessionStore

        cfg = copy.deepcopy(self._config)
        cfg.profile = "dev"
        cfg.service.environment = case.get("environment", "test")
        cfg.guards, cfg.routing = copy.deepcopy(guards), copy.deepcopy(routing)
        cfg.policy.engine, cfg.identity.mode, cfg.session.store, cfg.audit.sink = "local", "disabled", "memory", "memory"
        cfg.command_center.enabled = False
        cfg.workflows.enabled = True
        cfg.kill_switch = KillSwitchConfig(disabled_intents=list(case.get("disabled_intents") or []))

        class _Workflows:
            async def start_transaction(self, workflow_id: str, payload: dict[str, Any]) -> None: ...
            async def signal_approval(self, workflow_id: str, decision: dict[str, Any]) -> None: ...
            async def status(self, workflow_id: str) -> dict[str, Any]:
                return {"status": "running"}

        key_env = cfg.workflows.approval_signing_key_env
        service = build_service(
            cfg, catalogue=catalogue, transport=LocalAgentTransport(build_agents()), store=InMemorySessionStore(),
            audit_sink=InMemoryAuditSink(), workflows=_Workflows(), environ={key_env: secrets.token_urlsafe(40)},
        )
        await service.open_session(session_id="eval", user=UserContext("eval-user", case.get("acr", "standard"), "eval"),
                                   subject_token="", token_expires_at=0)
        turns = case.get("turns") or [case]
        problems: list[str] = []
        actual: list[dict[str, Any]] = []
        for i, turn in enumerate(turns, 1):
            response = await service.handle_turn(TurnRequest("eval", f"t{i}", turn["text"]))
            actual.append({"type": response.type.value, "intent": response.intent, "text": response.text[:200]})
            prefix = f"turn {i}: " if len(turns) > 1 else ""
            problems += [prefix + p for p in _turn_problems(turn, response)]
        expected: Any = {"type": case["expect_type"], "intent": case.get("expect_intent")} if "turns" not in case else [
            {"type": t["expect_type"], "intent": t.get("expect_intent")} for t in turns]
        text = case.get("text") or " / ".join(t["text"] for t in turns)
        return EvalResult(case["id"], "e2e", "e2e", not problems, expected, actual[0] if "turns" not in case else actual,
                          "; ".join(problems), text)


def _turn_problems(turn: dict[str, Any], response: Any) -> list[str]:
    problems = []
    if response.type.value != turn["expect_type"]:
        problems.append(f"type {response.type.value}")
    for needle in turn.get("must_contain") or []:
        if needle.lower() not in response.text.lower():
            problems.append(f"missing '{needle}'")
    for needle in turn.get("must_not_contain") or []:
        if needle.lower() in response.text.lower():
            problems.append(f"contains '{needle}'")
    if turn.get("expect_sources") and not response.sources:
        problems.append("no sources")
    if turn.get("expect_intent") and response.intent != turn["expect_intent"]:
        problems.append(f"intent {response.intent}")
    return problems
