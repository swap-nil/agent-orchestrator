"""Executes validated plans against domain agents over A2A.

* Steps in one layer run in parallel; layers run in dependency order.
* One deadline covers the whole turn. Each call gets the smaller of its own
  timeout and the time left, and the deadline travels to the agent in metadata.
* Only read steps are retried, and only for retryable failures.
* A circuit breaker per agent stops hammering an agent that is failing.
* Every step is checked by the policy enforcement point immediately before it
  runs, with the exact step, agent and approval state.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .a2a import A2AClient, A2AError
from .catalogue import Catalogue
from .config import ExecutionConfig
from .identity import TokenExchangeError, TokenExchanger
from .models import Plan, StepMode, StepResult, StepSpec, TaskState
from .policy import PolicyDecision
from .resilience import CircuitBreakers, backoff_delay_s
from .tracing import TraceContext, span

PolicyCheck = Callable[[StepSpec], Awaitable[PolicyDecision]]
StepHook = Callable[[StepResult, PolicyDecision | None], Awaitable[None]]


@dataclass
class ExecutionContext:
    session_id: str
    turn_id: str
    tenant: str
    subject_token: str
    trace: TraceContext
    deadline: float  # event-loop time
    locale: str = "en-CH"
    approval_token: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)
    # PII-redacted user text, sent only to steps with include_query (public R0 steps).
    query: str = ""


@dataclass
class ExecutionOutcome:
    results: dict[str, StepResult]
    success: bool
    partial: bool


def idempotency_key(session_id: str, turn_id: str, step_id: str) -> str:
    return hashlib.sha256(f"{session_id}:{turn_id}:{step_id}".encode()).hexdigest()[:32]


class Executor:
    def __init__(
        self,
        config: ExecutionConfig,
        catalogue: Catalogue,
        a2a: A2AClient,
        tokens: TokenExchanger,
        breakers: CircuitBreakers,
    ) -> None:
        self._config = config
        self._catalogue = catalogue
        self._a2a = a2a
        self._tokens = tokens
        self._breakers = breakers

    def forget_session(self, session_id: str) -> None:
        self._tokens.forget_session(session_id)

    def set_catalogue(self, catalogue: Catalogue) -> None:
        self._catalogue = catalogue

    @property
    def breakers(self) -> CircuitBreakers:
        return self._breakers

    async def run(
        self,
        plan: Plan,
        ctx: ExecutionContext,
        policy_check: PolicyCheck,
        on_step: StepHook | None = None,
        quorum: str | None = None,
    ) -> ExecutionOutcome:
        results: dict[str, StepResult] = {}
        for layer in plan.layers:
            coros = [self._run_step(step, ctx, results, policy_check, on_step) for step in layer]
            for result in await asyncio.gather(*coros):
                results[result.step_id] = result
        return self._evaluate(plan, results, quorum or plan.intent.quorum or self._config.default_quorum)

    @staticmethod
    def _evaluate(plan: Plan, results: dict[str, StepResult], quorum: str) -> ExecutionOutcome:
        required = [s for s in plan.steps if not s.optional]
        ok_required = [s for s in required if results.get(s.id) and results[s.id].ok]
        if quorum == "any":
            success = bool(ok_required) or (not required and any(r.ok for r in results.values()))
        elif quorum == "majority":
            success = len(ok_required) * 2 > len(required) if required else any(r.ok for r in results.values())
        else:
            success = len(ok_required) == len(required)
        partial = success and any(not r.ok for r in results.values())
        return ExecutionOutcome(results, success, partial)

    async def _run_step(
        self,
        step: StepSpec,
        ctx: ExecutionContext,
        prior: dict[str, StepResult],
        policy_check: PolicyCheck,
        on_step: StepHook | None,
    ) -> StepResult:
        failed_deps = [d for d in step.depends_on if not (prior.get(d) and prior[d].ok)]
        if failed_deps:
            result = StepResult(step.id, step.agent, TaskState.CANCELED, error=f"dependency failed: {failed_deps}", skipped=True, attempts=0)
            if on_step:
                await on_step(result, None)
            return result

        decision = await policy_check(step)
        if not decision.allow:
            result = StepResult(step.id, step.agent, TaskState.REJECTED, error="policy denied", attempts=0)
            if on_step:
                await on_step(result, decision)
            return result

        result = await self._call_with_retries(step, ctx, prior, decision)
        if on_step:
            await on_step(result, decision)
        return result

    async def _call_with_retries(
        self, step: StepSpec, ctx: ExecutionContext, prior: dict[str, StepResult], decision: PolicyDecision
    ) -> StepResult:
        loop = asyncio.get_running_loop()
        agent = self._catalogue.agent(step.agent)
        assert agent is not None  # plan validation guarantees this
        max_attempts = 1 + (self._config.read_retries if step.mode is StepMode.READ else 0)
        started = time.perf_counter()
        last_error = "not attempted"
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            remaining = ctx.deadline - loop.time()
            if remaining <= 0.05:
                last_error = "turn deadline exceeded"
                break
            if not self._breakers.allow(agent.name):
                last_error = "circuit open"
                break
            try:
                token = await self._tokens.token_for(ctx.session_id, ctx.subject_token, agent.audience, step.skill)
            except TokenExchangeError as exc:
                last_error = f"delegation failed: {exc}"
                break
            timeout_s = min((step.timeout_ms or self._config.default_step_timeout_ms) / 1000, remaining)
            child = ctx.trace.child()
            inputs = {sid: [a.data for a in r.artifacts] for sid, r in prior.items() if sid in step.depends_on and r.ok}
            metadata = {
                "idempotencyKey": idempotency_key(ctx.session_id, ctx.turn_id, step.id),
                "traceparent": child.traceparent,
                "deadlineEpochMs": int((time.time() + remaining) * 1000),
                "policyDecisionId": decision.decision_id,
                "tenant": ctx.tenant,
                "dataClasses": list(step.data_classes),
                "skill": step.skill,
            }
            if step.mode is StepMode.WRITE and ctx.approval_token:
                metadata["approvalToken"] = ctx.approval_token
            data = {"skill": step.skill, "inputs": inputs, "locale": ctx.locale, **ctx.extra_data}
            if step.include_query and ctx.query:
                data["query"] = ctx.query
            try:
                with span(f"invoke_agent {agent.name}", child, **{
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": agent.name,
                    "orchestrator.step.id": step.id,
                    "orchestrator.skill": step.skill,
                    "orchestrator.session.id": ctx.session_id,
                }):
                    outcome = await asyncio.wait_for(
                        self._a2a.send(
                            agent.name,
                            context_id=ctx.session_id,
                            instruction=step.instruction,
                            data=data,
                            metadata=metadata,
                            traceparent=child.traceparent,
                            bearer=token.access_token if token else None,
                            timeout_s=timeout_s,
                        ),
                        timeout=timeout_s + 0.05,
                    )
            except (A2AError, asyncio.TimeoutError) as exc:
                retryable = isinstance(exc, asyncio.TimeoutError) or getattr(exc, "retryable", False)
                last_error = str(exc) or type(exc).__name__
                self._breakers.record_failure(agent.name)
                if retryable and attempt < max_attempts:
                    delay = backoff_delay_s(attempt, self._config.retry_base_delay_ms, self._config.retry_max_delay_ms)
                    if ctx.deadline - loop.time() > delay + 0.05:
                        await asyncio.sleep(delay)
                        continue
                break
            self._breakers.record_success(agent.name)
            error = None if outcome.state is TaskState.COMPLETED else (outcome.status_text or outcome.state.value)
            return StepResult(
                step.id, agent.name, outcome.state, outcome.artifacts, outcome.task_id, error,
                (time.perf_counter() - started) * 1000, attempt,
            )
        return StepResult(
            step.id, agent.name, TaskState.FAILED, error=last_error,
            latency_ms=(time.perf_counter() - started) * 1000, attempts=attempt,
        )
