"""Turn inspector: what happened in one turn, and why.

Reconstructs a turn from its session's audit chain (the source of truth, which
is verified at the same time) and turns each record into a stage with a
plain-language explanation. Utterances are shown only when the caller may see
them; otherwise their length is shown.
"""

from __future__ import annotations

from typing import Any

from ..audit import AuditRecord, verify_chain

STATUS_OK, STATUS_WARN, STATUS_BLOCK, STATUS_INFO = "ok", "warn", "block", "info"


def _text_view(data: dict[str, Any], show_text: bool) -> str | None:
    text = data.get("text")
    if not isinstance(text, str):
        return None
    return text if show_text else f"[hidden: {len(text)} characters]"


def explain(record: AuditRecord, show_text: bool) -> dict[str, Any] | None:
    d = record.data
    e = record.event
    title, why, status, details = e, "", STATUS_INFO, {}
    if e == "turn_received":
        flags = [f for f in d.get("flags") or []]
        pii = [f[4:] for f in flags if str(f).startswith("pii:")]
        title = "Heard the user"
        parts = []
        if pii:
            parts.append(f"Personal data redacted before logging ({pii[0].replace(',', ', ')}).")
        if "prompt_injection" in flags:
            parts.append("Text matched a prompt-injection pattern.")
            status = STATUS_WARN
        why = " ".join(parts) or "Input passed hygiene checks."
        status = status if status != STATUS_INFO else STATUS_OK
        details = {"text": _text_view(d, show_text), "channel": d.get("channel"), "flags": flags}
    elif e == "input_blocked":
        title, status = "Input blocked", STATUS_BLOCK
        why = f"Refused before routing: {', '.join(str(f) for f in d.get('flags') or [] if not str(f).startswith('pii:'))}. Nothing was sent to any agent."
    elif e == "routed":
        title = "Routed"
        intent, conf, thr, src = d.get("intent"), d.get("confidence"), d.get("threshold"), d.get("source")
        if src == "disabled":
            why, status = f"Matched {intent}, which is switched off by the kill switch; the user is told it is unavailable.", STATUS_BLOCK
        elif d.get("clarify") and len(d.get("candidates") or []) > 1 and not intent:
            why, status = f"Ambiguous between {' and '.join(d['candidates'])}; the assistant asks instead of guessing.", STATUS_WARN
        elif d.get("clarify"):
            why = (f"Best match {intent} ({d.get('risk')}) had confidence {conf} below its threshold {thr}; asked to clarify."
                   if intent else "Nothing matched with enough confidence; asked to clarify.")
            status = STATUS_WARN
        elif src == "rules":
            why, status = f"Routing rules matched {intent} ({d.get('risk')}) with confidence {conf} ≥ threshold {thr}.", STATUS_OK
        elif src == "model":
            why, status = f"No rule matched; the model classifier chose {intent} ({d.get('risk')}). Models may only choose R0/R1 intents.", STATUS_OK
        elif src == "fallback":
            why, status = f"No rule matched; the R0 fallback intent {intent} answers.", STATUS_OK
        details = {"intent": intent, "risk": d.get("risk"), "confidence": conf, "threshold": thr, "source": src, "candidates": d.get("candidates")}
    elif e == "shadow_routed":
        title = f"Shadow routing ({d.get('change_id')})"
        why = (f"The candidate configuration would also route to {d.get('candidate')}." if d.get("agrees")
               else f"Disagreement: live routed to {d.get('live')}, the candidate would route to {d.get('candidate')}.")
        status = STATUS_OK if d.get("agrees") else STATUS_WARN
    elif e == "planned":
        steps = d.get("steps") or []
        layers = 1 + max((s.get("layer", 0) for s in steps), default=0)
        title = "Planned"
        why = f"{len(steps)} step(s) in {layers} layer(s), cost {d.get('cost_units')} units."
        if d.get("skipped_optional"):
            why += f" Under load, optional steps skipped: {', '.join(d['skipped_optional'])}."
            status = STATUS_WARN
        if not d.get("valid", True):
            why += f" Rejected: {'; '.join(d.get('reasons') or [])}."
            status = STATUS_BLOCK
        else:
            status = STATUS_OK if status == STATUS_INFO else status
        details = {"steps": steps}
    elif e == "policy_checked":
        decisions = d.get("decisions") or []
        denied = [x for x in decisions if not x.get("allow")]
        title = "Policy check (plan)"
        if denied:
            why, status = f"Denied for step {denied[0]['step']}: {'; '.join(denied[0].get('reasons') or [])}.", STATUS_BLOCK
        else:
            why, status = f"All {len(decisions)} step(s) allowed by {decisions[0].get('engine', 'policy') if decisions else 'policy'}.", STATUS_OK
        details = {"decisions": decisions}
    elif e in ("policy_denied", "plan_rejected"):
        return None  # already explained by policy_checked / planned
    elif e == "step_result":
        agent, state = d.get("agent"), str(d.get("state") or "").replace("TASK_STATE_", "").lower()
        title = f"{agent} · {d.get('skill') or d.get('step')}"
        if d.get("skipped"):
            why, status = f"Skipped: {d.get('error')}.", STATUS_WARN
        elif state == "completed":
            extra = f" after {d['attempts']} attempts" if (d.get("attempts") or 1) > 1 else ""
            why, status = f"Completed in {d.get('latency_ms')} ms{extra}. Policy re-checked just before the call.", STATUS_OK
        elif state == "rejected" and d.get("policy_reasons"):
            why, status = f"Denied at execution: {'; '.join(d['policy_reasons'])}.", STATUS_BLOCK
        else:
            why, status = f"{state or 'failed'}: {d.get('error')}.", STATUS_BLOCK
        details = {k: d.get(k) for k in ("step", "state", "attempts", "latency_ms", "task_id", "decision_id", "error")}
    elif e == "output_checked":
        title = "Answer checked"
        parts = [f"{len(d.get('sources') or [])} source(s) attached."]
        if d.get("dropped"):
            parts.append(f"{d['dropped']} artifact(s) dropped (no sources or classification not allowed).")
            status = STATUS_WARN
        if "disclaimer_added" in (d.get("flags") or []):
            parts.append("Advice disclaimer appended.")
        if not d.get("allowed", True):
            parts.append(f"Blocked: {', '.join(d.get('flags') or [])}; handed over instead.")
            status = STATUS_BLOCK
        why = " ".join(parts)
        status = STATUS_OK if status == STATUS_INFO else status
        details = {"sources": d.get("sources"), "flags": d.get("flags")}
    elif e == "output_blocked":
        return None
    elif e == "approval_requested":
        title, status = "Approval requested", STATUS_WARN
        why = f"Transaction prepared but not executed. Waiting for step-up approval bound to hash {str(d.get('action_hash'))[:12]}…"
        details = {"workflow_id": d.get("workflow_id"), "approval_id": d.get("approval_id")}
    elif e == "approval_decided":
        title = "Approval decided"
        why = "Approved; the workflow executes the write." if d.get("approved") else f"Not approved: {'; '.join(d.get('reasons') or [])}."
        status = STATUS_OK if d.get("approved") else STATUS_BLOCK
    elif e == "turn_completed":
        title = f"Responded: {d.get('type')}"
        why = f"Total {d.get('latency_ms')} ms."
        if d.get("reasons"):
            why += f" Reasons: {', '.join(d['reasons'])}."
        if d.get("partial"):
            why += " Answer marked partial."
        status = {"answer": STATUS_OK, "approval_required": STATUS_OK, "clarify": STATUS_WARN}.get(str(d.get("type")), STATUS_BLOCK)
        details = {"type": d.get("type"), "latency_ms": d.get("latency_ms"), "runtime_version": d.get("runtime_version")}
    elif e == "turn_replayed":
        title, why, status = "Replayed", "Same turn id retried; the stored answer was returned without executing again.", STATUS_OK
    elif e == "turn_error":
        title, why, status = "Internal error", "Unexpected error; the user was handed over safely. See service logs for this trace id.", STATUS_BLOCK
    else:
        why = ""
    return {"event": e, "title": title, "why": why, "status": status, "details": details, "ts": record.ts, "seq": record.seq}


async def inspect_turn(audit: Any, session_id: str, turn_id: str, show_text: bool) -> dict[str, Any] | None:
    chain: list[AuditRecord] = await audit.chain(session_id)
    ok, bad_index = verify_chain(chain)
    records = [r for r in chain if r.turn_id == turn_id]
    if not records:
        return None
    t0 = records[0].ts
    stages = []
    for r in records:
        stage = explain(r, show_text)
        if stage is not None:
            stage["offset_ms"] = round((r.ts - t0) * 1000, 1)
            stages.append(stage)
    done = next((r for r in reversed(records) if r.event == "turn_completed"), None)
    routed = next((r for r in records if r.event == "routed"), None)
    return {
        "session_id": session_id, "turn_id": turn_id, "trace_id": records[0].trace_id,
        "started": t0, "outcome": done.data.get("type") if done else None,
        "latency_ms": done.data.get("latency_ms") if done else None,
        "intent": routed.data.get("intent") if routed else None, "risk": routed.data.get("risk") if routed else None,
        "chain_valid": ok, "chain_first_invalid": bad_index, "chain_records": len(chain), "stages": stages,
    }


async def session_turns(audit: Any, session_id: str, show_text: bool) -> dict[str, Any] | None:
    chain: list[AuditRecord] = await audit.chain(session_id)
    if not chain:
        return None
    ok, bad = verify_chain(chain)
    turns: dict[str, dict[str, Any]] = {}
    for r in chain:
        if not r.turn_id:
            continue
        t = turns.setdefault(r.turn_id, {"turn_id": r.turn_id, "ts": r.ts, "text": None, "intent": None, "type": None, "latency_ms": None})
        if r.event == "turn_received":
            t["text"] = _text_view(r.data, show_text)
        elif r.event == "routed":
            t["intent"] = r.data.get("intent")
        elif r.event == "turn_completed":
            t["type"], t["latency_ms"] = r.data.get("type"), r.data.get("latency_ms")
    return {"session_id": session_id, "chain_valid": ok, "chain_first_invalid": bad, "records": len(chain),
            "turns": sorted(turns.values(), key=lambda t: t["ts"])}
