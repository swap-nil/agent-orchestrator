"""Orchestrator API and Temporal worker in one process, for local runs.

The test environment runs them as two containers sharing Redis. Locally the
session store is in memory, so the worker must use the same service instance
as the API to find the session when it executes an approved write.

    ORCH_CONFIG_FILE=config/orchestrator.local.yaml python scripts/local/orchestrator_all_in_one.py
"""

from __future__ import annotations

import asyncio

import uvicorn
from temporalio.worker import Worker

from orchestrator.adapters.temporal_gateway import connect
from orchestrator.api.app import create_app
from orchestrator.bootstrap import build_service
from orchestrator.config import load_config
from orchestrator.workflows.activities import TransactionActivities
from orchestrator.workflows.transaction import TransactionWorkflow


async def main() -> None:
    config = load_config()
    service = build_service(config)
    app = create_app(config, service)
    server = uvicorn.Server(uvicorn.Config(app, host=config.server.host, port=config.server.port,
                                           server_header=False, log_level=config.service.log_level.lower()))
    tasks = [asyncio.create_task(server.serve())]
    if config.workflows.enabled:
        acts = TransactionActivities(service)
        worker = Worker(await connect(config.workflows, config.profile), task_queue=config.workflows.task_queue,
                        workflows=[TransactionWorkflow], activities=[acts.execute_write, acts.record_outcome])
        tasks.append(asyncio.create_task(worker.run()))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


if __name__ == "__main__":
    asyncio.run(main())
