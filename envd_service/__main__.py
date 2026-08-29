"""Run the envd service: ``python -m envd_service``."""

from __future__ import annotations

import uvicorn

from envd_service.app import create_app
from envd_service.config import Settings


def main() -> None:
    settings = Settings()
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.envd_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()

