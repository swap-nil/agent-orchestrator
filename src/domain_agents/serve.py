"""Serve backend-driven domain agents (test environment).

One image, one process per agent in Kubernetes (``AGENT_NAMES=portfolio-agent``),
or all six in one process locally. Configuration from the environment:

    AGENT_NAMES            comma-separated agents to host (default: all)
    MOCK_BACKEND_URL       base URL of the mock core-banking API
    AGENT_TOKEN_ISSUER     expected issuer of delegated tokens, e.g. https://login.microsoftonline.com/<tenant>/v2.0
    AGENT_JWKS_URL         JWKS of that issuer; empty disables token verification (local only)
    AGENT_AUDIENCE         expected audience (the agent's app/client id)
    APPROVAL_PUBLIC_KEY    PEM public key of the orchestrator's approval signer (trade agent)

Run: ``uvicorn domain_agents.serve:app --port 8080``
"""

from __future__ import annotations

import os
from typing import Any

import jwt

from .backend_agents import BankTools, build_backend_agents
from .kit import TokenVerifier, asgi_app


def token_verifier(environ: Any = None) -> TokenVerifier | None:
    env = os.environ if environ is None else environ
    jwks_url = env.get("AGENT_JWKS_URL", "")
    if not jwks_url:
        return None
    audience, issuer = env.get("AGENT_AUDIENCE", ""), env.get("AGENT_TOKEN_ISSUER", "")
    if not (audience and issuer):
        raise RuntimeError("AGENT_AUDIENCE and AGENT_TOKEN_ISSUER are required with AGENT_JWKS_URL")
    jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def verify(token: str) -> dict[str, Any]:
        key = jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=audience, issuer=issuer, leeway=30,
                          options={"require": ["exp", "iat"]})

    return verify


def create_app(environ: Any = None):  # type: ignore[no-untyped-def]
    env = os.environ if environ is None else environ
    agents = build_backend_agents(
        BankTools(env.get("MOCK_BACKEND_URL", "http://localhost:8081")),
        approval_public_key=env.get("APPROVAL_PUBLIC_KEY", ""),
        token_verifier=token_verifier(env),
    )
    names = [n.strip() for n in env.get("AGENT_NAMES", "").split(",") if n.strip()] or list(agents)
    unknown = set(names) - set(agents)
    if unknown:
        raise RuntimeError(f"unknown agents in AGENT_NAMES: {sorted(unknown)}")
    return asgi_app({n: agents[n] for n in names})


app = create_app()
