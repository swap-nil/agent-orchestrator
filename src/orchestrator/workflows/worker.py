"""Temporal worker entry point: ``python -m orchestrator.workflows.worker``."""

from __future__ import annotations

import asyncio
import logging

from temporalio.worker import Worker

from ..adapters.temporal_gateway import connect
from ..bootstrap import build_service
from ..config import load_config
from .activities import TransactionActivities
from .transaction import TransactionWorkflow


async def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.service.log_level)
    service = build_service(config)
    client = await connect(config.workflows, config.profile)
    acts = TransactionActivities(service)
    worker = Worker(
        client,
        task_queue=config.workflows.task_queue,
        workflows=[TransactionWorkflow],
        activities=[acts.execute_write, acts.record_outcome],
        max_concurrent_activities=50,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
