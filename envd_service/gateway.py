"""Envd Gateway: single entry point routing SDK traffic to the node holding
each sandbox (``E2b-Sandbox-Id`` -> node), preserving Connect streaming and
HTTP semantics."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from gateway_common import GATEWAY_ROUTE_INVALIDATE_CHANNEL

logger = logging.getLogger(__name__)


class RouteCache:
    def __init__(self, ttl: float = 30.0) -> None:
        self._routes: dict[str, tuple[float, str]] = {}
        self._ttl = ttl

    def get(self, sandbox_id: str) -> str | None:
        entry = self._routes.get(sandbox_id)
        if entry is None:
            return None
        fetched_at, address = entry
        if time.time() - fetched_at > self._ttl:
            self._routes.pop(sandbox_id, None)
            return None
        return address

    def put(self, sandbox_id: str, address: str) -> None:
        self._routes[sandbox_id] = (time.time(), address)

    def invalidate(self, sandbox_id: str) -> None:
        self._routes.pop(sandbox_id, None)


def _route_ttl() -> float:
    """Route cache TTL from ``E2B_GATEWAY_ROUTE_TTL`` (seconds, >= 0.5)."""
    try:
        return max(0.5, float(os.getenv("E2B_GATEWAY_ROUTE_TTL", "30.0")))
    except ValueError:
        return 30.0


class RouteInvalidationSubscriber:
    """Drop cached routes on every replica via Redis pub/sub.

    The control plane publishes the sandbox id on
    ``GATEWAY_ROUTE_INVALIDATE_CHANNEL`` after migration/kill; this subscriber
    listens and invalidates the local ``RouteCache`` immediately, closing the
    stale-route window that the per-replica HTTP invalidation cannot cover.
    Best-effort: if Redis is unavailable the TTL still bounds staleness.
    """

    def __init__(
        self,
        routes: RouteCache,
        redis_url: str | None = None,
        client=None,
    ) -> None:
        self._routes = routes
        self._redis_url = redis_url
        self._client = client  # injected client (tests); None -> from URL
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._redis_url and self._client is None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="gateway-route-invalidate",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._listen()
            except Exception:
                logger.exception("gateway route invalidation subscriber error")
            self._stop.wait(2.0)

    def _listen(self) -> None:
        client = self._client
        pubsub = None
        try:
            if client is None:
                import redis

                client = redis.from_url(self._redis_url, decode_responses=True)
            pubsub = client.pubsub()
            pubsub.subscribe(GATEWAY_ROUTE_INVALIDATE_CHANNEL)
            while not self._stop.is_set():
                message = pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                sandbox_id = message.get("data")
                if isinstance(sandbox_id, bytes):
                    sandbox_id = sandbox_id.decode()
                if sandbox_id:
                    self._routes.invalidate(sandbox_id)
        finally:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass


def create_gateway(
    *,
    control_plane_url: str | None = None,
    internal_api_key: str | None = None,
    internal_api_keys: tuple[str, ...] | None = None,
) -> FastAPI:
    control_url = (
        control_plane_url or os.getenv("E2B_CONTROL_PLANE_URL", "")
    ).rstrip("/")
    keys: list[str] = []
    if internal_api_keys:
        keys.extend(internal_api_keys)
    single = internal_api_key or os.getenv(
        "E2B_INTERNAL_API_KEY", "internal-key"
    )
    if single:
        keys.append(single)
    for env_key in os.getenv("E2B_INTERNAL_API_KEYS", "").split(","):
        if env_key.strip():
            keys.append(env_key.strip())
    internal_keys = tuple(dict.fromkeys(keys))
    internal_key = internal_keys[0] if internal_keys else "internal-key"
    routes = RouteCache(ttl=_route_ttl())

    # Merged TLS mode (control_plane.combined_main) uses a self-signed
    # loopback cert for in-process route lookups; skip verification only for
    # https loopback URLs so remote https control-plane URLs keep the default
    # verified transport.
    control_verify = not (
        control_url.startswith("https://127.0.0.1")
        or control_url.startswith("https://localhost")
    )

    app = FastAPI(title="E2B Sandlock Gateway - Envd Router")
    app.state.route_cache = routes
    app.state.route_subscriber = RouteInvalidationSubscriber(
        routes, redis_url=os.getenv("E2B_REDIS_URL")
    )

    @app.post("/internal/routes/{sandbox_id}/invalidate")
    async def invalidate_route(sandbox_id: str, request: Request) -> Response:
        provided = request.headers.get("X-Internal-Key")
        if provided is None or not any(
            secrets.compare_digest(provided, candidate)
            for candidate in internal_keys
        ):
            return Response(status_code=401)
        routes.invalidate(sandbox_id)
        return Response(status_code=204)

    async def _resolve_address(sandbox_id: str) -> str:
        cached = routes.get(sandbox_id)
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=10, verify=control_verify) as client:
            resp = await client.get(
                f"{control_url}/internal/routes/{sandbox_id}",
                headers={"X-Internal-Key": internal_key},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"route lookup failed: {resp.status_code}")
            address = resp.json().get("address")
        if not address or address == "local://":
            raise RuntimeError("sandbox has no remote node address")
        routes.put(sandbox_id, address)
        return address

    _FORWARD_HEADERS = {
        "content-type",
        "authorization",
        "x-access-token",
        "e2b-sandbox-id",
        "e2b-sandbox-port",
        "accept",
        "connect-protocol-version",
        "x-mcp-access-token",
        # MCP streamable-HTTP sessions: the client sends Mcp-Session-Id on
        # every request after the first POST; dropping it makes the upstream
        # gateway lose the session and fail tool calls.
        "mcp-session-id",
    }

    def _forward_headers(request: Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower == "host" or lower == "content-length":
                continue
            if lower.startswith("x-metadata-") or lower in _FORWARD_HEADERS:
                headers[key] = value
        return headers

    async def _forward(
        address: str, path: str, request: Request, body: bytes
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        url = f"{address}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        client = httpx.AsyncClient(timeout=None)
        upstream = await client.send(
            client.build_request(
                request.method,
                url,
                headers=_forward_headers(request),
                content=body,
            ),
            stream=True,
        )
        return client, upstream

    def _stream(
        client: httpx.AsyncClient, upstream: httpx.Response
    ) -> StreamingResponse:
        async def _body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        response_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            not in ("content-length", "transfer-encoding", "connection")
        }
        return StreamingResponse(
            _body(),
            status_code=upstream.status_code,
            headers=response_headers,
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def proxy(path: str, request: Request) -> Response:
        sandbox_id = request.headers.get("E2b-Sandbox-Id")
        if not sandbox_id:
            return Response(
                status_code=401, content="Missing E2b-Sandbox-Id header"
            )
        try:
            address = await _resolve_address(sandbox_id)
        except Exception as e:
            logger.info("route lookup failed for %s: %s", sandbox_id, e)
            # 502 matches the envd /health contract so the SDK's
            # ``is_running()`` reports False when the node is gone.
            return Response(status_code=502, content="Sandbox route not found")

        # Buffer the request body once so a single retry can replay it after
        # the node moved (migration) or the stale route failed. Trade-off:
        # uploads are buffered in memory instead of streamed.
        body = await request.body()

        for attempt in (1, 2):
            try:
                client, upstream = await _forward(address, path, request, body)
            except httpx.HTTPError as e:
                logger.info(
                    "proxy to %s failed (attempt %d): %s", address, attempt, e
                )
                if attempt == 1:
                    # Node may be gone (migration / crash): drop the cached
                    # route and resolve once more before giving up.
                    routes.invalidate(sandbox_id)
                    try:
                        address = await _resolve_address(sandbox_id)
                    except Exception as re:
                        logger.info(
                            "re-resolve failed for %s: %s", sandbox_id, re
                        )
                        return Response(
                            status_code=502, content=f"Node unavailable: {e}"
                        )
                    continue
                return Response(
                    status_code=502, content=f"Node unavailable: {e}"
                )
            if attempt == 1 and upstream.status_code == 502:
                # The node answered but no longer hosts this sandbox (e.g.
                # migration): invalidate and re-resolve once, then replay.
                await upstream.aclose()
                await client.aclose()
                routes.invalidate(sandbox_id)
                try:
                    address = await _resolve_address(sandbox_id)
                except Exception as re:
                    logger.info("re-resolve failed for %s: %s", sandbox_id, re)
                    return Response(
                        status_code=502, content="Sandbox route not found"
                    )
                continue
            return _stream(client, upstream)

        return Response(status_code=502, content="Sandbox route not found")

    return app
