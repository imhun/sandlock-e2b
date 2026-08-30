"""Run the control plane and the envd gateway as ONE app on ONE port.

``python -m control_plane.combined_main`` serves both on
``E2B_CONTROL_PLANE_PORT`` (default 3000) in a single process (used by the
merged ``Dockerfile.control-plane-gateway`` image):

  * control plane API routes (``/v3/...``, ``/internal/...``, ...)
  * envd gateway catch-all proxy for ``E2b-Sandbox-Id`` traffic

The gateway app is mounted at ``/`` AFTER the control plane routes, so API
paths win and everything else (sandbox Connect-RPC / files / health /
metrics with the ``E2b-Sandbox-Id`` header) falls through to the proxy.
Clients point both ``E2B_API_URL`` and ``E2B_SANDBOX_URL`` at the same port.

Route lookup and invalidation stay in-process over localhost HTTP
(``E2B_CONTROL_PLANE_URL`` / ``E2B_GATEWAY_URL`` default to
http://127.0.0.1:<port>).
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    from control_plane.app import create_app
    from control_plane.config import Settings

    settings = Settings()
    port = settings.control_plane_port

    # Route lookup/invalidation through the merged process itself when unset.
    os.environ.setdefault("E2B_CONTROL_PLANE_URL", f"http://127.0.0.1:{port}")
    os.environ.setdefault("E2B_GATEWAY_URL", f"http://127.0.0.1:{port}")

    from envd_service.gateway import create_gateway

    cp_app = create_app(settings=settings)
    gw_app = create_gateway()
    # Mount after the control plane routes: API paths win, the gateway
    # catch-all handles the rest (requests carrying E2b-Sandbox-Id).
    cp_app.mount("/", gw_app)
    # Drop stale routes on this replica when any control plane migrates/kills
    # a sandbox (Redis pub/sub; daemon thread, dies with the process).
    gw_app.state.route_subscriber.start()

    uvicorn.run(
        cp_app,
        host="0.0.0.0",
        port=port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
