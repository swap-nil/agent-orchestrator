"""Domain agents backed by the mock core-banking API (test environment).

Same six agents and skills as the catalogue, but every answer is computed from
the fake bank's data through its HTTP API (the agents' "tools"), per user:
the user is the ``oid`` (or ``sub``) of the delegated token the orchestrator
obtained on their behalf. Placing an order is a real write in the fake bank,
guarded like production: the trade agent rebuilds the approved action,
checks it against the action hash and verifies the orchestrator's Ed25519
approval token before calling the order API.

Agents only receive the catalogue instruction and earlier steps' data, never
the user's words, so the FAQ agent answers with featured articles and the trade
agent proposes a deterministic order (10 percent of the largest position),
which the read-back states exactly before the user approves.
"""

from __future__ import annotations

from typing import Any

import httpx

from orchestrator.approvals import action_hash, verify_approval_token

from .kit import DomainAgent, SkillHandler, SkillRequest, SkillResult, TokenVerifier

DEV_SUBJECT = "local-dev-user"


class BankTools:
    """Thin client for the mock core-banking API."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout_s: float = 2.0) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def _call(self, method: str, path: str, subject: str = "", json: Any = None,
                    headers: dict[str, str] | None = None) -> dict[str, Any]:
        h = {"X-User-Subject": subject} if subject else {}
        h.update(headers or {})
        response = await self._client.request(method, path, json=json, headers=h)
        if response.status_code >= 400:
            detail = response.json().get("detail", "") if "json" in response.headers.get("content-type", "") else ""
            raise ToolError(f"{method} {path}: HTTP {response.status_code} {detail}".strip())
        return response.json()

    async def portfolio(self, subject: str) -> dict[str, Any]:
        return await self._call("GET", "/v1/portfolio", subject)

    async def indices(self) -> list[dict[str, Any]]:
        return (await self._call("GET", "/v1/market/indices"))["indices"]

    async def articles(self, query: str = "") -> list[dict[str, str]]:
        return (await self._call("GET", f"/v1/kb/articles?q={query}"))["articles"]

    async def rebalance(self, subject: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/advice/rebalance", subject)

    async def suitability(self, subject: str, proposal: dict[str, Any] | None) -> dict[str, Any]:
        return await self._call("POST", "/v1/compliance/suitability", subject, {"proposal": proposal})

    async def quote(self, subject: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/orders/quote", subject, {})

    async def place_order(self, subject: str, action: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/orders", subject, {"action": action}, {"Idempotency-Key": idempotency_key})


class ToolError(Exception):
    pass


def subject_of(req: SkillRequest) -> str:
    return str(req.claims.get("oid") or req.claims.get("sub") or DEV_SUBJECT)


def _pct(value: float) -> str:
    return f"{abs(value):.1f}".rstrip("0").rstrip(".")


def _first_input(req: SkillRequest, step: str, key: str) -> Any:
    for data in (req.data.get("inputs") or {}).get(step) or []:
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


def require_scope(skill: str, handler: SkillHandler) -> SkillHandler:
    """With a verified token, the delegated scope (``scp``) must name the skill being called."""

    async def guarded(req: SkillRequest) -> SkillResult:
        if req.claims and skill not in str(req.claims.get("scp", "")).split():
            return SkillResult("The delegated token does not grant this skill.", [], "internal", state="TASK_STATE_REJECTED")
        return await handler(req)

    return guarded


def build_backend_agents(
    tools: BankTools, *, approval_public_key: str = "", trade_intent: str = "trade.sell",
    token_verifier: TokenVerifier | None = None,
) -> dict[str, DomainAgent]:
    async def faq(req: SkillRequest) -> SkillResult:
        articles = await tools.articles()
        return SkillResult(" ".join(a["text"] for a in articles), [f"kb://articles/{a['id']}" for a in articles], "public")

    async def holdings(req: SkillRequest) -> SkillResult:
        pf = await tools.portfolio(subject_of(req))
        positions = pf["positions"]
        if not positions:
            return SkillResult("You currently hold no investments with us.", [f"core://positions/{pf['customer_id']}"],
                               "client_confidential", {"positions": []})
        top = positions[0]
        text = (f"You hold {len(positions)} positions worth about CHF {pf['total_value_chf']:,}; "
                f"the largest is the {top['instrument']} at {_pct(top['weight_pct'])} percent.")
        data = {"positions": [{k: p[k] for k in ("instrument_id", "instrument", "units", "value_chf", "asset_class")}
                              for p in positions],
                "total_value_chf": pf["total_value_chf"], "risk_profile": pf["risk_profile"]}
        return SkillResult(text, [f"core://positions/{pf['customer_id']}"], "client_confidential", data)

    async def quotes(req: SkillRequest) -> SkillResult:
        idx = await tools.indices()
        parts = [f"the {i['name']} is {'up' if i['change_pct'] >= 0 else 'down'} {_pct(i['change_pct'])} percent at "
                 f"{i['level']:,.0f}" for i in idx[:2]]
        text = ("Today " + " and ".join(parts) + ".") if parts else "Market data is not available right now."
        return SkillResult(text, [f"mkt://indices/{i['id'].lower()}" for i in idx[:2]], "public", {"indices": idx[:2]})

    async def rebalance(req: SkillRequest) -> SkillResult:
        result = await tools.rebalance(subject_of(req))
        return SkillResult(result["text"], ["model://allocation/mock-v1"], "client_confidential",
                           {"proposal": result.get("proposal"), "needed": result["needed"]})

    async def suitability(req: SkillRequest) -> SkillResult:
        proposal = _first_input(req, "proposal", "proposal")
        result = await tools.suitability(subject_of(req), proposal)
        return SkillResult(result["text"], [f"policy://suitability/{result['reference']}"], "client_confidential",
                           {"suitable": result["suitable"], "reference": result["reference"]})

    async def prepare(req: SkillRequest) -> SkillResult:
        action = await tools.quote(subject_of(req))
        return SkillResult("Order prepared, not placed.", ["core://quotes/mock"], "client_confidential", {"action": action})

    async def execute(req: SkillRequest) -> SkillResult:
        params = req.data.get("action")
        presented = str(req.data.get("actionHash") or "")
        if not isinstance(params, dict):
            return SkillResult("No approved action was provided.", [], "internal", state="TASK_STATE_REJECTED")
        # Rebuild the action the user approved; any change to it changes the hash.
        rebuilt = {"intent": trade_intent, "session_id": req.context_id, "tenant": str(req.metadata.get("tenant", "")),
                   "params": params}
        if action_hash(rebuilt) != presented:
            return SkillResult("The order does not match the approved action.", [], "internal", state="TASK_STATE_REJECTED")
        token = str(req.metadata.get("approvalToken") or "")
        if approval_public_key and not verify_approval_token(approval_public_key, token, presented):
            return SkillResult("The approval is not valid for this order.", [], "internal", state="TASK_STATE_REJECTED")
        order = await tools.place_order(subject_of(req), params, str(req.metadata.get("idempotencyKey", "")))
        return SkillResult(f"Your sell order {order['reference']} has been placed.", [f"core://orders/{order['reference']}"],
                           "client_confidential", {"reference": order["reference"], "status": order["status"]})

    def agent(name: str, skills: dict[str, SkillHandler], writes: set[str] | None = None) -> DomainAgent:
        guarded = {skill: require_scope(skill, handler) for skill, handler in skills.items()}
        return DomainAgent(name, guarded, write_skills=writes, token_verifier=token_verifier)

    return {
        "faq-agent": agent("faq-agent", {"faq.answer": faq}),
        "portfolio-agent": agent("portfolio-agent", {"portfolio.holdings": holdings}),
        "market-agent": agent("market-agent", {"market.quotes": quotes}),
        "advice-agent": agent("advice-agent", {"advice.rebalance": rebalance}),
        "compliance-agent": agent("compliance-agent", {"compliance.suitability": suitability}),
        "trade-agent": agent("trade-agent", {"trade.prepare": prepare, "trade.execute": execute}, {"trade.execute"}),
    }
