"""Temporal implementation of the workflow gateway."""

from __future__ import annotations

import os
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy

from ..config import ConfigError, WorkflowConfig
from ..workflows.transaction import APPROVAL_SIGNAL, TransactionWorkflow
from .temporal_codec import data_converter


async def connect(config: WorkflowConfig, profile: str) -> Client:
    key = os.environ.get(config.payload_key_env, "")
    if profile == "prod" and not key:
        raise ConfigError(f"prod: {config.payload_key_env} must hold a Fernet key to encrypt workflow payloads")
    return await Client.connect(
        config.temporal_target, namespace=config.namespace, tls=config.tls, data_converter=data_converter(key or None)
    )


class TemporalWorkflowGateway:
    def __init__(self, config: WorkflowConfig, profile: str = "dev") -> None:
        self._config = config
        self._profile = profile
        self._client: Client | None = None

    async def _get(self) -> Client:
        if self._client is None:
            self._client = await connect(self._config, self._profile)
        return self._client

    async def start_transaction(self, workflow_id: str, payload: dict[str, Any]) -> None:
        client = await self._get()
        await client.start_workflow(
            TransactionWorkflow.run,
            payload,
            id=workflow_id,
            task_queue=self._config.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def signal_approval(self, workflow_id: str, decision: dict[str, Any]) -> None:
        client = await self._get()
        await client.get_workflow_handle(workflow_id).signal(APPROVAL_SIGNAL, decision)

    async def status(self, workflow_id: str) -> dict[str, Any]:
        client = await self._get()
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        status = desc.status
        result: dict[str, Any] = {"status": status.name.lower() if status else "unknown"}
        if status == WorkflowExecutionStatus.COMPLETED:
            result["result"] = await handle.result()
        return result
