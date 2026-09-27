"""Domain agents backed by the mock core-banking API (test environment).

Same six agents and skills as the catalogue, but every answer is computed from
the fake bank's data through its HTTP API (the agents' "tools"), per user:
the user is the ``oid`` (or ``sub``) of the delegated token the orchestrator
obtained on their behalf. Placing an order is a real write in the fake bank,
guarded like production: the trade agent rebuilds the approved action,
checks it against the action hash and verifies the orchestrator's Ed25519
approval token before calling the order API.

Agents only receive the catalogue instruction and earlier steps' data, never
the user's words, with structured exceptions: the FAQ agent gets the
redacted question (``data.query``, public R0 step) to search the knowledge
base, the portfolio agent gets what the question is about (``data.slots.focus``:
smallest, largest, how many ...), and the trade agent gets the slots the
orchestrator extracted (``data.slots``: which holding, how much). The trade agent resolves the
holding against the customer's positions and asks back (input-required,
``data.missing``) instead of guessing when it is not held, ambiguous or the
quantity is missing or too large.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from orchestrator.approvals import action_hash, verify_approval_token
from orchestrator.slots import match_instruments, units_for

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
        path = "/v1/kb/articles" + (f"?{urlencode({'q': query[:500]})}" if query else "")
        return (await self._call("GET", path))["articles"]

    async def rebalance(self, subject: str) -> dict[str, Any]:
        return await self._call("POST", "/v1/advice/rebalance", subject)

    async def suitability(self, subject: str, proposal: dict[str, Any] | None) -> dict[str, Any]:
        return await self._call("POST", "/v1/compliance/suitability", subject, {"proposal": proposal})

    async def quote(self, subject: str, instrument_id: str = "", quantity: int = 0) -> dict[str, Any]:
        return await self._call("POST", "/v1/orders/quote", subject, {"instrument_id": instrument_id, "quantity": quantity})

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


INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"


def _names(positions: list[dict[str, Any]]) -> str:
    names = [f"the {p['instrument']}" for p in positions]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def resolve_sale(slots: dict[str, Any], positions: list[dict[str, Any]], source: str) -> tuple[dict[str, Any], int] | SkillResult:
    """The position and units the customer asked to sell, or a question back when that is not clear.

    Never falls back to a default holding or quantity: a guess here is an order the customer did not ask for.
    """
    def ask(text: str, missing: str) -> SkillResult:
        return SkillResult(text, [source], "client_confidential", {"missing": missing}, state=INPUT_REQUIRED)

    if not positions:
        return SkillResult("You currently hold no investments with us, so there is nothing to sell.", [source],
                           "client_confidential", {}, state=INPUT_REQUIRED)
    query = str((slots.get("instrument") or {}).get("query", ""))
    if not query:
        return ask(f"Which holding would you like to sell? You hold {_names(positions)}.", "instrument")
    hits = match_instruments(query, positions)
    if not hits:
        return ask(f"I can't find {query} in your portfolio. You hold {_names(positions)}. Which would you like to sell?",
                   "instrument")
    if len(hits) > 1:
        return ask(f"You hold more than one that matches: {_names(hits)}. Which one would you like to sell?", "instrument")
    pos = hits[0]
    quantity = slots.get("quantity")
    if not isinstance(quantity, dict) or not quantity:
        return ask(f"You hold {pos['units']} units of the {pos['instrument']}. How many would you like to sell?", "quantity")
    units = units_for(quantity, int(pos["units"]))
    if units > int(pos["units"]):
        return ask(f"You hold only {pos['units']} units of the {pos['instrument']}. How many would you like to sell?",
                   "quantity")
    return pos, units


def answer_holdings(pf: dict[str, Any], ask: str) -> str:
    """The answer to a portfolio question (``ask`` from the focus slot), or a summary when there is none."""
    positions = pf["positions"]  # largest first
    total = f"CHF {pf['total_value_chf']:,}"
    n = len(positions)
    held = f"{n} position{'s' if n != 1 else ''}"
    top, bottom = positions[0], positions[-1]
    if ask == "accounts":
        return f"You have one portfolio with us, account {pf['account_mask']}, holding {held} worth about {total}."
    if ask == "count":
        return f"You hold {held} worth about {total}."
    if ask == "smallest":
        return (f"Your smallest position is the {bottom['instrument']}: {bottom['units']} units worth about "
                f"CHF {bottom['value_chf']:,}, {_pct(bottom['weight_pct'])} percent of your portfolio.")
    if ask == "largest":
        return (f"Your largest position is the {top['instrument']}: {top['units']} units worth about "
                f"CHF {top['value_chf']:,}, {_pct(top['weight_pct'])} percent of your portfolio.")
    if ask == "total":
        return f"Your portfolio is worth about {total}, across {held}."
    if ask == "list":
        return f"You hold {_names(positions)}, worth about {total} in total."
    return f"You hold {held} worth about {total}; the largest is the {top['instrument']} at {_pct(top['weight_pct'])} percent."


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
        articles = await tools.articles(str(req.data.get("query") or ""))
        if not articles:
            # Nothing in the knowledge base answers it: say so rather than read out unrelated articles.
            return SkillResult("", [], "public", state=INPUT_REQUIRED)
        return SkillResult(" ".join(a["text"] for a in articles), [f"kb://articles/{a['id']}" for a in articles], "public")

    async def holdings(req: SkillRequest) -> SkillResult:
        pf = await tools.portfolio(subject_of(req))
        positions = pf["positions"]
        if not positions:
            return SkillResult("You currently hold no investments with us.", [f"core://positions/{pf['customer_id']}"],
                               "client_confidential", {"positions": []})
        text = answer_holdings(pf, str(((req.data.get("slots") or {}).get("focus") or {}).get("ask", "")))
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
        positions = _first_input(req, "holdings", "positions") or []
        resolved = resolve_sale(req.data.get("slots") or {}, positions, "core://positions/mock")
        if isinstance(resolved, SkillResult):
            return resolved
        pos, units = resolved
        try:
            action = await tools.quote(subject_of(req), str(pos["instrument_id"]), units)
        except ToolError as exc:
            return SkillResult(f"I couldn't prepare that order: {exc}.", [], "internal", state="TASK_STATE_FAILED")
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
        return SkillResult(f"Your order to sell {order['quantity']} units of the {order['instrument']} has been placed, "
                           f"reference {order['reference']}.", [f"core://orders/{order['reference']}"],
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
