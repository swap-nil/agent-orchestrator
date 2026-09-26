"""Portfolio agent: reference domain agent using Microsoft Agent Framework.

Facts (positions, values) come from the system of record through the tool
layer, deterministically. The model only phrases the summary, and only from
those facts, so every sentence is attributable to the listed sources.

Run: ``uvicorn domain_agents.portfolio_agent.agent:app --port 9001``

Written against Microsoft Agent Framework 1.x (Python). Check class and
parameter names against the version you pin; everything outside
``_phrase`` is framework-independent.
"""

from __future__ import annotations

import json
import os
from typing import Any

import jwt

from ..kit import DomainAgent, SkillRequest, SkillResult, asgi_app

AUDIENCE = os.environ.get("AGENT_AUDIENCE", "api://portfolio-agent")
ISSUER = os.environ.get("AGENT_TOKEN_ISSUER", "")
JWKS_URL = os.environ.get("AGENT_JWKS_URL", "")


async def fetch_positions(subject: str, tenant: str) -> list[dict[str, Any]]:
    """Replace with a call through the tool gateway (MCP, OAuth 2.1, delegated token)."""
    return [
        {"instrument": "Tech ETF", "units": 400, "value_chf": 48_000},
        {"instrument": "CHF Bond Fund", "units": 900, "value_chf": 54_000},
        {"instrument": "Gold ETC", "units": 120, "value_chf": 18_000},
    ]


async def _phrase(positions: list[dict[str, Any]], locale: str) -> str:
    """Use an in-region model to phrase a two-sentence summary from the facts only."""
    try:
        from agent_framework import Agent
        from agent_framework.azure import AzureOpenAIChatClient
    except ImportError:
        total = sum(p["value_chf"] for p in positions)
        return f"You hold {len(positions)} positions worth about CHF {total:,}."
    agent = Agent(
        client=AzureOpenAIChatClient(),  # endpoint/deployment from AZURE_OPENAI_* env vars (Switzerland North)
        name="portfolio-summariser",
        instructions=(
            "Summarise the holdings in at most two short spoken sentences in the user's language. "
            "Use only the numbers provided. No advice, no predictions."
        ),
    )
    result = await agent.run(f"locale={locale}\nholdings={json.dumps(positions)}")
    return str(result.text).strip()


async def holdings(req: SkillRequest) -> SkillResult:
    subject = str(req.claims.get("oid") or req.claims.get("sub") or "demo")
    tenant = str(req.metadata.get("tenant", ""))
    positions = await fetch_positions(subject, tenant)
    text = await _phrase(positions, str(req.data.get("locale", "en-CH")))
    return SkillResult(text, ["core://positions/" + tenant], "client_confidential", {"positions": positions})


def _verifier():  # type: ignore[no-untyped-def]
    if not JWKS_URL:
        return None
    jwks = jwt.PyJWKClient(JWKS_URL)

    def verify(token: str) -> dict[str, Any]:
        key = jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=AUDIENCE, issuer=ISSUER or None)

    return verify


app = asgi_app({"portfolio-agent": DomainAgent("portfolio-agent", {"portfolio.holdings": holdings}, token_verifier=_verifier())})
