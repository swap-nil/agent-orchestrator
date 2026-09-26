"""Alert rules evaluated against the rolling metrics.

Rules come from ``command_center.alert_rules``. Two built-in critical alerts
always apply: an open circuit breaker on any agent, and audit write failures.
An alert fires when its condition holds, can be acknowledged by an operator
(who and why are audited), and resolves on its own when the condition clears.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..config import AlertRuleConfig


@dataclass
class Alert:
    key: str
    rule_id: str
    severity: str
    title: str
    detail: str
    value: float | None
    threshold: float | None
    fired_at: float
    last_seen: float
    subject: str = ""
    status: str = "firing"  # firing | acknowledged | resolved
    acknowledged_by: str | None = None
    acknowledged_at: float | None = None
    ack_note: str = ""
    resolved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def metric_value(snapshot: dict[str, Any], path: str) -> Any:
    node: Any = snapshot
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@dataclass
class _Firing:
    key: str
    rule: AlertRuleConfig | None
    severity: str
    title: str
    detail: str
    value: float | None
    threshold: float | None
    subject: str = ""


@dataclass
class AlertEngine:
    rules: list[AlertRuleConfig]
    clock: Callable[[], float] = time.time
    active: dict[str, Alert] = field(default_factory=dict)
    history: list[Alert] = field(default_factory=list)
    history_limit: int = 200

    def _conditions(self, snapshots: dict[int, dict[str, Any]], breakers: dict[str, dict[str, Any]], audit_errors: int) -> list[_Firing]:
        out: list[_Firing] = []
        for rule in self.rules:
            snap = snapshots[rule.window_s]
            if rule.metric.startswith("agents.*."):
                field_name = rule.metric.split(".", 2)[2]
                for agent, row in (snap.get("agents") or {}).items():
                    value = row.get(field_name)
                    if row.get("calls", 0) < rule.min_samples or not isinstance(value, (int, float)):
                        continue
                    if (value > rule.threshold) if rule.op == ">" else (value < rule.threshold):
                        out.append(_Firing(f"{rule.id}:{agent}", rule, rule.severity, f"{agent}: {rule.description or rule.id}",
                                           f"{field_name} {value:g} {rule.op} {rule.threshold:g} over {rule.window_s}s", float(value), rule.threshold, agent))
                continue
            value = metric_value(snap, rule.metric)
            if not isinstance(value, (int, float)) or snap.get("turns", 0) < rule.min_samples:
                continue
            if (value > rule.threshold) if rule.op == ">" else (value < rule.threshold):
                out.append(_Firing(rule.id, rule, rule.severity, rule.description or rule.id,
                                   f"{rule.metric} {value:g} {rule.op} {rule.threshold:g} over {rule.window_s}s", float(value), rule.threshold))
        for agent, st in breakers.items():
            if st.get("state") == "open":
                out.append(_Firing(f"breaker:{agent}", None, "critical", f"{agent}: circuit breaker open",
                                   f"{st.get('consecutive_failures')} consecutive failures; calls are short-circuited", None, None, agent))
        if audit_errors > 0:
            out.append(_Firing("audit-errors", None, "critical", "Audit ledger write failures",
                               f"{audit_errors} failed writes since start; turns fail safe while this persists", float(audit_errors), 0.0))
        return out

    def evaluate(self, snapshots: dict[int, dict[str, Any]], breakers: dict[str, dict[str, Any]], audit_errors: int) -> tuple[list[Alert], list[Alert]]:
        """Returns (newly fired, newly resolved)."""
        now = self.clock()
        current = {f.key: f for f in self._conditions(snapshots, breakers, audit_errors)}
        fired: list[Alert] = []
        resolved: list[Alert] = []
        for key, f in current.items():
            alert = self.active.get(key)
            if alert is None:
                alert = Alert(key, f.rule.id if f.rule else key.split(":")[0], f.severity, f.title, f.detail, f.value, f.threshold, now, now, f.subject)
                self.active[key] = alert
                fired.append(alert)
            else:
                alert.last_seen, alert.detail, alert.value = now, f.detail, f.value
        for key in [k for k in self.active if k not in current]:
            alert = self.active.pop(key)
            alert.status, alert.resolved_at = "resolved", now
            self.history.append(alert)
            resolved.append(alert)
        del self.history[:-self.history_limit]
        return fired, resolved

    def acknowledge(self, key: str, operator: str, note: str) -> Alert | None:
        alert = self.active.get(key)
        if alert is None:
            return None
        alert.status, alert.acknowledged_by, alert.acknowledged_at, alert.ack_note = "acknowledged", operator, self.clock(), note
        return alert

    def listing(self) -> dict[str, Any]:
        order = {"critical": 0, "warning": 1}
        active = sorted(self.active.values(), key=lambda a: (order.get(a.severity, 2), a.status != "firing", -a.fired_at))
        return {"active": [a.to_dict() for a in active], "history": [a.to_dict() for a in reversed(self.history[-50:])]}
