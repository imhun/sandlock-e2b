"""Run the control plane: ``python -m control_plane``."""

from __future__ import annotations

import uvicorn

from control_plane.app import create_app
from control_plane.config import Settings, configure_logging, uvicorn_ssl_kwargs
from gateway_common.keepalive import uvicorn_keep_alive_kwargs


def main() -> None:
    settings = Settings()
    # N27: the startup pair ``workspace base = ...`` / ``platform state base =
    # ...`` is INFO, so the process has to let its own INFO through first.
    configure_logging(settings)
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.control_plane_port,
        log_level=settings.log_level.lower(),
        **uvicorn_keep_alive_kwargs(),
        **uvicorn_ssl_kwargs(settings),
    )


if __name__ == "__main__":
    main()
