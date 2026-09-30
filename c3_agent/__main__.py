"""Run the C3 per-node agent: ``python -m c3_agent`` (Task 2)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from c3_agent.app import create_app
from c3_agent.config import Settings


def _configure_logging(settings: Settings) -> int:
    """Make the agent's own INFO logging visible.

    The same arrangement the worker's and the control plane's entry points have
    (``envd_service.__main__._configure_logging``,
    ``control_plane.config.configure_logging``) and for the same reason:
    ``uvicorn.run`` only configures the ``uvicorn*`` loggers, while
    ``c3_agent.*`` inherits a root logger left at WARNING -- so the
    self-heal round's one greppable line ("is the sweep alive?") never appeared,
    and ``E2B_LOG_LEVEL=DEBUG`` was a no-op. ``logging.basicConfig`` is a no-op
    when the root logger already has handlers (e.g. under pytest), so the level
    is pinned explicitly as well. Returns the numeric level applied.
    """
    # An unknown name falls back to INFO (getattr's default) instead of raising
    # the way basicConfig(level="BOGUS") would.
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")
    logging.getLogger().setLevel(level)
    return level


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
    _configure_logging(settings)
    error = _startup_error(settings)
    if error is not None:
        sys.exit(error)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
