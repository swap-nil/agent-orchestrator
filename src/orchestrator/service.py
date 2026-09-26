"""The orchestrator pipeline.

``handle_turn`` runs one user turn end to end:

    session lock -> input guard -> route -> admission -> plan -> validate
    -> plan-phase policy -> (execute reads | prepare transaction + approval)
    -> aggregate -> output guard -> audit -> response

Every decision is written to the audit ledger on the session's chain. Any
unexpected error produces a safe fallback answer, never a stack trace.
"""

from __future__ import annotations

import asyncio
import logging
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .admission import AdmissionController, Rejected
from .aggregator import aggregate
from .approvals import ApprovalService, WorkflowGateway, action_hash, verify_approval_token
from .audit import AuditLog
from .catalogue import Catalogue
from .config import KillSwitchConfig, OrchestratorConfig
from .executor import ExecutionContext, Executor
from .guards import InputGuard, OutputGuard
from .models import (
    Plan, ResponseType, RiskClass, StepMode, StepResult, StepSpec, TaskState, TurnRequest, TurnResponse, UserContext,
)
from .planner import Planner, build_layers
from .policy import PolicyDecision, PolicyEnforcementPoint, build_policy_input
from .router import Router
from .state import SessionState, SessionStore, StateError, TokenCipher
from .tracing import TraceContext, continue_or_start, span

log = logging.getLogger("orchestrator")


class _SafeDict(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return "?"


def render_readback(template: str, params: dict[str, Any]) -> str:
    """Format a read-back template, tolerating missing keys and rejecting attribute access."""
    fields = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    if any(("." in f or "[" in f) for f in fields):
        raise ValueError("read-back templates may only use plain field names")
    return template.format_map(_SafeDict({k: v for k, v in params.items() if isinstance(v, (str, int, float))}))


@dataclass
class Components:
    config: OrchestratorConfig
    catalogue: Catalogue
    router: Router
    planner: Planner
    pep: PolicyEnforcementPoint
    executor: Executor
    input_guard: InputGuard
    output_guard: OutputGuard
    audit: AuditLog
    store: SessionStore
    cipher: TokenCipher
    admission: AdmissionController
    approvals: ApprovalService | None = None
    workflows: WorkflowGateway | None = None
    approval_public_key: Any = None  # Ed25519 public key used to re-verify approval tokens


class OrchestratorService:
    def __init__(self, c: Components) -> None:
        self.c = c
        self.cfg = c.config
        self._flags_cache: tuple[float, KillSwitchConfig] | None = None
        # Command center hooks (all optional): shadow router for a change under evaluation,
        # and a refresher that picks up runtime configuration changes made on any replica.
        self.shadow: tuple[str, Router] | None = None
        self.runtime_refresher: Callable[[], Any] | None = None
        self.runtime_version = 0

    # ------------------------------------------------------------------ sessions

    async def open_session(
        self,
        *,
        session_id: str,
        user: UserContext,
        subject_token: str,
        token_expires_at: float,
        traceparent: str | None = None,
    ) -> None:
        if not session_id or len(session_id) > 128:
            raise ValueError("invalid session id")
        state = SessionState(
            session_id=session_id,
            subject=user.subject,
            tenant=user.tenant,
            acr=user.acr,
            channel=user.channel,
            locale=user.locale,
            entitlements=list(user.entitlements),
            token_blob=self.c.cipher.encrypt(subject_token) if subject_token else "",
            token_expires_at=token_expires_at,
        )
        async with self.c.store.lock(session_id):
            if await self.c.store.get(session_id) is not None:
                raise ValueError("session already exists")
            await self.c.store.put(state)
        trace = continue_or_start(traceparent)
        await self.c.audit.record(
            session_id, "session_opened",
            {"acr": user.acr, "channel": user.channel, "tenant": user.tenant},
            session_id=session_id, trace_id=trace.trace_id,
        )

    async def close_session(self, session_id: str) -> None:
        async with self.c.store.lock(session_id):
            state = await self.c.store.get(session_id)
            if state is None:
                return
            state.closed = True
            state.token_blob = ""
            await self.c.store.put(state)
        self.c.executor.forget_session(session_id)
        await self.c.audit.record(session_id, "session_closed", {"turns": state.turns}, session_id=session_id)

    # ------------------------------------------------------------------ kill switch

    async def kill_switch(self) -> KillSwitchConfig:
        now = time.monotonic()
        if self._flags_cache and self._flags_cache[0] > now:
            return self._flags_cache[1]
        base = self.cfg.kill_switch
        try:
            runtime = await self.c.store.get_flags()
        except Exception:  # noqa: BLE001 - keep serving with configured switches if the store blips
            runtime = {}
        merged = KillSwitchConfig(
            disabled_agents=sorted(set(base.disabled_agents) | set(runtime.get("disabled_agents", []))),
            disabled_intents=sorted(set(base.disabled_intents) | set(runtime.get("disabled_intents", []))),
            disabled_risk_classes=sorted(set(base.disabled_risk_classes) | set(runtime.get("disabled_risk_classes", []))),
        )
        self._flags_cache = (now + 5.0, merged)
        return merged

    async def set_runtime_flags(self, flags: dict[str, list[str]], actor: str, reason: str = "") -> dict[str, list[str]]:
        allowed = {"disabled_agents", "disabled_intents", "disabled_risk_classes"}
        clean = {k: sorted({str(v) for v in flags.get(k, [])}) for k in allowed}
        try:
            previous = await self.c.store.get_flags()
        except Exception:  # noqa: BLE001
            previous = {}
        await self.c.store.put_flags(clean)
        self._flags_cache = None
        await self.c.audit.record("control-plane", "kill_switch_changed", {
            "flags": clean, "previous": {k: sorted(previous.get(k, [])) for k in allowed}, "actor": actor, "reason": reason,
        })
        return clean

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _remember(state: SessionState, response: TurnResponse, keep: int = 20) -> None:
        state.recent_turns[response.turn_id] = response.to_dict()
        while len(state.recent_turns) > keep:
            state.recent_turns.pop(next(iter(state.recent_turns)))

    def _response(self, rtype: ResponseType, text: str, req: TurnRequest, trace: TraceContext, **kw: Any) -> TurnResponse:
        return TurnResponse(type=rtype, text=text, session_id=req.session_id, turn_id=req.turn_id, trace_id=trace.trace_id, **kw)

    def _user(self, state: SessionState, channel: str) -> UserContext:
        return UserContext(
            subject=state.subject, acr=state.acr, tenant=state.tenant,
            entitlements=tuple(state.entitlements), channel=channel, locale=state.locale,
        )

    def _policy_input(self, user: UserContext, plan: Plan, step: StepSpec, kill: KillSwitchConfig, phase: str, approval_valid: bool) -> dict[str, Any]:
        return build_policy_input(
            environment=self.cfg.service.environment,
            cell_id=self.cfg.service.cell_id,
            user=user,
            intent=plan.intent,
            step=step,
            agent=self.c.catalogue.agent(step.agent),
            acr_levels=self.cfg.auth.acr_levels,
            kill=kill,
            allowed_channels=self.cfg.policy.allowed_channels,
            phase=phase,
            approval_valid=approval_valid,
        )

    def _subject_token(self, state: SessionState) -> str:
        if not state.token_blob:
            return ""
        if state.token_expires_at and state.token_expires_at < time.time():
            return ""
        return self.c.cipher.decrypt(state.token_blob)

    # ------------------------------------------------------------------ turns

    async def handle_turn(self, req: TurnRequest) -> TurnResponse:
        trace = continue_or_start(req.traceparent)
        m = self.cfg.messages
        started = time.perf_counter()
        if self.runtime_refresher is not None:
            try:
                await self.runtime_refresher()
            except Exception:  # noqa: BLE001 - keep serving with the current runtime configuration
                log.warning("runtime configuration refresh failed")
        try:
            async with self.c.store.lock(req.session_id):
                with span("orchestrator.turn", trace, **{"orchestrator.session.id": req.session_id, "orchestrator.turn.id": req.turn_id}):
                    response = await self._handle_locked(req, trace)
                await self._completed(req, trace, response, started)
                state = await self.c.store.get(req.session_id)
                if state is not None and req.turn_id not in state.recent_turns and not response.meta.get("replayed"):
                    self._remember(state, response)
                    await self.c.store.put(state)
                return response
        except (Rejected, StateError):
            response = self._response(ResponseType.BUSY, m.busy, req, trace, reasons=["busy"])
            await self._completed(req, trace, response, started, best_effort=True)
            return response
        except Exception:  # noqa: BLE001 - never leak internals to the user
            log.exception("turn failed", extra={"session_id": req.session_id, "trace_id": trace.trace_id})
            try:
                await self.c.audit.record(req.session_id, "turn_error", {}, session_id=req.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)
            except Exception:  # noqa: BLE001
                log.error("audit unavailable while recording a turn error")
            response = self._response(ResponseType.HANDOVER, m.handover, req, trace, reasons=["internal_error"])
            await self._completed(req, trace, response, started, best_effort=True)
            return response

    async def _completed(self, req: TurnRequest, trace: TraceContext, response: TurnResponse, started: float, best_effort: bool = False) -> None:
        """One terminal event per turn, on every path: the basis of latency, outcome and safeguard metrics."""
        data = {
            "type": response.type.value, "intent": response.intent, "risk": response.meta.get("risk"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1), "partial": response.partial,
            "reasons": response.reasons, "sources": response.sources, "dropped": response.meta.get("dropped", 0),
            "degraded": response.meta.get("degraded", False), "replayed": response.meta.get("replayed", False),
            "channel": req.channel, "runtime_version": self.runtime_version,
        }
        try:
            await self.c.audit.record(req.session_id, "turn_completed", data, session_id=req.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)
        except Exception:
            if not best_effort:
                raise
            log.error("audit unavailable while recording turn completion")

    async def _handle_locked(self, req: TurnRequest, trace: TraceContext) -> TurnResponse:
        m = self.cfg.messages
        audit = self.c.audit
        ids = {"session_id": req.session_id, "turn_id": req.turn_id, "trace_id": trace.trace_id}

        state = await self.c.store.get(req.session_id)
        if state is None or state.closed:
            return self._response(ResponseType.REFUSED, m.session_invalid, req, trace, reasons=["no_session"])
        replay = state.recent_turns.get(req.turn_id)
        if replay is not None:
            await audit.record(req.session_id, "turn_replayed", {}, **ids)
            return TurnResponse(
                type=ResponseType(replay["type"]), text=replay["text"], session_id=req.session_id, turn_id=req.turn_id,
                trace_id=replay["trace_id"], intent=replay.get("intent"), sources=list(replay.get("sources", [])),
                approval=replay.get("approval"), partial=bool(replay.get("partial")), meta={"replayed": True},
            )
        if state.turns >= self.cfg.budgets.max_turns_per_session:
            return self._response(ResponseType.HANDOVER, m.handover, req, trace, reasons=["turn_limit"])
        user = self._user(state, req.channel)

        verdict = await self.c.input_guard.check(req.text)
        logged_text = verdict.redacted_for_log if self.cfg.guards.redact_pii_in_logs else verdict.text
        await audit.record(req.session_id, "turn_received", {"text": logged_text, "flags": verdict.flags, "channel": req.channel}, **ids)
        state.turns += 1
        if not verdict.allowed:
            await audit.record(req.session_id, "input_blocked", {"flags": verdict.flags}, **ids)
            await self.c.store.put(state)
            return self._response(ResponseType.REFUSED, m.blocked_input, req, trace, reasons=verdict.flags)

        kill = await self.kill_switch()
        routing = await self.c.router.route(verdict.text, set(kill.disabled_intents))
        risk = routing.intent.risk.value if routing.intent else None
        await audit.record(req.session_id, "routed", {
            "intent": routing.intent.id if routing.intent else None, "confidence": round(routing.confidence, 3),
            "source": routing.source, "clarify": routing.needs_clarification, "candidates": list(routing.candidates),
            "risk": risk, "threshold": self.cfg.routing.min_confidence.get(risk) if risk else None,
        }, **ids)
        await self._shadow_route(req, verdict.text, kill, routing, ids)

        if routing.source == "disabled" and routing.intent is not None:
            await self.c.store.put(state)
            text = m.transactions_unavailable if routing.intent.risk is RiskClass.R3 else m.refused
            return self._response(ResponseType.REFUSED, text, req, trace, intent=routing.intent.id, reasons=["intent_disabled"], meta={"risk": risk})

        if routing.needs_clarification or routing.intent is None:
            state.clarification_rounds += 1
            await self.c.store.put(state)
            if state.clarification_rounds > self.cfg.routing.max_clarification_rounds:
                state.clarification_rounds = 0
                await self.c.store.put(state)
                return self._response(ResponseType.HANDOVER, m.handover, req, trace, reasons=["clarification_limit"])
            prompt = routing.intent.clarification_prompt if routing.intent and routing.intent.clarification_prompt else m.clarify_default
            return self._response(ResponseType.CLARIFY, prompt, req, trace, intent=routing.intent.id if routing.intent else None, meta={"risk": risk})
        state.clarification_rounds = 0
        intent = routing.intent

        priority = intent.risk is RiskClass.R3
        with self.c.admission.admit(priority=priority) as adm:
            meta = {"risk": intent.risk.value, "degraded": adm.degraded}
            plan = self.c.planner.build(intent, skip_optional=adm.degraded, unavailable_agents=set(kill.disabled_agents))
            check = self.c.planner.validate(plan, user, kill, state.cost_used)
            kept = {st.id for st in plan.steps}
            await audit.record(req.session_id, "planned", {
                "intent": intent.id, "risk": intent.risk.value, "degraded": adm.degraded,
                "steps": [
                    {"id": st.id, "agent": st.agent, "skill": st.skill, "mode": st.mode.value, "optional": st.optional,
                     "depends_on": list(st.depends_on), "layer": li}
                    for li, layer in enumerate(plan.layers) for st in layer
                ],
                "skipped_optional": [st.id for st in intent.steps if st.id not in kept],
                "cost_units": plan.cost_units, "valid": check.ok, "reasons": check.reasons,
            }, **ids)
            if not check.ok:
                await audit.record(req.session_id, "plan_rejected", {"intent": intent.id, "reasons": check.reasons}, **ids)
                await self.c.store.put(state)
                return self._response(ResponseType.REFUSED, m.refused, req, trace, intent=intent.id, reasons=check.reasons, meta=meta)

            decisions = []
            denied = None
            for step in plan.steps:
                decision = await self.c.pep.check(self._policy_input(user, plan, step, kill, "plan", False))
                decisions.append({"step": step.id, "agent": step.agent, "allow": decision.allow, "reasons": decision.reasons,
                                  "decision_id": decision.decision_id, "engine": decision.engine, "cached": decision.cached})
                if not decision.allow:
                    denied = (step, decision)
                    break
            await audit.record(req.session_id, "policy_checked", {"phase": "plan", "intent": intent.id, "decisions": decisions}, **ids)
            if denied is not None:
                step, decision = denied
                await audit.record(req.session_id, "policy_denied", {
                    "intent": intent.id, "step": step.id, "reasons": decision.reasons,
                    "decision_id": decision.decision_id, "phase": "plan",
                }, **ids)
                await self.c.store.put(state)
                return self._response(ResponseType.REFUSED, m.refused, req, trace, intent=intent.id, reasons=decision.reasons, meta=meta)

            if plan.write_steps:
                response = await self._prepare_transaction(req, trace, state, user, plan, kill)
            else:
                response = await self._execute_reads(req, trace, state, user, plan, kill)
            skipped = [st.id for st in intent.steps if st.id not in kept]
            if skipped and response.type is ResponseType.ANSWER and not response.partial:
                response.partial = True
                if self.cfg.messages.partial_suffix and not response.text.endswith(self.cfg.messages.partial_suffix):
                    response.text = f"{response.text} {self.cfg.messages.partial_suffix}"
            response.meta = {**meta, **response.meta}

        await self.c.store.put(state)
        return response

    async def _shadow_route(self, req: TurnRequest, text: str, kill: KillSwitchConfig, live: Any, ids: dict[str, str]) -> None:
        """Route the same text with a candidate router and record agreement. Never affects the answer."""
        shadow = self.shadow
        if shadow is None:
            return
        change_id, router = shadow
        try:
            candidate = await router.route(text, set(kill.disabled_intents))
        except Exception:  # noqa: BLE001 - shadow failures are contained
            return
        live_intent = live.intent.id if live.intent else None
        cand_intent = candidate.intent.id if candidate.intent else None
        agrees = live_intent == cand_intent and live.needs_clarification == candidate.needs_clarification
        await self.c.audit.record(req.session_id, "shadow_routed", {
            "change_id": change_id, "live": live_intent, "candidate": cand_intent,
            "live_clarify": live.needs_clarification, "candidate_clarify": candidate.needs_clarification, "agrees": agrees,
        }, **ids)

    async def _run(
        self, plan: Plan, state: SessionState, user: UserContext, kill: KillSwitchConfig, req: TurnRequest, trace: TraceContext,
    ):  # type: ignore[no-untyped-def]
        loop = asyncio.get_running_loop()
        ctx = ExecutionContext(
            session_id=state.session_id, turn_id=req.turn_id, tenant=state.tenant,
            subject_token=self._subject_token(state), trace=trace,
            deadline=loop.time() + self.cfg.budgets.turn_deadline_ms / 1000, locale=state.locale,
        )

        async def policy_check(step: StepSpec) -> PolicyDecision:
            return await self.c.pep.check(self._policy_input(user, plan, step, kill, "execute", False))

        async def on_step(result: StepResult, decision: PolicyDecision | None) -> None:
            await self.c.audit.record(state.session_id, "step_result", {
                "step": result.step_id, "agent": result.agent, "state": result.state.value, "phase": "execute",
                "skill": next((st.skill for st in plan.steps if st.id == result.step_id), None),
                "skipped": result.skipped,
                "task_id": result.task_id, "attempts": result.attempts, "latency_ms": round(result.latency_ms, 1),
                "error": result.error, "decision_id": decision.decision_id if decision else None,
                "policy_reasons": decision.reasons if decision and not decision.allow else [],
            }, session_id=state.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)

        outcome = await self.c.executor.run(plan, ctx, policy_check, on_step)
        state.cost_used += plan.cost_units
        return outcome

    async def _execute_reads(
        self, req: TurnRequest, trace: TraceContext, state: SessionState, user: UserContext, plan: Plan, kill: KillSwitchConfig,
    ) -> TurnResponse:
        m = self.cfg.messages
        intent = plan.intent
        outcome = await self._run(plan, state, user, kill, req, trace)
        if not outcome.success:
            denied = any(r.state is TaskState.REJECTED for r in outcome.results.values())
            if denied:
                return self._response(ResponseType.REFUSED, m.refused, req, trace, intent=intent.id, reasons=["policy_denied"])
            rtype = ResponseType.HANDOVER if intent.risk.rank >= RiskClass.R2.rank else ResponseType.ANSWER
            return self._response(rtype, m.handover if rtype is ResponseType.HANDOVER else m.failure, req, trace, intent=intent.id, reasons=["execution_failed"])

        agg = aggregate(plan, outcome.results, self.cfg.guards.response_allowed_classifications)
        if not agg.text:
            return self._response(ResponseType.ANSWER, m.failure, req, trace, intent=intent.id, reasons=["empty_answer"], meta={"dropped": agg.dropped})
        out = self.c.output_guard.check(agg.text, intent.risk, agg.sources)
        await self.c.audit.record(req.session_id, "output_checked", {
            "intent": intent.id, "allowed": out.allowed, "flags": out.flags, "sources": agg.sources, "dropped": agg.dropped,
        }, session_id=req.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)
        if not out.allowed:
            await self.c.audit.record(req.session_id, "output_blocked", {"flags": out.flags, "intent": intent.id},
                                      session_id=req.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)
            return self._response(ResponseType.HANDOVER, m.handover, req, trace, intent=intent.id, reasons=out.flags, meta={"dropped": agg.dropped})
        text = out.text
        partial = outcome.partial or agg.dropped > 0
        if partial and m.partial_suffix:
            text = f"{text} {m.partial_suffix}"
        return self._response(ResponseType.ANSWER, text, req, trace, intent=intent.id, sources=agg.sources, partial=partial,
                              meta={"dropped": agg.dropped})

    async def _prepare_transaction(
        self, req: TurnRequest, trace: TraceContext, state: SessionState, user: UserContext, plan: Plan, kill: KillSwitchConfig,
    ) -> TurnResponse:
        m = self.cfg.messages
        intent = plan.intent
        if self.c.workflows is None or self.c.approvals is None:
            return self._response(ResponseType.REFUSED, m.transactions_unavailable, req, trace, intent=intent.id, reasons=["workflows_disabled"])

        read_steps = [s for s in plan.steps if s.mode is StepMode.READ]
        write_steps = plan.write_steps
        read_ids = {s.id for s in read_steps}
        for w in write_steps:
            if not set(w.depends_on) <= read_ids:
                return self._response(ResponseType.REFUSED, m.refused, req, trace, intent=intent.id, reasons=["write_depends_on_write"])
        read_plan = Plan(intent=intent, steps=read_steps, layers=build_layers(read_steps)) if read_steps else None

        params: dict[str, Any] = {}
        if read_plan is not None:
            outcome = await self._run(read_plan, state, user, kill, req, trace)
            if not outcome.success:
                return self._response(ResponseType.HANDOVER, m.handover, req, trace, intent=intent.id, reasons=["prepare_failed"])
            for w in write_steps:
                for dep in w.depends_on:
                    for artifact in outcome.results[dep].artifacts:
                        action_part = artifact.data.get("action")
                        if isinstance(action_part, dict):
                            params.update(action_part)
        if not params:
            return self._response(ResponseType.HANDOVER, m.handover, req, trace, intent=intent.id, reasons=["no_action_prepared"])

        action = {"intent": intent.id, "session_id": state.session_id, "tenant": state.tenant, "params": params}
        workflow_id = f"txn-{state.session_id}-{req.turn_id}"
        ticket = await self.c.approvals.create(state.session_id, workflow_id, action, summary=intent.description)
        readback = render_readback(intent.readback_template, params)
        payload = {
            "workflow_id": workflow_id,
            "session_id": state.session_id,
            "turn_id": req.turn_id,
            "intent_id": intent.id,
            "write_step_ids": [w.id for w in write_steps],
            "action": action,
            "action_hash": ticket.action_hash,
            "approval_id": ticket.approval_id,
            "traceparent": trace.traceparent,
            "approval_timeout_s": self.cfg.workflows.approval_timeout_s,
        }
        await self.c.workflows.start_transaction(workflow_id, payload)
        state.pending_workflows = (state.pending_workflows + [workflow_id])[-20:]
        await self.c.audit.record(state.session_id, "approval_requested", {
            "workflow_id": workflow_id, "approval_id": ticket.approval_id, "action_hash": ticket.action_hash,
            "intent": intent.id,
        }, session_id=state.session_id, turn_id=req.turn_id, trace_id=trace.trace_id)
        return self._response(
            ResponseType.APPROVAL_REQUIRED, f"{readback} {m.approval_prompt}".strip(), req, trace, intent=intent.id,
            approval={
                "approval_id": ticket.approval_id, "workflow_id": workflow_id, "action_hash": ticket.action_hash,
                "action": action, "expires_at": int(ticket.expires_at), "required_acr": ticket.required_acr,
            },
        )

    # ------------------------------------------------------------------ approvals

    async def decide_approval(
        self, approval_id: str, *, approve: bool, subject: str, acr: str, presented_action_hash: str,
        traceparent: str | None = None, user_token: str | None = None, token_expires_at: float = 0.0,
    ) -> dict[str, Any]:
        if self.c.approvals is None or self.c.workflows is None:
            return {"accepted": False, "reasons": ["workflows_disabled"]}
        trace = continue_or_start(traceparent)
        outcome = await self.c.approvals.decide(
            approval_id, approve=approve, subject=subject, acr=acr, presented_action_hash=presented_action_hash,
        )
        ticket = outcome.ticket
        chain = ticket.workflow_id if ticket else "approvals"
        await self.c.audit.record(chain, "approval_decided", {
            "approval_id": approval_id, "approved": outcome.approved, "reasons": outcome.reasons, "acr": acr,
        }, session_id=ticket.session_id if ticket else "", trace_id=trace.trace_id)
        if ticket is not None and outcome.approved and user_token:
            # The write runs on behalf of the user with the fresh step-up token, not the session's original one.
            async with self.c.store.lock(ticket.session_id):
                session = await self.c.store.get(ticket.session_id)
                if session is not None:
                    session.approval_tokens[ticket.workflow_id] = {
                        "blob": self.c.cipher.encrypt(user_token), "expires_at": token_expires_at,
                    }
                    await self.c.store.put(session)
        if ticket is not None:
            await self.c.audit.record(ticket.session_id, "approval_decided", {
                "workflow_id": ticket.workflow_id, "approved": outcome.approved, "reasons": outcome.reasons,
            }, session_id=ticket.session_id, trace_id=trace.trace_id)
        if ticket is not None and (outcome.approved or outcome.declined):
            await self.c.workflows.signal_approval(ticket.workflow_id, {
                "approved": outcome.approved, "approval_token": outcome.token, "traceparent": trace.traceparent,
            })
        return {"accepted": outcome.approved, "declined": outcome.declined, "reasons": outcome.reasons,
                "workflow_id": ticket.workflow_id if ticket else None}

    async def execute_approved_write(self, payload: dict[str, Any], approval_token: str) -> dict[str, Any]:
        """Called by the Temporal activity once the user has approved. Idempotent per workflow."""
        cfg = self.cfg
        if self.c.approval_public_key is None:
            raise RuntimeError("approval signing key not configured")
        if not verify_approval_token(self.c.approval_public_key, approval_token, payload["action_hash"]):
            raise PermissionError("approval token invalid for this action")
        if action_hash(payload["action"]) != payload["action_hash"]:
            raise PermissionError("action was modified after approval")
        intent = self.c.catalogue.intent(payload["intent_id"])
        if intent is None:
            raise LookupError("intent no longer in catalogue")
        state = await self.c.store.get(payload["session_id"])
        if state is None:
            raise LookupError("session expired before execution")
        user = self._user(state, state.channel)
        kill = await self.kill_switch()
        subject_token = self._subject_token(state)
        bound = state.approval_tokens.get(payload["workflow_id"])
        if bound and (not bound.get("expires_at") or bound["expires_at"] > time.time()):
            subject_token = self.c.cipher.decrypt(bound["blob"])
        writes = [replace(s, depends_on=()) for s in intent.steps if s.id in set(payload["write_step_ids"])]
        plan = Plan(intent=intent, steps=writes, layers=build_layers(writes))
        trace = continue_or_start(payload.get("traceparent"))
        loop = asyncio.get_running_loop()
        ctx = ExecutionContext(
            session_id=state.session_id, turn_id=payload["turn_id"], tenant=state.tenant,
            subject_token=subject_token, trace=trace,
            # Writes run outside the voice turn: give each its configured timeout plus retry headroom.
            deadline=loop.time() + (max((s.timeout_ms or cfg.execution.default_step_timeout_ms) for s in writes) + 1000) / 1000,
            locale=state.locale, approval_token=approval_token,
            extra_data={"action": payload["action"]["params"], "actionHash": payload["action_hash"]},
        )

        async def policy_check(step: StepSpec) -> PolicyDecision:
            return await self.c.pep.check(self._policy_input(user, plan, step, kill, "execute", True))

        async def on_step(result: StepResult, decision: PolicyDecision | None) -> None:
            await self.c.audit.record(payload["workflow_id"], "write_step_result", {
                "step": result.step_id, "agent": result.agent, "state": result.state.value, "task_id": result.task_id,
                "error": result.error, "decision_id": decision.decision_id if decision else None,
            }, session_id=state.session_id, turn_id=payload["turn_id"], trace_id=trace.trace_id)

        outcome = await self.c.executor.run(plan, ctx, policy_check, on_step, quorum="all")
        refs = [a.data.get("reference") for r in outcome.results.values() for a in r.artifacts if a.data.get("reference")]
        texts = [a.text for r in outcome.results.values() for a in r.artifacts if a.text]
        return {"success": outcome.success, "references": refs, "text": " ".join(texts)}

    async def workflow_status(self, workflow_id: str, session_id: str) -> dict[str, Any]:
        if self.c.workflows is None:
            return {"status": "unavailable"}
        state = await self.c.store.get(session_id)
        if state is None or workflow_id not in state.pending_workflows:
            return {"status": "not_found"}
        return await self.c.workflows.status(workflow_id)

    def apply_runtime(self, catalogue: Catalogue, guards: Any, routing: Any, version: int) -> None:
        """Hot-swap the behavioural configuration (patterns, instructions, prompts, guard lists, thresholds).

        Runtime changes never alter structure (risk classes, steps, agents, writes), so a turn
        that straddles the swap still sees one consistent plan shape.
        """
        from .guards import InputGuard, OutputGuard
        from .planner import Planner

        cfg = self.cfg
        cfg.guards = guards
        cfg.routing = routing
        classifier = getattr(self.c.router, "_model", None)
        external = getattr(self.c.input_guard, "_classifier", None)
        self.c.catalogue = catalogue
        self.c.router = Router(catalogue, routing, classifier)
        self.c.planner = Planner(catalogue, cfg.budgets, cfg.auth.acr_levels, cfg.service.environment)
        self.c.input_guard = InputGuard(guards, external)
        self.c.output_guard = OutputGuard(guards)
        self.c.executor.set_catalogue(catalogue)
        self.runtime_version = version

    async def ready(self) -> bool:
        return await self.c.store.ping()
