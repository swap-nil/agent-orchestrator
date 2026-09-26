"""Rolling-window metrics derived from decision events.

The aggregator listens to the event bus and keeps time buckets for the last
``metrics_retention_s`` seconds. Snapshots and time series for any window are
computed from those buckets, so every number on the console can be traced
back to audit events. Long-term history belongs in the OpenTelemetry backend;
this is the operational, real-time view.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import DecisionEvent

TERMINAL_TYPES = ("answer", "clarify", "approval_required", "handover", "refused", "busy")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(ordered[lo], 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)


def _agent() -> dict[str, Any]:
    return {"calls": 0, "ok": 0, "failed": 0, "rejected": 0, "timeouts": 0, "retries": 0, "latencies": [], "skills": defaultdict(int)}


def _intent() -> dict[str, Any]:
    return {"turns": 0, "types": defaultdict(int), "latencies": []}


@dataclass
class Bucket:
    start: int
    turns: int = 0
    replays: int = 0
    types: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    latencies: list[float] = field(default_factory=list)
    degraded: int = 0
    partial: int = 0
    errors: int = 0
    intents: dict[str, dict[str, Any]] = field(default_factory=lambda: defaultdict(_intent))
    risks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    agents: dict[str, dict[str, Any]] = field(default_factory=lambda: defaultdict(_agent))
    input_blocks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    pii: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    injection_flags: int = 0
    output_blocks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    disclaimers: int = 0
    dropped: int = 0
    policy_checks: int = 0
    policy_denials: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    policy_denied_turns: int = 0
    plan_rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    approvals: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    transactions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    shadow: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: {"agree": 0, "disagree": 0}))
    sessions_opened: int = 0
    operator_actions: int = 0


class TelemetryAggregator:
    def __init__(self, bucket_s: int = 10, retention_s: int = 3600, clock: Callable[[], float] = time.time, max_sessions: int = 300) -> None:
        self.bucket_s = bucket_s
        self.retention_s = retention_s
        self._clock = clock
        self._buckets: "OrderedDict[int, Bucket]" = OrderedDict()
        self.sessions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._max_sessions = max_sessions
        self.events_seen = 0
        self.last_event_ts: float | None = None

    # ------------------------------------------------------------------ ingest

    def _bucket(self, ts: float) -> Bucket:
        start = int(ts // self.bucket_s) * self.bucket_s
        bucket = self._buckets.get(start)
        if bucket is None:
            bucket = Bucket(start)
            self._buckets[start] = bucket
            if len(self._buckets) > 1 and next(reversed(self._buckets)) != start:
                self._buckets = OrderedDict(sorted(self._buckets.items()))
            cutoff = self._clock() - self.retention_s - self.bucket_s
            while self._buckets and next(iter(self._buckets)) < cutoff:
                self._buckets.popitem(last=False)
        return bucket

    def _session(self, event: DecisionEvent) -> dict[str, Any] | None:
        sid = event.session_id
        if not sid:
            return None
        entry = self.sessions.get(sid)
        if entry is None:
            entry = {"session_id": sid, "started": event.ts, "last": event.ts, "turns": 0, "last_intent": None,
                     "last_type": None, "channel": None, "open": True, "flags": set()}
            self.sessions[sid] = entry
            while len(self.sessions) > self._max_sessions:
                self.sessions.popitem(last=False)
        entry["last"] = max(entry["last"], event.ts)
        self.sessions.move_to_end(sid)
        return entry

    def ingest(self, event: DecisionEvent) -> None:
        self.events_seen += 1
        self.last_event_ts = event.ts
        b = self._bucket(event.ts)
        d = event.data
        sess = self._session(event) if not event.chain.startswith(("txn-", "control-plane", "alerts", "approvals")) else None
        name = event.event

        if name == "turn_completed":
            if d.get("replayed"):
                b.replays += 1
                return
            rtype = str(d.get("type"))
            b.turns += 1
            b.types[rtype] += 1
            for r in d.get("reasons") or []:
                b.reasons[str(r)] += 1
            if "internal_error" in (d.get("reasons") or []):
                b.errors += 1
            lat = d.get("latency_ms")
            if isinstance(lat, (int, float)):
                b.latencies.append(float(lat))
            b.degraded += 1 if d.get("degraded") else 0
            b.partial += 1 if d.get("partial") else 0
            if d.get("risk"):
                b.risks[str(d["risk"])] += 1
            intent = d.get("intent") or "(none)"
            it = b.intents[intent]
            it["turns"] += 1
            it["types"][rtype] += 1
            if isinstance(lat, (int, float)):
                it["latencies"].append(float(lat))
            if sess is not None:
                sess["turns"] += 1
                sess["last_intent"] = d.get("intent")
                sess["last_type"] = rtype
                sess["channel"] = d.get("channel")
                if rtype == "handover":
                    sess["flags"].add("handover")
                if rtype == "approval_required":
                    sess["flags"].add("transaction")
        elif name == "turn_received":
            for flag in d.get("flags") or []:
                flag = str(flag)
                if flag.startswith("pii:"):
                    for kind in flag[4:].split(","):
                        b.pii[kind] += 1
                if flag == "prompt_injection":
                    b.injection_flags += 1
        elif name == "input_blocked":
            for flag in d.get("flags") or []:
                if not str(flag).startswith("pii:"):
                    b.input_blocks[str(flag)] += 1
            if sess is not None:
                sess["flags"].add("blocked")
        elif name in ("step_result", "write_step_result"):
            agent = str(d.get("agent") or "?")
            a = b.agents[agent]
            state = str(d.get("state") or "")
            if d.get("skipped"):
                return
            if state.endswith("REJECTED") and d.get("attempts", 1) == 0:
                a["rejected"] += 1
                return
            a["calls"] += 1
            if d.get("skill"):
                a["skills"][str(d["skill"])] += 1
            attempts = int(d.get("attempts") or 1)
            a["retries"] += max(0, attempts - 1)
            if state.endswith("COMPLETED"):
                a["ok"] += 1
            else:
                a["failed"] += 1
                err = str(d.get("error") or "").lower()
                if "timed out" in err or "deadline" in err:
                    a["timeouts"] += 1
            lat = d.get("latency_ms")
            if isinstance(lat, (int, float)) and lat > 0:
                a["latencies"].append(float(lat))
        elif name == "policy_checked":
            b.policy_checks += len(d.get("decisions") or [])
        elif name == "policy_denied":
            b.policy_denied_turns += 1
            for r in d.get("reasons") or []:
                b.policy_denials[str(r)] += 1
            if sess is not None:
                sess["flags"].add("denied")
        elif name == "plan_rejected":
            for r in d.get("reasons") or []:
                b.plan_rejections[str(r)] += 1
            if sess is not None:
                sess["flags"].add("denied")
        elif name == "output_checked":
            b.dropped += int(d.get("dropped") or 0)
            if "disclaimer_added" in (d.get("flags") or []):
                b.disclaimers += 1
        elif name == "output_blocked":
            for flag in d.get("flags") or []:
                b.output_blocks[str(flag)] += 1
        elif name == "approval_requested":
            b.approvals["requested"] += 1
        elif name == "approval_decided" and "workflow_id" in d:
            if d.get("approved"):
                b.approvals["approved"] += 1
            elif "declined by user" in (d.get("reasons") or []):
                b.approvals["declined"] += 1
            else:
                b.approvals["rejected"] += 1
        elif name.startswith("transaction_"):
            b.transactions[name[len("transaction_"):]] += 1
        elif name == "shadow_routed":
            key = "agree" if d.get("agrees") else "disagree"
            b.shadow[str(d.get("change_id"))][key] += 1
        elif name == "session_opened":
            b.sessions_opened += 1
            if sess is not None:
                sess["channel"] = d.get("channel")
        elif name in ("session_closed", "session_terminated"):
            target = d.get("session_id") or event.session_id
            if target in self.sessions:
                self.sessions[target]["open"] = False
                if name == "session_terminated":
                    self.sessions[target]["flags"].add("terminated")
        if event.chain == "control-plane":
            b.operator_actions += 1

    # ------------------------------------------------------------------ query

    def _window(self, window_s: int) -> list[Bucket]:
        cutoff = self._clock() - window_s
        return [bk for start, bk in self._buckets.items() if start + self.bucket_s > cutoff]

    def snapshot(self, window_s: int = 300) -> dict[str, Any]:
        buckets = self._window(window_s)
        turns = sum(bk.turns for bk in buckets)
        types: dict[str, int] = defaultdict(int)
        reasons: dict[str, int] = defaultdict(int)
        risks: dict[str, int] = defaultdict(int)
        latencies: list[float] = []
        agents: dict[str, dict[str, Any]] = defaultdict(_agent)
        intents: dict[str, dict[str, Any]] = defaultdict(_intent)
        merge_keys = ("input_blocks", "pii", "output_blocks", "policy_denials", "plan_rejections", "approvals", "transactions")
        merged: dict[str, dict[str, int]] = {k: defaultdict(int) for k in merge_keys}
        shadow: dict[str, dict[str, int]] = defaultdict(lambda: {"agree": 0, "disagree": 0})
        totals = defaultdict(int)
        for bk in buckets:
            latencies.extend(bk.latencies)
            for k, v in bk.types.items():
                types[k] += v
            for k, v in bk.reasons.items():
                reasons[k] += v
            for k, v in bk.risks.items():
                risks[k] += v
            for key in merge_keys:
                for k, v in getattr(bk, key).items():
                    merged[key][k] += v
            for name, a in bk.agents.items():
                t = agents[name]
                for f in ("calls", "ok", "failed", "rejected", "timeouts", "retries"):
                    t[f] += a[f]
                t["latencies"].extend(a["latencies"])
                for sk, n in a["skills"].items():
                    t["skills"][sk] += n
            for name, it in bk.intents.items():
                t = intents[name]
                t["turns"] += it["turns"]
                t["latencies"].extend(it["latencies"])
                for k, v in it["types"].items():
                    t["types"][k] += v
            for cid, v in bk.shadow.items():
                shadow[cid]["agree"] += v["agree"]
                shadow[cid]["disagree"] += v["disagree"]
            for f in ("replays", "degraded", "partial", "errors", "injection_flags", "disclaimers", "dropped", "policy_checks",
                      "policy_denied_turns", "sessions_opened", "operator_actions"):
                totals[f] += getattr(bk, f)

        def rate(n: float) -> float:
            return round(n / turns, 4) if turns else 0.0

        agent_rows = {}
        for name, a in sorted(agents.items()):
            calls = a["calls"]
            agent_rows[name] = {
                "calls": calls, "ok": a["ok"], "failed": a["failed"], "rejected_by_policy": a["rejected"],
                "timeouts": a["timeouts"], "retries": a["retries"],
                "error_rate": round(a["failed"] / calls, 4) if calls else 0.0,
                "p50_ms": percentile(a["latencies"], 0.5), "p95_ms": percentile(a["latencies"], 0.95),
                "skills": dict(a["skills"]),
            }
        intent_rows = {
            name: {"turns": it["turns"], "types": dict(it["types"]), "p95_ms": percentile(it["latencies"], 0.95)}
            for name, it in sorted(intents.items(), key=lambda kv: -kv[1]["turns"])
        }
        blocked = sum(merged["input_blocks"].values())
        return {
            "window_s": window_s,
            "generated_at": self._clock(),
            "turns": turns,
            "turns_per_min": round(turns / (window_s / 60), 2) if window_s else 0.0,
            "types": dict(types),
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "risks": dict(risks),
            "latency": {"p50_ms": percentile(latencies, 0.5), "p95_ms": percentile(latencies, 0.95),
                        "p99_ms": percentile(latencies, 0.99), "samples": len(latencies)},
            "rates": {
                "answer_rate": rate(types.get("answer", 0)),
                "clarify_rate": rate(types.get("clarify", 0)),
                "handover_rate": rate(types.get("handover", 0)),
                "refusal_rate": rate(types.get("refused", 0)),
                "busy_rate": rate(types.get("busy", 0)),
                "error_rate": rate(totals["errors"]),
                "input_block_rate": rate(blocked),
                "policy_denial_rate": rate(totals["policy_denied_turns"]),
                "degraded_rate": rate(totals["degraded"]),
                "partial_rate": rate(totals["partial"]),
            },
            "agents": agent_rows,
            "intents": intent_rows,
            "safeguards": {
                "input_blocks": dict(merged["input_blocks"]), "injection_flags": totals["injection_flags"],
                "pii_redactions": dict(merged["pii"]), "output_blocks": dict(merged["output_blocks"]),
                "ungrounded_dropped": totals["dropped"], "disclaimers_added": totals["disclaimers"],
                "policy_checks": totals["policy_checks"], "policy_denials": dict(merged["policy_denials"]),
                "plan_rejections": dict(merged["plan_rejections"]),
            },
            "approvals": dict(merged["approvals"]),
            "transactions": dict(merged["transactions"]),
            "shadow": {cid: {**v, "agreement": round(v["agree"] / (v["agree"] + v["disagree"]), 4) if (v["agree"] + v["disagree"]) else None}
                       for cid, v in shadow.items()},
            "replays": totals["replays"],
            "sessions_opened": totals["sessions_opened"],
            "active_sessions": sum(1 for s in self.sessions.values() if s["open"] and s["last"] > self._clock() - 900),
            "operator_actions": totals["operator_actions"],
        }

    def timeseries(self, window_s: int = 900, points: int = 60) -> dict[str, Any]:
        step = max(self.bucket_s, int(math.ceil(window_s / points / self.bucket_s)) * self.bucket_s)
        now = self._clock()
        end = int(now // step) * step + step
        start = end - (window_s // step) * step
        series: dict[str, list[Any]] = {k: [] for k in ("t", "turns", "p95_ms", "handover", "refused", "busy", "blocked", "errors", "agent_failures")}
        by_type: dict[str, list[int]] = {t: [] for t in TERMINAL_TYPES}
        slots: dict[int, list[Bucket]] = defaultdict(list)
        for bstart, bk in self._buckets.items():
            if start <= bstart < end:
                slots[(bstart - start) // step].append(bk)
        for i in range((end - start) // step):
            group = slots.get(i, [])
            lat = [x for bk in group for x in bk.latencies]
            series["t"].append(start + i * step)
            series["turns"].append(sum(bk.turns for bk in group))
            series["p95_ms"].append(percentile(lat, 0.95))
            series["handover"].append(sum(bk.types.get("handover", 0) for bk in group))
            series["refused"].append(sum(bk.types.get("refused", 0) for bk in group))
            series["busy"].append(sum(bk.types.get("busy", 0) for bk in group))
            series["blocked"].append(sum(sum(bk.input_blocks.values()) + sum(bk.output_blocks.values()) for bk in group))
            series["errors"].append(sum(bk.errors for bk in group))
            series["agent_failures"].append(sum(a["failed"] for bk in group for a in bk.agents.values()))
            for t in TERMINAL_TYPES:
                by_type[t].append(sum(bk.types.get(t, 0) for bk in group))
        return {"step_s": step, "window_s": window_s, **series, "types": by_type}

    def recent_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = list(self.sessions.values())[-limit:]
        return [{**r, "flags": sorted(r["flags"])} for r in reversed(rows)]
