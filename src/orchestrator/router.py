"""Intent routing.

Rule-based routing is always tried first and is the only way to reach advice
(R2) or transaction (R3) intents. An optional model classifier may pick among
information and personalised-read intents (R0, R1) when no rule matches. The
model proposes; the routing table decides.

An intent's ``exclude_patterns`` veto it ("should I sell ..." asks for advice,
it is not a sell order). A request that matches several intents and splits
cleanly on a conjunction ("how is my portfolio and sell half of my SMI
tracker") becomes a compound decision: one segment per intent, lowest risk
first, at most one advice or transaction segment.
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
    # Compound request: (segment text, intent) in execution order.
    segments: tuple[tuple[str, Intent], ...] = ()


_SPLIT_RE = re.compile(
    r"\s*(?:[;,]\s*(?:and\s+|then\s+|also\s+)?|\b(?:and then|and also|and|then|also|plus)\b)\s*", re.IGNORECASE,
)
MAX_SEGMENTS = 3


class Router:
    def __init__(self, catalogue: Catalogue, config: RoutingConfig, model: ModelClassifier | None = None) -> None:
        self._catalogue = catalogue
        self._config = config
        self._model = model
        self._compiled: dict[str, list[re.Pattern[str]]] = {
            intent.id: [re.compile(p, re.IGNORECASE) for p in intent.patterns]
            for intent in catalogue.intents.values()
        }
        self._excluded: dict[str, list[re.Pattern[str]]] = {
            intent.id: [re.compile(p, re.IGNORECASE) for p in intent.exclude_patterns]
            for intent in catalogue.intents.values()
        }
        self._model_risks = {RiskClass(r) for r in config.model_classifier.allowed_risk_classes}

    def _threshold(self, intent: Intent) -> float:
        return self._config.min_confidence.get(intent.risk.value, 1.0)

    def rule_scores(self, text: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for intent_id, patterns in self._compiled.items():
            if any(p.search(text) for p in self._excluded[intent_id]):
                continue
            hits = sum(1 for p in patterns if p.search(text))
            if hits:
                scores[intent_id] = hits
        return scores

    def _unique(self, text: str, disabled: set[str]) -> Intent | None:
        scores = {k: v for k, v in self.rule_scores(text).items() if k not in disabled}
        if not scores:
            return None
        best = max(scores.values())
        top = [k for k, v in scores.items() if v == best]
        return self._catalogue.intents[top[0]] if len(top) == 1 else None

    def _split(self, text: str, disabled: set[str]) -> tuple[tuple[str, Intent], ...]:
        """Split a multi-part request into segments that each route to exactly one intent."""
        parts = [p.strip(" .?!") for p in _SPLIT_RE.split(text) if p and p.strip(" .?!")]
        if not 2 <= len(parts) <= MAX_SEGMENTS + 2:
            return ()
        segments: list[tuple[str, Intent]] = []
        for part in parts:
            intent = self._unique(part, disabled)
            if intent is None:
                if not segments:
                    return ()
                # A fragment without its own intent belongs to the previous segment ("sell my ETF and the gold").
                prev_text, prev_intent = segments[-1]
                segments[-1] = (f"{prev_text} and {part}", prev_intent)
                continue
            if segments and segments[-1][1].id == intent.id:
                segments[-1] = (f"{segments[-1][0]} and {part}", intent)
                continue
            segments.append((part, intent))
        if not 2 <= len(segments) <= MAX_SEGMENTS or len({i.id for _, i in segments}) != len(segments):
            return ()
        if sum(1 for _, i in segments if i.risk.rank >= RiskClass.R2.rank) > 1:
            return ()  # one advice or transaction at a time
        return tuple(sorted(segments, key=lambda seg: seg[1].risk.rank))

    async def route(self, text: str, disabled_intents: set[str] | None = None) -> RoutingDecision:
        disabled_intents = disabled_intents or set()
        all_scores = self.rule_scores(text)
        scores = {k: v for k, v in all_scores.items() if k not in disabled_intents}
        if len(scores) > 1:
            segments = self._split(text, disabled_intents)
            if segments:
                return RoutingDecision(None, 1.0, "rules", False, tuple(i.id for _, i in segments), segments)
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
