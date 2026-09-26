"""Fake core-banking system for the test environment.

A seeded, deterministic dataset (customers, accounts, positions, instruments,
indices, knowledge base) plus the business logic the domain agents call as
tools: valuation, rebalancing, suitability, order quotes and order placement.
Everything is invented; no real person, account or security is represented.

Signed-in users are mapped to a fake customer: by explicit assignment
(``assign``), otherwise by a stable hash of their subject (Entra ``oid``), so
the same tester always sees the same portfolio. Orders change positions, so a
sale shows up in the next holdings answer. State is in memory; ``reset``
restores the seed.
"""

from __future__ import annotations

import copy
import hashlib
import math
import random
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SEED = 20260926
CURRENCY = "CHF"


@dataclass(frozen=True)
class Instrument:
    id: str
    name: str
    asset_class: str  # equity | bond | commodity | real_estate
    base_price: float


@dataclass(frozen=True)
class Index:
    id: str
    name: str
    base_level: float


@dataclass
class Customer:
    id: str
    name: str
    risk_profile: str  # conservative | balanced | growth
    account_mask: str
    positions: dict[str, int] = field(default_factory=dict)  # instrument id -> units


INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("TECH-ETF", "Tech ETF", "equity", 120.0),
    Instrument("SMI-TRK", "SMI Tracker Fund", "equity", 118.5),
    Instrument("GLOB-EQ", "Global Equity Fund", "equity", 86.2),
    Instrument("EM-ETF", "Emerging Markets ETF", "equity", 41.7),
    Instrument("NESN", "Nestle registered share", "equity", 88.4),
    Instrument("NOVN", "Novartis registered share", "equity", 91.3),
    Instrument("ROG", "Roche participation certificate", "equity", 262.0),
    Instrument("CHF-BOND", "CHF Bond Fund", "bond", 60.0),
    Instrument("GREEN-BOND", "Green Bond Fund", "bond", 97.8),
    Instrument("CORP-BOND", "Swiss Corporate Bond Fund", "bond", 102.4),
    Instrument("GOLD-ETC", "Gold ETC", "commodity", 150.0),
    Instrument("RE-FUND", "Swiss Real Estate Fund", "real_estate", 135.6),
)
INDICES: tuple[Index, ...] = (
    Index("SMI", "SMI", 12_150.0),
    Index("SPI", "SPI", 16_020.0),
    Index("SX5E", "Euro Stoxx 50", 5_310.0),
    Index("SPX", "S&P 500", 6_480.0),
)
TARGETS: dict[str, dict[str, float]] = {
    "conservative": {"equity": 0.25, "bond": 0.55, "commodity": 0.10, "real_estate": 0.10},
    "balanced": {"equity": 0.45, "bond": 0.35, "commodity": 0.10, "real_estate": 0.10},
    "growth": {"equity": 0.70, "bond": 0.15, "commodity": 0.05, "real_estate": 0.10},
}
# How far an asset class may drift from target before the portfolio is outside its profile.
TOLERANCE = 0.05
KNOWLEDGE_BASE: tuple[dict[str, str], ...] = (
    {"id": "opening-hours", "title": "Branch opening hours",
     "text": "Our branches are open Monday to Friday, 9:00 to 17:00, and Thursdays until 18:30."},
    {"id": "contact", "title": "How to reach us",
     "text": "You can reach the service desk 24 hours a day on 0800 000 000 or through secure messaging in e-banking."},
    {"id": "custody-fees", "title": "Custody fees",
     "text": "Custody fees are 0.25 percent a year of the portfolio value, at least CHF 60, charged quarterly."},
    {"id": "card-lost", "title": "Lost or stolen card",
     "text": "Block a lost card immediately in the mobile app under Cards, or call the service desk."},
    {"id": "ebanking-reset", "title": "E-banking password reset",
     "text": "Reset your e-banking password with the mobile app's identity check; it takes about two minutes."},
    {"id": "savings-rate", "title": "Savings interest rate",
     "text": "The savings account currently pays 0.5 percent interest a year on balances up to CHF 100,000."},
    {"id": "intl-transfer", "title": "International transfers",
     "text": "SEPA transfers in euro are free; other international transfers cost CHF 5 and arrive in one to three days."},
)
FEATURED_ARTICLES = ("opening-hours", "contact")

_FIRST = ("Anna", "Luca", "Sara", "Marco", "Lea", "Noah", "Mia", "Elias", "Laura", "Jonas", "Nina", "David")
_LAST = ("Meier", "Keller", "Brunner", "Rossi", "Weber", "Huber", "Schmid", "Frei", "Baumann", "Graf", "Fischer", "Moser")


def _seed_customers() -> dict[str, Customer]:
    rng = random.Random(SEED)  # noqa: S311 - reproducible fake data, not security
    profiles = ("conservative", "balanced", "growth")
    customers: dict[str, Customer] = {}
    for i in range(12):
        profile = profiles[i % 3]
        held = rng.sample(INSTRUMENTS, rng.randint(3, 6))
        # Portfolio of roughly CHF 40k to 400k, deliberately off target so rebalancing has something to say.
        budget = rng.choice((40_000, 75_000, 120_000, 220_000, 400_000))
        weights = [rng.uniform(0.5, 2.0) for _ in held]
        total = sum(weights)
        positions = {ins.id: max(1, int(budget * w / total / ins.base_price)) for ins, w in zip(held, weights, strict=True)}
        customers[f"C{1001 + i}"] = Customer(
            id=f"C{1001 + i}", name=f"{_FIRST[i]} {_LAST[i]}", risk_profile=profile,
            account_mask=f"****{rng.randint(1000, 9999)}", positions=positions,
        )
    return customers


def _drift(key: str, now: float, period_s: float = 5400.0, amplitude: float = 0.012) -> float:
    """Deterministic intraday movement so prices look alive but are reproducible."""
    phase = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 2 * math.pi
    return amplitude * math.sin(2 * math.pi * now / period_s + phase)


class MockBank:
    def __init__(self, clock: Any = time.time) -> None:
        self._clock = clock
        self._instruments = {i.id: i for i in INSTRUMENTS}
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self.customers = _seed_customers()
        self.assignments: dict[str, str] = {}
        self.orders: list[dict[str, Any]] = []
        self._orders_by_key: dict[str, dict[str, Any]] = {}

    def customer_for(self, subject: str) -> Customer:
        if not subject:
            raise KeyError("subject required")
        cid = self.assignments.get(subject)
        if cid is None:
            ids = sorted(self.customers)
            cid = ids[int(hashlib.sha256(subject.encode()).hexdigest(), 16) % len(ids)]
        return self.customers[cid]

    def assign(self, subject: str, customer_id: str) -> Customer:
        if customer_id not in self.customers:
            raise KeyError(f"unknown customer {customer_id}")
        self.assignments[subject] = customer_id
        return self.customers[customer_id]

    # ------------------------------------------------------------------ market

    def price(self, instrument_id: str) -> float:
        ins = self._instruments[instrument_id]
        return round(ins.base_price * (1 + _drift(ins.id, self._clock())), 2)

    def instruments(self) -> list[dict[str, Any]]:
        return [{**asdict(i), "price": self.price(i.id), "currency": CURRENCY} for i in INSTRUMENTS]

    def indices(self) -> list[dict[str, Any]]:
        out = []
        for idx in INDICES:
            change = _drift(idx.id, self._clock(), amplitude=0.009)
            out.append({"id": idx.id, "name": idx.name, "level": round(idx.base_level * (1 + change), 1),
                        "change_pct": round(change * 100, 2)})
        return out

    # ------------------------------------------------------------------ portfolio

    def portfolio(self, subject: str) -> dict[str, Any]:
        c = self.customer_for(subject)
        positions = []
        for iid, units in sorted(c.positions.items()):
            if units <= 0:
                continue
            ins = self._instruments[iid]
            price = self.price(iid)
            positions.append({"instrument_id": iid, "instrument": ins.name, "asset_class": ins.asset_class,
                              "units": units, "price": price, "value_chf": round(units * price)})
        total = sum(p["value_chf"] for p in positions)
        for p in positions:
            p["weight_pct"] = round(p["value_chf"] / total * 100, 1) if total else 0.0
        positions.sort(key=lambda p: p["value_chf"], reverse=True)
        return {"customer_id": c.id, "name": c.name, "risk_profile": c.risk_profile, "account_mask": c.account_mask,
                "currency": CURRENCY, "total_value_chf": total, "positions": positions,
                "allocation": self._allocation(positions, total)}

    @staticmethod
    def _allocation(positions: list[dict[str, Any]], total: float) -> dict[str, float]:
        alloc = {k: 0.0 for k in ("equity", "bond", "commodity", "real_estate")}
        for p in positions:
            alloc[p["asset_class"]] += p["value_chf"]
        return {k: round(v / total, 4) if total else 0.0 for k, v in alloc.items()}

    # ------------------------------------------------------------------ advice

    def rebalance(self, subject: str) -> dict[str, Any]:
        pf = self.portfolio(subject)
        target = TARGETS[pf["risk_profile"]]
        gaps = {k: pf["allocation"][k] - target[k] for k in target}
        over = max(gaps, key=lambda k: gaps[k])
        under = min(gaps, key=lambda k: gaps[k])
        shift = round(min(gaps[over], -gaps[under]) * 100)
        if shift < 1:
            return {"needed": False, "risk_profile": pf["risk_profile"], "allocation": pf["allocation"], "target": target,
                    "text": "Your allocation is already within your agreed risk profile; no change is needed."}
        amount = round(pf["total_value_chf"] * shift / 100, -2)
        return {
            "needed": True, "risk_profile": pf["risk_profile"], "allocation": pf["allocation"], "target": target,
            "proposal": {"from": over, "to": under, "shift_pct": shift, "amount_chf": amount},
            "text": (f"Draft: move about {shift} percent (about CHF {amount:,.0f}) from {over.replace('_', ' ')} "
                     f"to {under.replace('_', ' ')}."),
        }

    def suitability(self, subject: str, proposal: dict[str, Any] | None) -> dict[str, Any]:
        """A proposal is suitable when it moves both asset classes it touches toward target without overshooting."""
        pf = self.portfolio(subject)
        target = TARGETS[pf["risk_profile"]]
        before, after = pf["allocation"], dict(pf["allocation"])
        if proposal:
            shift = float(proposal.get("shift_pct", 0)) / 100
            after[proposal["from"]] = after.get(proposal["from"], 0) - shift
            after[proposal["to"]] = after.get(proposal["to"], 0) + shift
        touched = [proposal["from"], proposal["to"]] if proposal else []
        suitable = bool(touched) and all(
            abs(after[k] - target[k]) < abs(before[k] - target[k]) + 1e-9
            and (after[k] - target[k]) * (before[k] - target[k]) >= -1e-9  # no overshoot past target
            for k in touched
        )
        remaining = [k for k in target if abs(after[k] - target[k]) > TOLERANCE]
        reference = "SUIT-" + hashlib.sha256(f"{pf['customer_id']}|{sorted(after.items())}".encode()).hexdigest()[:8].upper()
        if not proposal:
            text = "There is no proposal to check."
        elif not suitable:
            text = "This change would not suit your agreed risk profile. An advisor can review the options with you."
        else:
            outcome = "back within" if not remaining else "closer to"
            text = (f"Moving about {proposal['shift_pct']} percent from {proposal['from'].replace('_', ' ')} to "
                    f"{proposal['to'].replace('_', ' ')} would bring your portfolio {outcome} your agreed "
                    f"{pf['risk_profile']} risk profile.")
        return {"suitable": suitable, "remaining_drift": remaining, "reference": reference, "text": text}

    # ------------------------------------------------------------------ orders

    def quote_sell(self, subject: str, instrument_id: str = "", quantity: int = 0) -> dict[str, Any]:
        """Prepare (not place) a sell order. Without a choice: 10 percent of the largest position."""
        pf = self.portfolio(subject)
        if not pf["positions"]:
            raise ValueError("no positions to sell")
        pos = next((p for p in pf["positions"] if p["instrument_id"] == instrument_id), None) if instrument_id else None
        pos = pos or pf["positions"][0]
        qty = quantity or max(1, round(pos["units"] * 0.1))
        if qty > pos["units"]:
            raise ValueError("quantity exceeds the position")
        return {"side": "sell", "instrument_id": pos["instrument_id"], "instrument": pos["instrument"], "quantity": qty,
                "account_mask": pf["account_mask"], "estimated_amount": round(qty * pos["price"]), "currency": CURRENCY}

    def place_order(self, subject: str, action: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("idempotency key required")
        if idempotency_key in self._orders_by_key:
            return self._orders_by_key[idempotency_key]
        c = self.customer_for(subject)
        iid, qty = str(action.get("instrument_id", "")), int(action.get("quantity", 0))
        if action.get("side") != "sell" or iid not in self._instruments or qty <= 0:
            raise ValueError("invalid order")
        if c.positions.get(iid, 0) < qty:
            raise ValueError("insufficient units")
        c.positions[iid] -= qty
        order = {"reference": "ORD-" + secrets.token_hex(4).upper(), "status": "accepted", "customer_id": c.id,
                 "side": "sell", "instrument_id": iid, "instrument": self._instruments[iid].name, "quantity": qty,
                 "price": self.price(iid), "currency": CURRENCY, "placed_at": int(self._clock())}
        self.orders.append(order)
        self._orders_by_key[idempotency_key] = order
        return order

    def orders_for(self, subject: str) -> list[dict[str, Any]]:
        cid = self.customer_for(subject).id
        return [copy.deepcopy(o) for o in self.orders if o["customer_id"] == cid]

    # ------------------------------------------------------------------ knowledge base

    def articles(self, query: str = "") -> list[dict[str, str]]:
        words = {w for w in query.lower().split() if len(w) > 2}
        if not words:
            return [a for a in KNOWLEDGE_BASE if a["id"] in FEATURED_ARTICLES]
        scored = [(sum(w in (a["title"] + " " + a["text"]).lower() for w in words), a) for a in KNOWLEDGE_BASE]
        return [a for score, a in sorted(scored, key=lambda s: -s[0]) if score > 0][:3]
