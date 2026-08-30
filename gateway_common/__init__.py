"""Shared helpers used by both the control plane and the envd service."""

# Redis pub/sub channel for gateway route invalidation. The control plane
# publishes the sandbox id after migration/kill; every gateway replica
# subscribes and drops its cached route immediately (multi-replica support).
GATEWAY_ROUTE_INVALIDATE_CHANNEL = "e2b:gateway:routes"
