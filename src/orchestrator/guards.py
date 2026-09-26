"""Input and output guardrails.

These are deterministic, fast checks that run on every turn. They are one
layer of defence, not the only one: the policy engine, plan validation and the
tool gateway enforce the controls that must never fail. A model-based
classifier (for example an in-region content-safety service) can be plugged in
through :class:`ExternalClassifier`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .config import GuardConfig
from .models import RiskClass

# --------------------------------------------------------------------------- PII

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+|00)\d{2}[\s-]?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\w)|\b0\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    iban = candidate.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Replace emails, IBANs, card numbers and phone numbers. Returns (text, kinds found)."""
    found: list[str] = []

    def sub_iban(m: re.Match[str]) -> str:
        if _iban_ok(m.group(0)):
            found.append("iban")
            return "[IBAN]"
        return m.group(0)

    def sub_card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card")
            return "[CARD]"
        return m.group(0)

    def sub_email(m: re.Match[str]) -> str:
        found.append("email")
        return "[EMAIL]"

    def sub_phone(m: re.Match[str]) -> str:
        found.append("phone")
        return "[PHONE]"

    text = _IBAN.sub(sub_iban, text)
    text = _CARD.sub(sub_card, text)
    text = _EMAIL.sub(sub_email, text)
    text = _PHONE.sub(sub_phone, text)
    return text, found


# --------------------------------------------------------------------------- input guard


class ExternalClassifier(Protocol):
    async def is_unsafe(self, text: str) -> bool: ...


@dataclass
class InputVerdict:
    allowed: bool
    text: str
    redacted_for_log: str
    flags: list[str] = field(default_factory=list)


class InputGuard:
    def __init__(self, config: GuardConfig, classifier: ExternalClassifier | None = None) -> None:
        self._config = config
        self._patterns = [re.compile(p, re.IGNORECASE) for p in config.injection_patterns]
        self._classifier = classifier

    async def check(self, text: str) -> InputVerdict:
        flags: list[str] = []
        cleaned = "".join(ch for ch in text if ch.isprintable() or ch in "\n\t").strip()
        redacted, pii = redact_pii(cleaned)
        if pii:
            flags.append("pii:" + ",".join(sorted(set(pii))))
        if not cleaned:
            return InputVerdict(False, cleaned, redacted, flags + ["empty"])
        if len(cleaned) > self._config.max_input_chars:
            return InputVerdict(False, cleaned, redacted[:200], flags + ["too_long"])
        injection = any(p.search(cleaned) for p in self._patterns)
        if not injection and self._classifier is not None:
            injection = await self._classifier.is_unsafe(cleaned)
        if injection:
            flags.append("prompt_injection")
            if self._config.injection_action == "block":
                return InputVerdict(False, cleaned, redacted, flags)
        return InputVerdict(True, cleaned, redacted, flags)


# --------------------------------------------------------------------------- output guard


@dataclass
class OutputVerdict:
    allowed: bool
    text: str
    flags: list[str] = field(default_factory=list)


class OutputGuard:
    def __init__(self, config: GuardConfig) -> None:
        self._config = config
        self._prohibited = [p.lower() for p in config.prohibited_phrases]
        self._pressure = [p.lower() for p in config.pressure_phrases]

    def check(self, text: str, risk: RiskClass, sources: list[str]) -> OutputVerdict:
        flags: list[str] = []
        lowered = text.lower()
        if any(p in lowered for p in self._prohibited):
            return OutputVerdict(False, text, ["prohibited_phrase"])
        if risk.rank >= RiskClass.R2.rank and any(p in lowered for p in self._pressure):
            return OutputVerdict(False, text, ["pressure_language"])
        if risk.rank >= RiskClass.R1.rank and not sources:
            return OutputVerdict(False, text, ["ungrounded"])
        if risk is RiskClass.R2 and self._config.r2_disclaimer and self._config.r2_disclaimer not in text:
            text = f"{text.rstrip()} {self._config.r2_disclaimer}"
            flags.append("disclaimer_added")
        return OutputVerdict(True, text, flags)
