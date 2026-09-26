"""Demo agents with canned answers (used by the local stub server and the contract tests)."""

from __future__ import annotations

from .kit import DomainAgent, SkillRequest, SkillResult


async def faq(req: SkillRequest) -> SkillResult:
    return SkillResult("Our branches are open Monday to Friday, 9:00 to 17:00.", ["kb://branches/opening-hours"], "public")


async def holdings(req: SkillRequest) -> SkillResult:
    return SkillResult(
        "You hold three positions worth about CHF 120,000; the largest is the Tech ETF at 40 percent.",
        ["core://positions/demo"], "client_confidential",
        {"positions": [{"instrument": "Tech ETF", "units": 400}, {"instrument": "CHF Bond Fund", "units": 900}]},
    )


async def quotes(req: SkillRequest) -> SkillResult:
    return SkillResult("The SMI is up 0.4 percent today.", ["mkt://indices/smi"], "public")


async def rebalance(req: SkillRequest) -> SkillResult:
    return SkillResult("Draft: move about 5 percent from equities to bonds.", ["model://allocation/v3"], "client_confidential")


async def suitability(req: SkillRequest) -> SkillResult:
    return SkillResult(
        "Moving about 5 percent from equities to bonds would bring your portfolio back within your agreed risk profile.",
        ["policy://suitability/v7"], "client_confidential",
    )


async def prepare(req: SkillRequest) -> SkillResult:
    return SkillResult("Order prepared.", ["core://quotes/demo"], "client_confidential", {"action": {
        "instrument": "Tech ETF", "quantity": 50, "account_mask": "****1234", "estimated_amount": 6200, "currency": "CHF",
    }})


async def execute(req: SkillRequest) -> SkillResult:
    return SkillResult("Your sell order has been placed.", ["core://orders/demo"], "client_confidential",
                       {"reference": "ORD-" + req.metadata.get("idempotencyKey", "demo")[:8]})


def build_agents() -> dict[str, DomainAgent]:
    return {
        "faq-agent": DomainAgent("faq-agent", {"faq.answer": faq}),
        "portfolio-agent": DomainAgent("portfolio-agent", {"portfolio.holdings": holdings}),
        "market-agent": DomainAgent("market-agent", {"market.quotes": quotes}),
        "advice-agent": DomainAgent("advice-agent", {"advice.rebalance": rebalance}),
        "compliance-agent": DomainAgent("compliance-agent", {"compliance.suitability": suitability}),
        "trade-agent": DomainAgent(
            "trade-agent", {"trade.prepare": prepare, "trade.execute": execute}, write_skills={"trade.execute"}
        ),
    }
