"""API key authentication for the control plane."""

from __future__ import annotations

import secrets

from fastapi import Header, Request

from control_plane.api.errors import OfficialError


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    settings = request.app.state.settings
    key = x_api_key
    if key is None:
        # Some SDK versions emit X-API-KEY; headers are case-insensitive but
        # FastAPI dependency resolution already normalized this one.
        key = request.headers.get("X-API-KEY")
    if key is None or key not in settings.all_api_keys:
        raise OfficialError(401, "Unauthorized")
    # E3.1 fail-closed: with E2B_TENANTS configured every non-admin key must
    # be mapped to a tenant. Legacy E2B_API_KEYS/E2B_API_KEY that were never
    # mapped must not list/read/delete tenant resources or bypass tenant
    # quota/rate limits, so they are rejected here before any handler runs.
    if settings.tenants_enabled and key not in settings.admin_api_keys:
        if settings.tenant_of_key(key) is None:
            raise OfficialError(403, "API key is not mapped to any tenant")
    return key


def verify_internal_key(provided: str | None, settings) -> bool:
    """Constant-time check of X-Internal-Key against the active key list.

    E3.6: ``settings.internal_api_keys`` (E2B_INTERNAL_API_KEYS) carries the
    rotation window — every listed key is valid. When the list is empty the
    legacy single ``internal_api_key`` is the only credential. Workers and
    the gateway accept the same list, so a deploy can add the new key,
    roll the fleet, then drop the old key from the list.

    ⚠ This proves "you are one of the components", **not** "you are worker-1":
    the list is fleet-wide (N49). The per-node binding is
    :func:`node_id_for_key`; a handler that acts for a node must consult it too.
    """
    if provided is None:
        return False
    keys = getattr(settings, "all_internal_api_keys", None)
    if keys is None:
        keys = (
            getattr(settings, "internal_api_key", None) or "internal-key",
        )
    if not keys:
        return False
    return any(secrets.compare_digest(provided, key) for key in keys)


def node_id_for_key(provided: str | None, settings) -> str | None:
    """The node a credential is bound to, or ``None`` for a fleet credential.

    C3 Task 2 / N49: this is step 1 of the internal API's three-step validation
    -- the **only** trusted source of a caller's identity. The binding is
    configuration (``E2B_INTERNAL_NODE_KEYS``), never the request: a mapping
    learned from the request would let an attacker re-pin a node's credential to
    themselves, which is exactly the hole the source-IP second factor exists to
    close (``docs/c3-privilege-relocation.md`` §11.1 item 9 premise (a)).

    ``None`` means the key carries no node identity (the legacy fleet key);
    callers must make that path explicit and named, never treat it as "node
    unknown, so allow anything". Compared in constant time so a timing oracle
    cannot recover which keys are node-bound.
    """
    if provided is None:
        return None
    mapping = getattr(settings, "internal_node_keys", None) or {}
    matched: str | None = None
    for key, node_id in mapping.items():
        if secrets.compare_digest(provided, key):
            matched = node_id
    return matched


def tenant_of(request: Request) -> tuple[str | None, bool]:
    """Resolve the request key to ``(tenant_id, is_admin)``.

    Admin keys return ``(None, True)`` (all tenants). Tenant keys return
    ``(tenant_id, False)``. In compatible mode (E2B_TENANTS unset) every
    key returns ``(None, False)`` — no isolation, matching legacy behavior.
    """
    settings = request.app.state.settings
    key = request.headers.get("X-API-Key") or request.headers.get("X-API-KEY")
    if key is None:
        return None, False
    if key in settings.admin_api_keys:
        return None, True
    return settings.tenant_of_key(key), False


def tenant_scope(request: Request) -> str | None:
    """Tenant to filter list endpoints by; ``None`` means no filtering
    (admin keys and compatible mode)."""
    tenant, is_admin = tenant_of(request)
    return None if is_admin else tenant


def _require_owned(
    request: Request,
    record,
    *,
    resource_id: str | None = None,
    label: str = "resource",
) -> None:
    """Single-resource ownership guard: a non-matching tenant gets the same
    404 as a missing resource (no existence leak)."""
    tenant, is_admin = tenant_of(request)
    if is_admin or tenant is None:
        return
    if getattr(record, "tenant_id", None) != tenant:
        if resource_id:
            raise OfficialError(404, f"{label} {resource_id} not found")
        raise OfficialError(404, f"{label} not found")


def _require_related(
    request: Request,
    record,
    *,
    resource_id: str | None = None,
    label: str = "resource",
) -> None:
    """Cross-resource guard (mount a volume, fork from a snapshot/template,
    inject a secret): mismatched tenants are rejected with 403."""
    tenant, is_admin = tenant_of(request)
    if is_admin or tenant is None:
        return
    if getattr(record, "tenant_id", None) != tenant:
        suffix = f" {resource_id}" if resource_id else ""
        raise OfficialError(
            403, f"{label}{suffix} does not belong to this tenant"
        )
