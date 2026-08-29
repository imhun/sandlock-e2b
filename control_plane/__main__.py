"""Run the control plane: ``python -m control_plane``."""

from __future__ import annotations

import uvicorn

from control_plane.app import create_app
from control_plane.config import Settings


def main() -> None:
    settings = Settings()
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.control_plane_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

