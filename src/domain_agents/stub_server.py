"""Local demo: all six catalogue agents with canned answers, behind one server.

Stands in for the A2A gateway plus domain agents so the full stack runs on a
laptop with no model keys. Run: ``uvicorn domain_agents.stub_server:app --port 8443``
"""

from __future__ import annotations

from .demo_agents import build_agents
from .kit import asgi_app

app = asgi_app(build_agents())
