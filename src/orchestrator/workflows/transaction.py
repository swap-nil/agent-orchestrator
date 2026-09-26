"""Durable transaction workflow (Temporal).

    prepared -> wait for approval signal (with timeout) -> execute write -> done

This module imports only the standard library and temporalio so it is safe in
Temporal's deterministic workflow sandbox. Activities are referenced by name
and implemented in ``activities.py``.

The write activity is retried only for transient failures. Retries are safe
because every write carries a deterministic idempotency key
(session, turn, step), which domain agents are required to honour.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

APPROVAL_SIGNAL = "approval"
EXECUTE_WRITE = "execute_write"
RECORD_OUTCOME = "record_outcome"

_NON_RETRYABLE = ["PermissionError", "LookupError", "ValueError"]


@workflow.defn(name="TransactionWorkflow")
class TransactionWorkflow:
    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None
        self._status = "awaiting_approval"

    @workflow.signal(name=APPROVAL_SIGNAL)
    def approval(self, decision: dict[str, Any]) -> None:
        # First decision wins; later signals are ignored.
        if self._decision is None:
            self._decision = decision

    @workflow.query(name="status")
    def status(self) -> str:
        return self._status

    async def _record(self, payload: dict[str, Any], outcome: str, detail: dict[str, Any]) -> None:
        await workflow.execute_activity(
            RECORD_OUTCOME,
            {"payload": payload, "outcome": outcome, "detail": detail},
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=10, initial_interval=timedelta(seconds=1)),
        )

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = timedelta(seconds=int(payload.get("approval_timeout_s", 600)))
        try:
            await workflow.wait_condition(lambda: self._decision is not None, timeout=timeout)
        except asyncio.TimeoutError:
            self._status = "expired"
            await self._record(payload, "expired", {})
            return {"status": "expired"}

        decision = self._decision or {}
        if not decision.get("approved") or not decision.get("approval_token"):
            self._status = "declined"
            await self._record(payload, "declined", {})
            return {"status": "declined"}

        self._status = "executing"
        try:
            result = await workflow.execute_activity(
                EXECUTE_WRITE,
                {"payload": payload, "approval_token": decision["approval_token"]},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    non_retryable_error_types=_NON_RETRYABLE,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - activity failure after retries
            self._status = "failed"
            await self._record(payload, "failed", {"error": type(exc).__name__})
            return {"status": "failed"}

        self._status = "completed" if result.get("success") else "failed"
        await self._record(payload, self._status, {"references": result.get("references", [])})
        return {"status": self._status, "references": result.get("references", []), "text": result.get("text", "")}
