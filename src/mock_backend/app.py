"""HTTP API of the fake core-banking system (the tools behind the domain agents).

Reachable only inside the cluster (NetworkPolicy): the domain agents call it
on behalf of the user in ``X-User-Subject``, and the test client reads it so
testers can compare answers with the underlying data.

Run: ``uvicorn mock_backend.app:app --port 8080``
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .bank import MockBank


class QuoteIn(BaseModel):
    instrument_id: str = ""
    quantity: int = Field(default=0, ge=0)


class OrderIn(BaseModel):
    action: dict[str, Any]


class SuitabilityIn(BaseModel):
    proposal: dict[str, Any] | None = None


class AssignIn(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    customer_id: str


def create_app(bank: MockBank | None = None) -> FastAPI:
    bank = bank or MockBank()
    app = FastAPI(title="Mock core banking (test data only)", redoc_url=None)

    def subject_of(value: str) -> str:
        if not value:
            raise HTTPException(400, "X-User-Subject header required")
        return value

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/portfolio")
    async def portfolio(x_user_subject: str = Header(default="")) -> dict[str, Any]:
        return bank.portfolio(subject_of(x_user_subject))

    @app.get("/v1/market/indices")
    async def indices() -> dict[str, Any]:
        return {"indices": bank.indices()}

    @app.get("/v1/market/instruments")
    async def instruments() -> dict[str, Any]:
        return {"instruments": bank.instruments()}

    @app.get("/v1/kb/articles")
    async def articles(q: str = Query(default="", max_length=500)) -> dict[str, Any]:
        return {"articles": bank.articles(q)}

    @app.post("/v1/advice/rebalance")
    async def rebalance(x_user_subject: str = Header(default="")) -> dict[str, Any]:
        return bank.rebalance(subject_of(x_user_subject))

    @app.post("/v1/compliance/suitability")
    async def suitability(body: SuitabilityIn, x_user_subject: str = Header(default="")) -> dict[str, Any]:
        return bank.suitability(subject_of(x_user_subject), body.proposal)

    @app.post("/v1/orders/quote")
    async def quote(body: QuoteIn, x_user_subject: str = Header(default="")) -> dict[str, Any]:
        try:
            return bank.quote_sell(subject_of(x_user_subject), body.instrument_id, body.quantity)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/orders", status_code=201)
    async def place(
        body: OrderIn, x_user_subject: str = Header(default=""), idempotency_key: str = Header(default=""),
    ) -> dict[str, Any]:
        try:
            return bank.place_order(subject_of(x_user_subject), body.action, idempotency_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/orders")
    async def orders(x_user_subject: str = Header(default="")) -> dict[str, Any]:
        return {"orders": bank.orders_for(subject_of(x_user_subject))}

    # Test administration: see which fake customers exist, pin a tester to one, restore the seed.
    @app.get("/v1/admin/customers")
    async def customers() -> dict[str, Any]:
        return {"customers": [{"id": c.id, "name": c.name, "risk_profile": c.risk_profile} for c in bank.customers.values()]}

    @app.post("/v1/admin/assign")
    async def assign(body: AssignIn) -> dict[str, Any]:
        try:
            c = bank.assign(body.subject, body.customer_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"subject": body.subject, "customer_id": c.id}

    @app.post("/v1/admin/reset")
    async def reset() -> dict[str, str]:
        bank.reset()
        return {"status": "reset"}

    return app


app = create_app()
