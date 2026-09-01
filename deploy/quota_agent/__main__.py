"""Run the quota-agent server: ``python -m deploy.quota_agent`` (E2.6)."""

from __future__ import annotations

import sys

import uvicorn

from deploy.quota_agent.app import create_app
from deploy.quota_agent.config import Settings


def main() -> None:
    settings = Settings()
    if not settings.token:
        sys.exit(
            "E2B_QUOTA_AGENT_TOKEN is required; refusing to start without auth"
        )
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
