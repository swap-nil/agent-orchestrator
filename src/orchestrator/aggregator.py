"""Merges agent outputs into one grounded answer.

Only the plan's leaf steps (steps nothing else depends on) contribute to the
answer; earlier steps are inputs. For personalised, advice and transaction
intents an artifact without sources is dropped, so no unattributed claim
reaches the user. Artifacts above the allowed classification are dropped too.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Plan, RiskClass, StepResult


@dataclass
class Aggregate:
    text: str
    sources: list[str] = field(default_factory=list)
    dropped: int = 0


def leaf_step_ids(plan: Plan) -> list[str]:
    depended_on = {d for s in plan.steps for d in s.depends_on}
    return [s.id for s in plan.steps if s.id not in depended_on]


def aggregate(plan: Plan, results: dict[str, StepResult], allowed_classifications: list[str]) -> Aggregate:
    texts: list[str] = []
    sources: list[str] = []
    dropped = 0
    require_sources = plan.intent.risk.rank >= RiskClass.R1.rank
    allowed = set(allowed_classifications)
    for step_id in leaf_step_ids(plan):
        result = results.get(step_id)
        if result is None or not result.ok:
            continue
        for artifact in result.artifacts:
            if not artifact.text:
                continue
            if artifact.classification not in allowed or (require_sources and not artifact.sources):
                dropped += 1
                continue
            texts.append(artifact.text.strip())
            for src in artifact.sources:
                if src not in sources:
                    sources.append(src)
    return Aggregate(" ".join(texts), sources, dropped)
