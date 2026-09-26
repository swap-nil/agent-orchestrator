"""Intent routing.

Rule-based routing is always tried first and is the only way to reach advice
(R2) or transaction (R3) intents. An optional model classifier may pick among
information and personalised-read intents (R0, R1) when no rule matches. The
model proposes; the routing table decides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .catalogue import Catalogue
from .config import RoutingConfig
from .models import Intent, RiskClass


class ModelClassifier(Protocol):
    async def classify(self, text: str, candidates: list[Intent]) -> tuple[str | None, float]: ...


@dataclass
class RoutingDecision:
    intent: Intent | None
    confidence: float
    source: str  # rules | model | none
    needs_clarification: bool
    candidates: tuple[str, ...] = ()


class Router:
    def __init__(self, catalogue: Catalogue, config: RoutingConfig, model: ModelClassifier | None = None) -> None:
        self._catalogue = catalogue
        self._config = config
        self._model = model
        self._compiled: dict[str, list[re.Pattern[str]]] = {
            intent.id: [re.compile(p, re.IGNORECASE) for p in intent.patterns]
            for intent in catalogue.intents.values()
        }
        self._model_risks = {RiskClass(r) for r in config.model_classifier.allowed_risk_classes}

    def _threshold(self, intent: Intent) -> float:
        return self._config.min_confidence.get(intent.risk.value, 1.0)

    def _rule_scores(self, text: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for intent_id, patterns in self._compiled.items():
            hits = sum(1 for p in patterns if p.search(text))
            if hits:
                scores[intent_id] = hits
        return scores

    async def route(self, text: str, disabled_intents: set[str] | None = None) -> RoutingDecision:
        disabled_intents = disabled_intents or set()
        all_scores = self._rule_scores(text)
        scores = {k: v for k, v in all_scores.items() if k not in disabled_intents}
        if not scores and all_scores:
            # The request clearly targets a switched-off intent: say so, never fall back to another intent.
            best = max(all_scores, key=lambda k: all_scores[k])
            return RoutingDecision(self._catalogue.intents[best], 1.0, "disabled", False, (best,))
        if scores:
            total = sum(scores.values())
            best_id = max(scores, key=lambda k: (scores[k], -self._catalogue.intents[k].risk.rank))
            ranked = sorted(scores, key=lambda k: -scores[k])
            tied = [k for k in ranked if scores[k] == scores[best_id]]
            intent = self._catalogue.intents[best_id]
            confidence = scores[best_id] / total
            if len(tied) > 1:
                # Ambiguous between intents: never guess, ask.
                return RoutingDecision(None, confidence, "rules", True, tuple(tied))
            needs = confidence < self._threshold(intent)
            return RoutingDecision(intent, confidence, "rules", needs, tuple(ranked))

        if self._model is not None and self._config.model_classifier.enabled:
            candidates = [
                i for i in self._catalogue.intents.values()
                if i.risk in self._model_risks and i.id not in disabled_intents
            ]
            if candidates:
                try:
                    intent_id, confidence = await self._model.classify(text, candidates)
                except Exception:  # noqa: BLE001 - classifier failure must not fail the turn
                    intent_id, confidence = None, 0.0
                intent = self._catalogue.intents.get(intent_id or "")
                if intent is not None and intent in candidates:
                    needs = confidence < self._threshold(intent)
                    return RoutingDecision(intent, confidence, "model", needs, (intent.id,))

        fallback = self._catalogue.intents.get(self._config.fallback_intent) if self._config.fallback_intent else None
        if fallback is not None and fallback.risk is RiskClass.R0 and fallback.id not in disabled_intents:
            return RoutingDecision(fallback, 0.0, "fallback", False, (fallback.id,))
        return RoutingDecision(None, 0.0, "none", True)
