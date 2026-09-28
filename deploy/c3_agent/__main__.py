"""Run the C3 per-node agent: ``python -m deploy.c3_agent`` (Task 2)."""

from __future__ import annotations

import sys

import uvicorn

from deploy.c3_agent.app import create_app
from deploy.c3_agent.config import Settings


def _startup_error(settings: Settings) -> str | None:
    """The one named reason this service must not start, or ``None``.

    Both are hard, not warnings: without a token an exposed port is unguarded,
    and without its own node id the agent cannot make its only local decision
    ("addressed to me?") and would either refuse everything or (worse) accept
    instructions for any node.
    """
    if not settings.token:
        return "E2B_C3_AGENT_TOKEN is required; refusing to start without auth"
    if not settings.node_id:
        return (
            "E2B_C3_AGENT_NODE_ID (or E2B_NODE_ID) is required; the agent "
            "must know which node it is"
        )
    return None


def main() -> None:
    settings = Settings()
    error = _startup_error(settings)
    if error is not None:
        sys.exit(error)
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
