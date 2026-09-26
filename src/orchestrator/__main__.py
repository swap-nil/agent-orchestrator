"""Run the orchestrator API: ``python -m orchestrator`` (reads ORCH_CONFIG_FILE)."""

from __future__ import annotations

import uvicorn

from .api.app import create_app
from .config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        proxy_headers=False,
        server_header=False,
        log_level=config.service.log_level.lower(),
        timeout_graceful_shutdown=20,
    )


if __name__ == "__main__":
    main()
