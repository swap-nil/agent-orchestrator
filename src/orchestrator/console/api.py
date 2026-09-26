"""Command center API: one route table, one permission model, any HTTP server.

``ConsoleAPI.dispatch`` maps (method, path, query, body, headers) to
(status, JSON body). The FastAPI app and the development server are thin
wrappers around it, so both enforce exactly the same roles, reasons and
four-eyes rules. ``ConsoleAPI.stream`` yields decision events for
server-sent events.

Everything an operator does is written to the ``control-plane`` audit chain
with their identity and reason.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from ..api.security import AuthError
from ..audit import verify_chain
from ..service import OrchestratorService
from .alerts import AlertEngine
from .changes import ChangeError, RuntimeConfigManager
from .evals import EvalRunner
from .events import DecisionEvent
from .inspector import inspect_turn, session_turns
from .operators import Operator, OperatorAuth
from .telemetry import TelemetryAggregator

Handler = Callable[..., Awaitable[tuple[int, Any]]]
PREFIX = "/admin/cc"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    role: str


class ConsoleAPI:
    def __init__(
        self,
        service: OrchestratorService,
        telemetry: TelemetryAggregator,
        alerts: AlertEngine,
        evals: EvalRunner,
        changes: RuntimeConfigManager,
        auth: OperatorAuth,
        chaos: Any = None,
    ) -> None:
        self.service = service
        self.cfg = service.cfg
        self.cc = service.cfg.command_center
        self.telemetry = telemetry
        self.alerts = alerts
        self.evals = evals
        self.changes = changes
        self.auth = auth
        self.chaos = chaos  # dev server only: fault injection into the local agents
        self.bus = service.c.audit.bus
        self.started = time.time()
        self.routes: list[Route] = []
        r = self._route
        r("GET", "/me", self.me, "viewer")
        r("GET", "/overview", self.overview, "viewer")
        r("GET", "/timeseries", self.timeseries, "viewer")
        r("GET", "/events", self.events, "viewer")
        r("GET", "/sessions", self.sessions, "viewer")
        r("GET", "/sessions/(?P<sid>[^/]+)", self.session, "viewer")
        r("GET", "/sessions/(?P<sid>[^/]+)/turns/(?P<tid>[^/]+)", self.turn, "viewer")
        r("POST", "/sessions/(?P<sid>[^/]+)/terminate", self.terminate, "operator")
        r("GET", "/kill-switch", self.get_kill_switch, "viewer")
        r("PUT", "/kill-switch", self.put_kill_switch, "operator")
        r("POST", "/breakers/(?P<agent>[^/]+)/reset", self.reset_breaker, "operator")
        r("GET", "/alerts", self.get_alerts, "viewer")
        r("POST", "/alerts/(?P<key>[^/]+)/ack", self.ack_alert, "operator")
        r("GET", "/evals", self.get_evals, "viewer")
        r("POST", "/evals/run", self.run_evals, "operator")
        r("GET", "/evals/(?P<rid>[^/]+)", self.get_eval, "viewer")
        r("GET", "/catalogue", self.catalogue, "viewer")
        r("GET", "/changes", self.list_changes, "viewer")
        r("POST", "/changes", self.propose, "operator")
        r("GET", "/changes/(?P<cid>[^/]+)", self.get_change, "viewer")
        r("POST", "/changes/(?P<cid>[^/]+)/shadow", self.shadow, "operator")
        r("POST", "/shadow/stop", self.stop_shadow, "operator")
        r("POST", "/changes/(?P<cid>[^/]+)/approve", self.approve, "approver")
        r("POST", "/changes/(?P<cid>[^/]+)/reject", self.reject, "approver")
        r("POST", "/preview", self.preview, "viewer")
        r("GET", "/versions", self.versions, "viewer")
        r("POST", "/versions/rollback", self.rollback, "operator")
        r("GET", "/audit/control", self.control_audit, "viewer")
        r("GET", "/audit/(?P<chain>[^/]+)/verify", self.verify, "viewer")
        r("GET", "/chaos", self.get_chaos, "viewer")
        r("PUT", "/chaos", self.put_chaos, "operator")

    def _route(self, method: str, path: str, handler: Handler, role: str) -> None:
        self.routes.append(Route(method, re.compile("^" + path + "$"), handler, role))

    # ------------------------------------------------------------------ dispatch

    async def dispatch(self, method: str, path: str, query: dict[str, str], body: Any, headers: dict[str, str]) -> tuple[int, Any]:
        if not self.cc.enabled:
            return 404, {"error": "command center disabled"}
        if path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        try:
            operator = self.auth.resolve(headers)
        except AuthError as exc:
            return exc.status, {"error": str(exc)}
        allowed_methods = []
        for route in self.routes:
            match = route.pattern.match(path)
            if not match:
                continue
            if route.method != method:
                allowed_methods.append(route.method)
                continue
            if not operator.has(route.role):
                return 403, {"error": f"requires the {route.role} role"}
            try:
                return await route.handler(operator, query or {}, body if isinstance(body, dict) else {}, **match.groupdict())
            except ApiError as exc:
                return exc.status, {"error": str(exc)}
            except ChangeError as exc:
                return exc.status, {"error": str(exc)}
        if allowed_methods:
            return 405, {"error": "method not allowed"}
        return 404, {"error": "not found"}

    async def stream(self, query: dict[str, str], headers: dict[str, str]) -> tuple[Operator, AsyncIterator[dict[str, Any]]]:
        operator = self.auth.resolve(headers)
        if not operator.has("viewer"):
            raise AuthError("requires the viewer role", 403)
        session = query.get("session")
        resume = query.get("after") or {k.lower(): v for k, v in headers.items()}.get("last-event-id")
        if resume:
            after = int(resume)
        else:
            # Pin the position now: events published before the first read are replayed from the buffer, not lost.
            latest = self.bus.recent(1)
            after = latest[-1].seq if latest else 0

        async def gen() -> AsyncIterator[dict[str, Any]]:
            async for event in self.bus.subscribe(after_seq=after):
                if session and event.session_id != session:
                    continue
                yield self.project(event, operator)

        return operator, gen()

    # ------------------------------------------------------------------ helpers

    def _may_see_text(self, operator: Operator) -> bool:
        return self.cc.show_utterances and "investigator" in operator.roles

    def project(self, event: DecisionEvent, operator: Operator) -> dict[str, Any]:
        item = event.to_dict()
        text = item["data"].get("text")
        if isinstance(text, str) and not self._may_see_text(operator):
            item["data"] = {**item["data"], "text": f"[hidden: {len(text)} characters]"}
        return item

    def _reason(self, body: dict[str, Any], key: str = "reason") -> str:
        reason = str(body.get(key) or "").strip()
        if self.cc.require_reason_for_actions and len(reason) < 5:
            raise ApiError(400, f"a {key} of at least 5 characters is required for this action")
        return reason[:500]

    async def _control(self, event: str, operator: Operator, data: dict[str, Any]) -> None:
        await self.service.c.audit.record("control-plane", event, {**data, "operator": operator.id, "operator_name": operator.name})

    @staticmethod
    def _window(query: dict[str, str], default: int = 300) -> int:
        try:
            return max(30, min(int(query.get("window", default)), 3600))
        except ValueError:
            return default

    async def tick(self) -> None:
        """Evaluate alerts and record transitions. Called on overview reads and by the server's background loop."""
        windows = {rule.window_s for rule in self.alerts.rules} | {300}
        snaps = {w: self.telemetry.snapshot(w) for w in windows}
        fired, resolved = self.alerts.evaluate(snaps, self.service.c.executor.breakers.snapshot(), self.service.c.audit.errors)
        for alert in fired:
            await self.service.c.audit.record("alerts", "alert_fired", {"key": alert.key, "severity": alert.severity,
                                                                        "title": alert.title, "detail": alert.detail})
        for alert in resolved:
            await self.service.c.audit.record("alerts", "alert_resolved", {"key": alert.key, "title": alert.title})

    # ------------------------------------------------------------------ handlers

    async def me(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, {
            "operator": op.to_dict(), "profile": self.cfg.profile, "environment": self.cfg.service.environment,
            "cell": self.cfg.service.cell_id, "region": self.cfg.service.region, "service": self.cfg.service.name,
            "four_eyes": self.cc.require_four_eyes, "utterances_visible": self._may_see_text(op),
            "reason_required": self.cc.require_reason_for_actions, "chaos": self.chaos is not None,
            "runtime_changes": self.cc.runtime_changes_enabled, "operator_auth": self.cc.operator_auth,
        }

    async def overview(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        await self.tick()
        window = self._window(q)
        kill = await self.service.kill_switch()
        latest = self.evals.latest(self.changes.active)
        return 200, {
            "snapshot": self.telemetry.snapshot(window),
            "alerts": self.alerts.listing(),
            "kill_switch": {"disabled_agents": kill.disabled_agents, "disabled_intents": kill.disabled_intents,
                            "disabled_risk_classes": kill.disabled_risk_classes},
            "breakers": self.service.c.executor.breakers.snapshot(),
            "agents": sorted(self.service.c.catalogue.agents),
            "intents": {i.id: i.risk.value for i in self.service.c.catalogue.intents.values()},
            "runtime": {"version": self.changes.active, "shadow": self.service.shadow[0] if self.service.shadow else None,
                        "pending_changes": sum(1 for c in self.changes.listing() if c["status"] in ("evaluated", "shadow"))},
            "evals": latest.summary() if latest else None,
            "health": {"audit_errors": self.service.c.audit.errors, "bus_publish_errors": self.service.c.audit.publish_errors,
                       "events_seen": self.telemetry.events_seen, "last_event_ts": self.telemetry.last_event_ts,
                       "in_flight_turns": self.service.c.admission.in_flight,
                       "max_concurrent_turns": self.cfg.admission.max_concurrent_turns,
                       "uptime_s": round(time.time() - self.started), "store_ok": await self.service.ready()},
            "budgets": {"turn_deadline_ms": self.cfg.budgets.turn_deadline_ms},
        }

    async def timeseries(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, self.telemetry.timeseries(self._window(q, 900), int(q.get("points", 60)))

    async def events(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        items = self.bus.recent(min(int(q.get("limit", 200)), 1000), int(q.get("after", 0)))
        if q.get("session"):
            items = [e for e in items if e.session_id == q["session"]]
        return 200, {"events": [self.project(e, op) for e in items]}

    async def sessions(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"sessions": self.telemetry.recent_sessions(int(q.get("limit", 50)))}

    async def session(self, op: Operator, q: dict[str, str], b: dict[str, Any], sid: str) -> tuple[int, Any]:
        view = await session_turns(self.service.c.audit, sid, self._may_see_text(op))
        if view is None:
            raise ApiError(404, "unknown session")
        return 200, view

    async def turn(self, op: Operator, q: dict[str, str], b: dict[str, Any], sid: str, tid: str) -> tuple[int, Any]:
        show = self._may_see_text(op)
        trace = await inspect_turn(self.service.c.audit, sid, tid, show)
        if trace is None:
            raise ApiError(404, "unknown turn")
        if show:
            await self._control("utterance_viewed", op, {"session_id": sid, "turn_id": tid})
        return 200, trace

    async def terminate(self, op: Operator, q: dict[str, str], b: dict[str, Any], sid: str) -> tuple[int, Any]:
        reason = self._reason(b)
        if await self.service.c.store.get(sid) is None:
            raise ApiError(404, "unknown session")
        await self.service.close_session(sid)
        await self._control("session_terminated", op, {"session_id": sid, "reason": reason})
        return 200, {"terminated": sid}

    async def get_kill_switch(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        kill = await self.service.kill_switch()
        configured = self.cfg.kill_switch
        return 200, {"effective": {"disabled_agents": kill.disabled_agents, "disabled_intents": kill.disabled_intents,
                                   "disabled_risk_classes": kill.disabled_risk_classes},
                     "configured": {"disabled_agents": configured.disabled_agents, "disabled_intents": configured.disabled_intents,
                                    "disabled_risk_classes": configured.disabled_risk_classes}}

    async def put_kill_switch(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        reason = self._reason(b)
        cat = self.service.c.catalogue
        known = {"disabled_agents": set(cat.agents), "disabled_intents": set(cat.intents), "disabled_risk_classes": {"R0", "R1", "R2", "R3"}}
        flags: dict[str, list[str]] = {}
        for key, valid in known.items():
            values = b.get(key) or []
            if not isinstance(values, list) or not set(map(str, values)) <= valid:
                raise ApiError(400, f"{key} contains unknown values")
            flags[key] = [str(v) for v in values]
        clean = await self.service.set_runtime_flags(flags, actor=op.id, reason=reason)
        return 200, {"runtime": clean, **(await self.get_kill_switch(op, q, b))[1]}

    async def reset_breaker(self, op: Operator, q: dict[str, str], b: dict[str, Any], agent: str) -> tuple[int, Any]:
        reason = self._reason(b)
        self.service.c.executor.breakers.reset(agent)
        await self._control("breaker_reset", op, {"agent": agent, "reason": reason})
        return 200, {"breakers": self.service.c.executor.breakers.snapshot()}

    async def get_alerts(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        await self.tick()
        return 200, {**self.alerts.listing(), "rules": [rule.__dict__ for rule in self.alerts.rules]}

    async def ack_alert(self, op: Operator, q: dict[str, str], b: dict[str, Any], key: str) -> tuple[int, Any]:
        note = str(b.get("note") or "").strip()[:500]
        alert = self.alerts.acknowledge(key, op.id, note)
        if alert is None:
            raise ApiError(404, "no active alert with that key")
        await self._control("alert_acknowledged", op, {"key": key, "note": note})
        return 200, alert.to_dict()

    async def get_evals(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        runs = [r.summary() for r in reversed(self.evals.history)]
        latest = self.evals.latest(self.changes.active)
        return 200, {"runs": runs, "latest": latest.to_dict() if latest else None, "cases": len(self.evals.cases)}

    async def get_eval(self, op: Operator, q: dict[str, str], b: dict[str, Any], rid: str) -> tuple[int, Any]:
        run = self.evals.get(rid)
        if run is None:
            raise ApiError(404, "unknown eval run")
        return 200, run.to_dict()

    async def run_evals(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        source, guards, routing = self.changes.effective()
        run = await self.evals.run(self.service.c.catalogue, guards, routing, label=f"manual v{self.changes.active} by {op.name}",
                                   runtime_version=self.changes.active)
        await self._control("evals_run", op, {"run_id": run.id, "pass_rate": run.pass_rate, "passed": run.passed, "total": run.total})
        return 200, run.to_dict()

    async def catalogue(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, self.changes.catalogue_view()

    async def list_changes(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"changes": self.changes.listing(), "active_version": self.changes.active}

    async def get_change(self, op: Operator, q: dict[str, str], b: dict[str, Any], cid: str) -> tuple[int, Any]:
        change = self.changes._get(cid)  # noqa: SLF001 - read access with expiry applied
        run = self.evals.get(change["eval"]["id"])
        snap = self.telemetry.snapshot(3600).get("shadow", {}).get(cid)
        return 200, {**change, "eval_results": run.to_dict()["results"] if run else [], "shadow_stats": snap}

    async def propose(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        ops = b.get("ops")
        if not isinstance(ops, list):
            raise ApiError(400, "ops must be a list")
        change = await self.changes.propose(op.id, ops, str(b.get("reason") or ""), str(b.get("title") or "")[:120])
        return 201, change

    async def shadow(self, op: Operator, q: dict[str, str], b: dict[str, Any], cid: str) -> tuple[int, Any]:
        return 200, await self.changes.start_shadow(cid, op.id)

    async def stop_shadow(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        await self.changes.stop_shadow(op.id)
        return 200, {"shadow": None}

    async def approve(self, op: Operator, q: dict[str, str], b: dict[str, Any], cid: str) -> tuple[int, Any]:
        return 200, await self.changes.approve(cid, op.id, str(b.get("note") or "")[:500])

    async def reject(self, op: Operator, q: dict[str, str], b: dict[str, Any], cid: str) -> tuple[int, Any]:
        return 200, await self.changes.reject(cid, op.id, str(b.get("note") or "")[:500])

    async def preview(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        text = str(b.get("text") or "").strip()
        if not text or len(text) > 500:
            raise ApiError(400, "text must be 1 to 500 characters")
        kill = await self.service.kill_switch()
        ops = b.get("ops") if isinstance(b.get("ops"), list) else None
        return 200, await self.changes.preview(text, ops, set(kill.disabled_intents))

    async def versions(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        return 200, self.changes.versions()

    async def rollback(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        reason = self._reason(b)
        try:
            version = int(b.get("version"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ApiError(400, "version must be an integer") from exc
        return 200, await self.changes.rollback(version, op.id, reason)

    async def control_audit(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        chain = await self.service.c.audit.chain("control-plane")
        ok, bad = verify_chain(chain)
        limit = min(int(q.get("limit", 100)), 500)
        rows = [{"seq": r.seq, "ts": r.ts, "event": r.event, "data": r.data, "hash": r.hash} for r in chain[-limit:]]
        return 200, {"valid": ok, "first_invalid_index": bad, "records": len(chain), "entries": list(reversed(rows))}

    async def verify(self, op: Operator, q: dict[str, str], b: dict[str, Any], chain: str) -> tuple[int, Any]:
        records = await self.service.c.audit.chain(chain)
        ok, bad = verify_chain(records)
        return 200, {"chain_id": chain, "records": len(records), "valid": ok, "first_invalid_index": bad}

    async def get_chaos(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        if self.chaos is None:
            raise ApiError(404, "fault injection is only available on the development server")
        return 200, self.chaos.view()

    async def put_chaos(self, op: Operator, q: dict[str, str], b: dict[str, Any]) -> tuple[int, Any]:
        if self.chaos is None:
            raise ApiError(404, "fault injection is only available on the development server")
        self.chaos.update(b)
        await self._control("fault_injection_changed", op, {"settings": self.chaos.view()})
        return 200, self.chaos.view()


_SHELL_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    "<style>:root{color-scheme:light}body{margin:0}[hidden]{display:none!important}</style></head><body>"
)


def console_page(static_dir: Any = None) -> str:
    """The console is authored as a page fragment (it is also published as an artifact); servers add the shell."""
    from pathlib import Path

    base = Path(static_dir) if static_dir else Path(__file__).resolve().parent / "static"
    return _SHELL_HEAD + (base / "console.html").read_text(encoding="utf-8") + "</body></html>"


def format_sse(item: dict[str, Any]) -> str:
    return f"id: {item['seq']}\nevent: decision\ndata: {json.dumps(item, default=str, separators=(',', ':'))}\n\n"


async def heartbeat_merge(gen: AsyncIterator[dict[str, Any]], interval_s: float = 15.0) -> AsyncIterator[str]:
    """SSE frames with keep-alive comments so proxies do not close idle streams."""
    agen = gen.__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(agen.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield format_sse(item)
    finally:
        if pending is not None:
            pending.cancel()
        await agen.aclose()


def build_console(service: OrchestratorService, *, chaos: Any = None, operator_auth: OperatorAuth | None = None) -> ConsoleAPI:
    """Wire telemetry, alerts, evals and runtime change control around a service."""
    from .evals import load_suite

    cc = service.cfg.command_center
    if service.c.audit.bus is None:
        from .events import InMemoryEventBus

        service.c.audit.bus = InMemoryEventBus(buffer_size=cc.buffer_events)
    telemetry = TelemetryAggregator(bucket_s=cc.metrics_bucket_s, retention_s=cc.metrics_retention_s)
    service.c.audit.bus.add_listener(telemetry.ingest)
    evals = EvalRunner(service.cfg, load_suite(cc.evals_file))
    changes = RuntimeConfigManager(service, evals)
    service.runtime_refresher = changes.refresh
    return ConsoleAPI(service, telemetry, AlertEngine(list(cc.alert_rules)), evals, changes,
                      operator_auth or OperatorAuth(cc), chaos=chaos)
