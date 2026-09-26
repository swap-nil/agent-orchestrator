"""Temporal activities for the transaction workflow."""

from __future__ import annotations

from typing import Any

from temporalio import activity

from ..service import OrchestratorService
from .transaction import EXECUTE_WRITE, RECORD_OUTCOME


class TransactionActivities:
    def __init__(self, service: OrchestratorService) -> None:
        self._service = service

    @activity.defn(name=EXECUTE_WRITE)
    async def execute_write(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._service.execute_approved_write(request["payload"], request["approval_token"])

    @activity.defn(name=RECORD_OUTCOME)
    async def record_outcome(self, request: dict[str, Any]) -> None:
        payload = request["payload"]
        await self._service.c.audit.record(
            payload["workflow_id"], f"transaction_{request['outcome']}", request.get("detail", {}),
            session_id=payload["session_id"], turn_id=payload["turn_id"],
        )
