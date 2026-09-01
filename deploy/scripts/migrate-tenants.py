#!/usr/bin/env python3
"""One-shot migration of unowned resources to tenants (E3.1).

Tenant isolation assigns every resource a ``tenant_id`` at creation. Pre-E3.1
resources were created with ``tenant_id = None`` ("unowned"); before enabling
E2B_TENANTS on a control plane those records must be migrated, otherwise
they are only visible to admin keys.

The script migrates, from the control plane workspace root:
  * volume records        ``<root>/_volumes/<vol_id>/``  (writes ``_meta``)
  * snapshot records      ``<root>/<snap_id>/snapshot.json``
  * template records      ``<root>/_templates/<tpl_id>/template.json``
  * secret records        ``<root>/_secrets/<sec_id>/secret.json``
and, when ``--redis-url`` is given, sandbox records in the shared Redis
record store (``<namespace>:record:*``).

Assignment rules (``--mapping``, JSON):
    {"t1": {"names": ["alpha"]},
     "t2": {"before": "2026-08-01T00:00:00Z"}}
* name rules match the resource name (volume/template/secret name, first
  snapshot name, sandbox metadata.name / client_id) and are evaluated first;
* time rules match ``created_at`` (``before``/``after``, ISO 8601) and are
  evaluated after name rules;
* anything still unassigned falls back to ``--default-tenant``;
* anything still unowned is reported (and fails with ``--strict``).

Misconfiguration guard: when E2B_ADMIN_API_KEYS is empty the script refuses
to run unless E2B_TENANTS (or ``--tenant-map``) is configured, so an
intended isolation rollout cannot silently leave every key without a tenant
or admin role.

Example:
    E2B_TENANTS='{"t1":["keyA"],"t2":["keyC"]}' \
    python deploy/scripts/migrate-tenants.py \
        --workspace-root /var/lib/e2b \
        --mapping '{"t1":{"before":"2026-08-15T00:00:00Z"}}' \
        --default-tenant t2 \
        --redis-url redis://127.0.0.1:6379
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_json_env(name: str) -> dict[str, Any] | None:
    raw = os.getenv(name)
    if not raw:
        return None
    return json.loads(raw)


def assign_tenant(
    name: str | None,
    created_at: Any,
    rules: dict[str, dict[str, Any]],
    default_tenant: str | None,
) -> str | None:
    """Pick a tenant for an unowned resource, or ``None`` to leave unowned."""
    created_dt = _parse_dt(created_at)
    for tenant, rule in rules.items():
        if name and name in (rule.get("names") or []):
            return tenant
    for tenant, rule in rules.items():
        if created_dt is None:
            continue
        before = _parse_dt(rule.get("before"))
        after = _parse_dt(rule.get("after"))
        if before is not None and created_dt < before:
            return tenant
        if after is not None and created_dt >= after:
            return tenant
    return default_tenant


def _resource_name(payload: dict[str, Any], *, volume_id: str | None = None) -> str | None:
    for key in ("name",):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    names = payload.get("names")
    if isinstance(names, list) and names and isinstance(names[0], str):
        return names[0]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("name", "tenant"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return volume_id


def _ensure_json_payload(
    path: Path,
    payload: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
    )


def migrate_volume_dir(
    volume_dir: Path,
    meta_dir: Path,
    rules: dict[str, dict[str, Any]],
    default_tenant: str | None,
    *,
    dry_run: bool,
) -> tuple[str, str | None, bool]:
    """Migrate one volume directory; returns (volume_id, tenant, changed)."""
    volume_id = volume_dir.name
    meta_path = meta_dir / f"{volume_id}.json"
    if meta_path.is_file():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        payload = {
            "volume_id": volume_id,
            "name": volume_id,
            "token": "",
            "node_id": "local",
            "created_at": volume_dir.stat().st_mtime,
            "per_sandbox_quota_mb": 0,
            "tenant_id": None,
        }
    if payload.get("tenant_id") is not None:
        return volume_id, payload["tenant_id"], False
    tenant = assign_tenant(
        _resource_name(payload, volume_id=volume_id),
        payload.get("created_at"),
        rules,
        default_tenant,
    )
    payload["tenant_id"] = tenant
    _ensure_json_payload(meta_path, payload, dry_run)
    return volume_id, tenant, True


def migrate_record_json(
    path: Path,
    rules: dict[str, dict[str, Any]],
    default_tenant: str | None,
    *,
    dry_run: bool,
    name_fallback: str | None = None,
) -> tuple[str | None, str | None, bool]:
    """Migrate one JSON record file; returns (resource_id, tenant, changed)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    resource_id = (
        payload.get("snapshot_id")
        or payload.get("template_id")
        or payload.get("secret_id")
    )
    if payload.get("tenant_id") is not None:
        return resource_id, payload["tenant_id"], False
    tenant = assign_tenant(
        _resource_name(payload) or name_fallback,
        payload.get("created_at"),
        rules,
        default_tenant,
    )
    payload["tenant_id"] = tenant
    _ensure_json_payload(path, payload, dry_run)
    return resource_id, tenant, True


def migrate_redis_sandboxes(
    client,
    namespace: str,
    rules: dict[str, dict[str, Any]],
    default_tenant: str | None,
    *,
    dry_run: bool,
) -> Counter:
    """Migrate sandbox records in the shared Redis store; per-tenant counts."""
    counts: Counter = Counter()
    pattern = f"{namespace}:record:*"
    for key in client.keys(pattern):
        raw = client.get(key)
        if not raw:
            continue
        payload = json.loads(raw)
        if payload.get("tenant_id") is not None:
            continue
        tenant = assign_tenant(
            _resource_name(payload),
            payload.get("started_at"),
            rules,
            default_tenant,
        )
        payload["tenant_id"] = tenant
        if tenant is not None:
            counts[tenant] += 1
        else:
            counts["<unowned>"] += 1
        if not dry_run:
            client.set(key, json.dumps(payload, separators=(",", ":")))
    return counts


def _validate_deployment_config(tenant_map: dict[str, Any] | None) -> None:
    admin_keys = os.getenv("E2B_ADMIN_API_KEYS", "").strip()
    tenants = tenant_map or parse_json_env("E2B_TENANTS")
    if not admin_keys and not tenants:
        raise SystemExit(
            "E2B_ADMIN_API_KEYS is empty and E2B_TENANTS is not set: refusing "
            "to migrate. Configure E2B_TENANTS (or --tenant-map) before "
            "enabling tenant isolation, otherwise all resources end up "
            "unowned and every key loses access."
        )


def run_migration(
    workspace_root: Path,
    *,
    rules: dict[str, dict[str, Any]],
    default_tenant: str | None,
    dry_run: bool,
    redis_client=None,
    redis_namespace: str = "e2b",
) -> dict[str, Any]:
    """Migrate all discovered resources; returns a report dict."""
    report: dict[str, Any] = {}

    volumes_dir = workspace_root / "_volumes"
    volume_counts: Counter = Counter()
    volume_changed = 0
    if volumes_dir.is_dir():
        meta_dir = volumes_dir / "_meta"
        for entry in sorted(volumes_dir.iterdir()):
            if not entry.is_dir() or entry.name == "_meta":
                continue
            _volume_id, tenant, changed = migrate_volume_dir(
                entry, meta_dir, rules, default_tenant, dry_run=dry_run
            )
            volume_counts[tenant if tenant is not None else "<unowned>"] += 1
            volume_changed += int(changed)
    report["volumes"] = dict(volume_counts)
    report["volumesChanged"] = volume_changed

    json_sections = {
        "snapshots": (
            list(workspace_root.glob("*/snapshot.json")),
            "snapshot_id",
        ),
        "templates": (
            list((workspace_root / "_templates").glob("*/template.json"))
            if (workspace_root / "_templates").is_dir()
            else [],
            "template_id",
        ),
        "secrets": (
            list((workspace_root / "_secrets").glob("*/secret.json"))
            if (workspace_root / "_secrets").is_dir()
            else [],
            "secret_id",
        ),
    }
    for section, (paths, _id_key) in json_sections.items():
        counts: Counter = Counter()
        changed = 0
        for path in paths:
            _resource_id, tenant, is_changed = migrate_record_json(
                path,
                rules,
                default_tenant,
                dry_run=dry_run,
                name_fallback=path.parent.name,
            )
            counts[tenant if tenant is not None else "<unowned>"] += 1
            changed += int(is_changed)
        report[section] = dict(counts)
        report[f"{section}Changed"] = changed

    if redis_client is not None:
        counts = migrate_redis_sandboxes(
            redis_client,
            redis_namespace,
            rules,
            default_tenant,
            dry_run=dry_run,
        )
        report["sandboxes"] = dict(counts)
        report["sandboxesChanged"] = sum(counts.values())
    return report


def _unowned_count(report: dict[str, Any]) -> int:
    total = 0
    for section in ("volumes", "snapshots", "templates", "secrets", "sandboxes"):
        counts = report.get(section) or {}
        total += int(counts.get("<unowned>", 0))
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate unowned E3.1 resources to tenants."
    )
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--redis-namespace", default="e2b")
    parser.add_argument("--tenant-map", default=None, help="JSON E2B_TENANTS equivalent")
    parser.add_argument("--mapping", default=None, help="JSON assignment rules")
    parser.add_argument("--default-tenant", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    tenant_map = json.loads(args.tenant_map) if args.tenant_map else None
    _validate_deployment_config(tenant_map)
    rules = json.loads(args.mapping) if args.mapping else {}
    if not isinstance(rules, dict):
        parser.error("--mapping must be a JSON object")
    default_tenant = args.default_tenant or os.getenv("E2B_DEFAULT_TENANT")

    redis_client = None
    if args.redis_url:
        try:
            import redis
        except ImportError as exc:
            raise SystemExit("--redis-url requires the redis package") from exc
        redis_client = redis.Redis.from_url(args.redis_url)

    report = run_migration(
        args.workspace_root,
        rules=rules,
        default_tenant=default_tenant,
        dry_run=args.dry_run,
        redis_client=redis_client,
        redis_namespace=args.redis_namespace,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    unowned = _unowned_count(report)
    if unowned:
        print(
            f"WARNING: {unowned} resource(s) remain unowned; "
            "assign rules/default-tenant or review the mapping.",
            file=sys.stderr,
        )
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
