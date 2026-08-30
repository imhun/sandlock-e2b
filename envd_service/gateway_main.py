"""Run the envd gateway: ``python -m envd_service.gateway_main``."""

from __future__ import annotations

import os

import uvicorn

from envd_service.gateway import create_gateway


def main() -> None:
    app = create_gateway()
    app.state.route_subscriber.start()
    port = int(os.getenv("E2B_GATEWAY_PORT", "49983"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
