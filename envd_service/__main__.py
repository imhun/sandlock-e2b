"""Run the envd service: ``python -m envd_service``."""

from __future__ import annotations

import logging

import uvicorn

from envd_service.app import create_app
from envd_service.config import Settings
from gateway_common.keepalive import uvicorn_keep_alive_kwargs


def _configure_logging(settings: Settings) -> int:
    """Make the worker's own INFO logging visible (F4).

    ``uvicorn.run(log_level=...)`` only configures the ``uvicorn*`` loggers;
    ``envd_service.*`` inherits the root logger, whose default WARNING level
    drops every INFO line -- including the own-identity readiness line
    (``own-identity instance ready ...``) and ``worker image warmed`` -- and makes
    ``E2B_LOG_LEVEL=DEBUG`` a no-op. This entry point is the worker image's own
    process (``CMD ["python", "-m", "envd_service"]``), so raising the root
    level cannot pollute control-plane output.

    ``logging.basicConfig`` is a no-op when the root logger already has
    handlers (e.g. under pytest), so the level is pinned explicitly as well.
    Returns the numeric level applied.
    """
    # An unknown name falls back to INFO (getattr's default) instead of
    # raising the way basicConfig(level="BOGUS") would.
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    logging.getLogger().setLevel(level)
    return level


def main() -> None:
    settings = Settings()
    _configure_logging(settings)
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.envd_port,
        log_level=settings.log_level.lower(),
        **uvicorn_keep_alive_kwargs(),
    )


if __name__ == "__main__":
    main()
