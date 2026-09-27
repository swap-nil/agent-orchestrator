"""Deterministic slot extraction and instrument matching.

Intents may declare slots (``intents.yaml``). The orchestrator fills them from
the user's words with fixed rules, never a model, and passes them to agents as
structured data (``data.slots``); agents still never receive the raw text.
Values are short, sanitised phrases or numbers, so they cannot carry
instructions.

Kinds:

* ``instrument``: the phrase naming a holding ("the SMI tracker fund"), kept as
  ``{"query": "SMI tracker fund"}``. Agents resolve it against the customer's
  holdings with :func:`match_instruments`; the orchestrator re-checks the
  prepared action with :func:`instrument_matches`.
* ``quantity``: ``{"units": 50}``, ``{"fraction": 0.5}`` ("half", "25 percent")
  or ``{"fraction": 1.0}`` ("all", "everything").
* ``focus``: what a portfolio question is about, from a fixed vocabulary:
  ``{"ask": "accounts" | "count" | "smallest" | "largest" | "total" | "list"}``
  ("how many portfolios", "my smallest position", "what is it worth"). Absent
  for an open question ("how is my portfolio doing").

When the user answers a follow-up question ("which holding?"), the whole reply
is the value, so :func:`extract_slots` takes the slot being asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SLOT_KINDS = ("instrument", "quantity", "focus")
MAX_QUERY_CHARS = 60


@dataclass(frozen=True)
class SlotSpec:
    name: str
    kind: str
    required: bool = False
    prompt: str = ""


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty five": 25, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100,
}
_FRACTIONS = {
    "half": 0.5, "a half": 0.5, "one half": 0.5, "a third": 1 / 3, "one third": 1 / 3, "third": 1 / 3,
    "a quarter": 0.25, "one quarter": 0.25, "quarter": 0.25, "two thirds": 2 / 3, "three quarters": 0.75,
}
_ALL = (r"all|everything|the (entire|whole) (position|holding|lot)|(my|the) (entire|whole|full) (position|holding)|"
        r"all of (it|them)")
_NUM = r"\d+(?:[.,]\d+)?|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True))
_FRAC = "|".join(sorted(map(re.escape, _FRACTIONS), key=len, reverse=True))
_UNIT_WORDS = r"units?|shares?|pieces?|parts?"

_PERCENT_RE = re.compile(rf"\b({_NUM})\s*(%|percent|per cent)", re.IGNORECASE)
_FRACTION_RE = re.compile(rf"\b({_FRAC})\b", re.IGNORECASE)
_ALL_RE = re.compile(rf"\b({_ALL})\b", re.IGNORECASE)
_UNITS_RE = re.compile(rf"\b({_NUM})\s*(?:{_UNIT_WORDS})?\b(?!\s*(%|percent|per cent))", re.IGNORECASE)

# Words stripped from an instrument phrase: verbs, quantities, determiners, politeness.
_QUANTITY_PHRASE_RE = re.compile(
    rf"\b(({_NUM})\s*(%|percent|per cent)|({_FRAC})|({_ALL})|({_NUM})\s*(?:{_UNIT_WORDS})?|some|a few|a bit|part|portion)\b"
    r"(\s+of)?",
    re.IGNORECASE,
)
_LEADING_RE = re.compile(
    r"^(?:(?:please|ok(?:ay)?|so|and|then|also|now|i want to|i'd like to|i would like to|can you|could you|"
    r"would you|let's|lets|go ahead and|just)\s+)*(?:sell|sell off|get rid of|dump|liquidate|offload)?\s*",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"\b(?:my|the|our|of|units?|shares?|pieces?|position in|holding in|holdings? of|in|from|out of|please|"
    r"for me|now|today|right away|immediately|at market( price)?|the one|one|then|instead|actually|rather|maybe|"
    r"thanks|thank you|ok(?:ay)?|yes|yeah|sure|let's go with|go with|how about|i mean)\b",
    re.IGNORECASE,
)
_TRAILING_RE = re.compile(r"\b(?:from|in|out of) (?:my|the) (?:portfolio|account|depot|holdings)\b.*$", re.IGNORECASE)

# Words that describe an instrument type rather than name one: "the fund" alone is ambiguous.
GENERIC_TOKENS = frozenset({
    "etf", "etfs", "fund", "funds", "share", "shares", "stock", "stocks", "registered", "participation",
    "certificate", "etc", "tracker", "position", "holding", "units", "unit", "swiss",
})
_STOP_TOKENS = frozenset({"the", "my", "a", "an", "of", "in", "and", "one", "that", "this", "it"})
_PLACEHOLDER_TOKENS = frozenset({"position", "holding", "investment", "asset", "stuff", "thing", "some", "something"})


def _number(raw: str) -> float | None:
    raw = raw.strip().lower()
    if raw in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[raw])
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def extract_quantity(text: str, *, bare: bool = False) -> dict[str, Any] | None:
    """Quantity in ``text``. ``bare`` accepts a lone number ("20") as units, used when answering "how much?"."""
    m = _PERCENT_RE.search(text)
    if m:
        value = _number(m.group(1))
        if value is not None and 0 < value <= 100:
            return {"fraction": round(value / 100, 4)}
    m = _FRACTION_RE.search(text)
    if m:
        return {"fraction": round(_FRACTIONS[m.group(1).lower()], 4)}
    if _ALL_RE.search(text):
        return {"fraction": 1.0}
    for m in _UNITS_RE.finditer(text):
        unit_word = re.search(rf"\b({_NUM})\s*(?:{_UNIT_WORDS})\b", m.group(0), re.IGNORECASE)
        if unit_word or bare:
            value = _number(m.group(1))
            if value is not None and value > 0 and value == int(value):
                return {"units": int(value)}
    return None


def _clean(phrase: str) -> str:
    phrase = re.sub(r"[^\w\s&'-]", " ", phrase)
    phrase = _TRAILING_RE.sub("", phrase)
    phrase = _QUANTITY_PHRASE_RE.sub(" ", phrase)
    phrase = _FILLER_RE.sub(" ", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip(" -'")
    return phrase[:MAX_QUERY_CHARS].strip()


def extract_instrument(text: str, *, bare: bool = False) -> dict[str, Any] | None:
    """The instrument phrase after a sell verb ("sell half of my SMI tracker" -> "SMI tracker").

    ``bare`` treats the whole reply as the phrase ("the Novartis one"), used when answering "which holding?".
    """
    m = re.search(r"\b(?:sell(?: off)?|get rid of|dump|liquidate|offload)\b(.*)$", text, re.IGNORECASE)
    if m:
        phrase = m.group(1)
    elif bare:
        phrase = _LEADING_RE.sub("", text)
    else:
        return None
    query = _clean(phrase)
    words = set(tokens(query))
    # "sell my position" names no holding: leave the slot empty so the user is asked which one.
    return {"query": query} if words and not words <= _PLACEHOLDER_TOKENS else None


# Most specific first: "how many ... is my largest" asks about the largest.
_FOCUS = (
    ("smallest", re.compile(r"\b(smallest|lowest|least valuable|tiniest)\b", re.IGNORECASE)),
    ("largest", re.compile(r"\b(largest|biggest|highest|most valuable|top holding|main holding)\b", re.IGNORECASE)),
    ("accounts", re.compile(r"\bhow many (portfolios|accounts|depots)\b", re.IGNORECASE)),
    ("count", re.compile(r"\bhow many\b", re.IGNORECASE)),
    ("list", re.compile(r"\b(what do i (own|hold)|list|which (positions|holdings|investments))\b", re.IGNORECASE)),
    ("total", re.compile(r"\b(total|worth|how much (is|are) my)\b", re.IGNORECASE)),
)


def extract_focus(text: str) -> dict[str, Any] | None:
    """What a portfolio question asks about, or None for an open "how is it doing"."""
    return next(({"ask": ask} for ask, pattern in _FOCUS if pattern.search(text)), None)


def extract_slots(specs: tuple[SlotSpec, ...], text: str, asking: str = "") -> dict[str, Any]:
    """Fill the declared slots from ``text``. ``asking`` names the slot a follow-up question asked for."""
    found: dict[str, Any] = {}
    for spec in specs:
        bare = spec.name == asking
        if spec.kind == "quantity":
            value = extract_quantity(text, bare=bare)
        elif spec.kind == "instrument":
            value = extract_instrument(text, bare=bare)
        elif spec.kind == "focus":
            value = extract_focus(text)
        else:
            value = None
        if value is not None:
            found[spec.name] = value
    return found


def missing_required(specs: tuple[SlotSpec, ...], slots: dict[str, Any]) -> SlotSpec | None:
    return next((s for s in specs if s.required and s.name not in slots), None)


def describe(slots: dict[str, Any]) -> dict[str, str]:
    """Plain values for prompt templates: {instrument} and {quantity}."""
    out: dict[str, str] = {}
    for name, value in slots.items():
        if not isinstance(value, dict):
            continue
        if "query" in value:
            out[name] = str(value["query"])
        elif "units" in value:
            out[name] = f"{value['units']} units"
        elif value.get("fraction") == 1.0:
            out[name] = "all"
        elif "fraction" in value:
            out[name] = f"{round(float(value['fraction']) * 100)} percent"
    return out


# ------------------------------------------------------------------ instrument matching


def _stem(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("é", "e").replace("è", "e").replace("ä", "a")
                       .replace("ö", "o").replace("ü", "u"))
    return [_stem(w) for w in words if w not in _STOP_TOKENS]


def instrument_matches(query: str, name: str, instrument_id: str = "") -> bool:
    """True when every distinctive word of ``query`` appears in the instrument's name or id.

    Only generic words ("the fund") match any instrument of that type.
    """
    q = tokens(query)
    if not q:
        return False
    hay = set(tokens(name)) | set(tokens(instrument_id.replace("-", " "))) | {instrument_id.lower()}
    distinctive = [t for t in q if _stem(t) not in {_stem(g) for g in GENERIC_TOKENS}]
    needed = distinctive or q
    return all(t in hay for t in needed)


def match_instruments(query: str, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Positions whose instrument matches ``query`` (see :func:`instrument_matches`).

    When several match, generic words in the query ("fund" vs "ETF") break the tie.
    """
    hits = [p for p in positions if instrument_matches(query, str(p.get("instrument", "")), str(p.get("instrument_id", "")))]
    if len(hits) > 1:
        q = set(tokens(query))
        best = max(len(q & set(tokens(str(p.get("instrument", ""))))) for p in hits)
        hits = [p for p in hits if len(q & set(tokens(str(p.get("instrument", ""))))) == best]
    return hits


def units_for(quantity: dict[str, Any], held: int) -> int:
    """Units to sell for a quantity slot, given the units held."""
    if "units" in quantity:
        return int(quantity["units"])
    fraction = float(quantity.get("fraction", 0))
    return held if fraction >= 1.0 else max(1, round(held * fraction))
