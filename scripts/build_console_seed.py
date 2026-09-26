"""Embed the real configuration into the command center's demo mode.

    PYTHONPATH=src python scripts/build_console_seed.py

The console runs in demo mode when no orchestrator API is reachable (for
example when published as a standalone page). Demo mode simulates the
platform in the browser using the same intent catalogue, agent registry,
guard settings, routing thresholds, messages, eval suite and reference-agent
answers as the repository, so what it shows matches the real behaviour.
tests/test_console_seed.py fails if the embedded seed is out of date.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_agents.demo_agents import build_agents  # noqa: E402
from domain_agents.kit import SkillRequest  # noqa: E402
from orchestrator.config import load_config  # noqa: E402
from orchestrator.console.evals import load_suite  # noqa: E402

import yaml  # noqa: E402

CONSOLE = ROOT / "src" / "orchestrator" / "console" / "static" / "console.html"
START, END = "/*DEMO_SEED_START*/", "/*DEMO_SEED_END*/"


async def agent_answers() -> dict[str, dict]:
    answers = {}
    for agent in build_agents().values():
        for skill, handler in agent._skills.items():  # noqa: SLF001 - build-time introspection of the reference agents
            result = await handler(SkillRequest(skill, "", {}, {"idempotencyKey": "demo0000"}, "demo"))
            answers[skill] = {"text": result.text, "sources": result.sources, "classification": result.classification,
                              "data": result.data, "agent": agent.name}
    return answers


def build_seed() -> dict:
    cfg = load_config(ROOT / "config" / "orchestrator.dev.yaml", environ={})
    return {
        "intents": yaml.safe_load((ROOT / "config" / "intents.yaml").read_text(encoding="utf-8"))["intents"],
        "agents": yaml.safe_load((ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"))["agents"],
        "guards": {k: v for k, v in asdict(cfg.guards).items()},
        "routing": {"min_confidence": cfg.routing.min_confidence, "fallback_intent": cfg.routing.fallback_intent,
                    "max_clarification_rounds": cfg.routing.max_clarification_rounds},
        "budgets": asdict(cfg.budgets),
        "acr_levels": cfg.auth.acr_levels,
        "approval_min_acr": cfg.auth.approval_min_acr,
        "allowed_channels": cfg.policy.allowed_channels,
        "messages": asdict(cfg.messages),
        "alert_rules": [asdict(r) for r in cfg.command_center.alert_rules],
        "change_min_pass_rate": cfg.command_center.change_min_pass_rate,
        "evals": load_suite(str(ROOT / "config" / "evals.yaml")),
        "answers": asyncio.run(agent_answers()),
    }


def render(html: str, seed: dict) -> str:
    blob = json.dumps(seed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    pattern = re.compile(re.escape(START) + ".*?" + re.escape(END), re.S)
    if not pattern.search(html):
        raise SystemExit("seed markers not found in console.html")
    return pattern.sub(lambda _m: START + blob + END, html, count=1)


def main() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    CONSOLE.write_text(render(html, build_seed()), encoding="utf-8")
    print(f"seed embedded into {CONSOLE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
