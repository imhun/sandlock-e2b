"""E3.1: resource records carry tenant_id and persist it (registry layer)."""

from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRecord, SandboxRegistry
from control_plane.registry.secrets import (
    SecretRegistry,
    SecretTenantMismatchError,
)
from control_plane.registry.snapshots import SnapshotRegistry
from control_plane.registry.templates import TemplateRegistry
from control_plane.registry.volumes import VolumeRegistry


def _settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _create_sandbox(registry, **kw):
    kwargs = dict(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    kwargs.update(kw)
    return registry.create(**kwargs)


def test_sandbox_record_storage_round_trip_keeps_tenant():
    record = SandboxRecord(
        template_id="base",
        sandbox_id="sbx_t1",
        client_id="cli_1",
        tenant_id="t1",
    )
    restored = SandboxRecord.from_storage_dict(record.to_storage_dict())
    assert restored.tenant_id == "t1"


def test_sandbox_record_storage_defaults_to_none():
    record = SandboxRecord(
        template_id="base", sandbox_id="sbx_x", client_id="cli_1"
    )
    restored = SandboxRecord.from_storage_dict(record.to_storage_dict())
    assert restored.tenant_id is None


def test_sandbox_create_stamps_tenant_and_list_filters(workspace):
    registry = SandboxRegistry(_settings())
    t1 = _create_sandbox(registry, tenant_id="t1")
    t2 = _create_sandbox(registry, tenant_id="t2")
    _create_sandbox(registry)  # unowned (compat mode)

    assert t1.tenant_id == "t1"
    assert t2.tenant_id == "t2"
    assert [r.sandbox_id for r in registry.list(tenant_id="t1")] == [t1.sandbox_id]
    assert [r.sandbox_id for r in registry.list(tenant_id="t2")] == [t2.sandbox_id]
    assert len(registry.list()) == 3


def test_sandbox_tenant_persists_in_redis_store():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    registry = SandboxRegistry(_settings(), redis_client=client)
    _create_sandbox(registry, tenant_id="t1")

    other = SandboxRegistry(_settings(), redis_client=fakeredis.FakeRedis(server=server))
    records = other.list(tenant_id="t1")
    assert len(records) == 1
    assert records[0].tenant_id == "t1"
    assert other.list(tenant_id="t2") == []


def test_volume_tenant_id_persists_across_instances(workspace):
    base = workspace / "volumes"
    first = VolumeRegistry(base)
    created = first.create("data", tenant_id="t1")
    assert created.tenant_id == "t1"

    second = VolumeRegistry(base)
    loaded = second.get(created.volume_id)
    assert loaded.tenant_id == "t1"
    assert loaded.name == "data"
    assert [v.volume_id for v in second.list(tenant_id="t1")] == [created.volume_id]
    assert second.list(tenant_id="t2") == []


def test_snapshot_tenant_id_persists_across_instances(workspace):
    source = workspace / "src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "file.txt").write_text("x", encoding="utf-8")
    base = workspace / "snapshots"
    first = SnapshotRegistry(base)
    created = first.create_from_sandbox(
        workspace_dir=source,
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        tenant_id="t1",
    )
    assert created.tenant_id == "t1"

    second = SnapshotRegistry(base)
    loaded = second.get(created.snapshot_id)
    assert loaded.tenant_id == "t1"
    assert [s.snapshot_id for s in second.list(tenant_id="t1")] == [
        created.snapshot_id
    ]
    assert second.list(tenant_id="t2") == []


def test_template_tenant_id_persists_across_instances(workspace):
    base = workspace / "templates"
    first = TemplateRegistry(base)
    created, _build = first.create("web", tenant_id="t1")
    assert created.tenant_id == "t1"

    second = TemplateRegistry(base)
    loaded = second.get(created.template_id)
    assert loaded.tenant_id == "t1"
    assert second.get_by_name("web").tenant_id == "t1"
    assert [t.template_id for t in second.list(tenant_id="t1")] == [
        created.template_id
    ]
    assert second.list(tenant_id="t2") == []


def test_secret_tenant_id_persists_across_instances(workspace):
    base = workspace / "secrets"
    first = SecretRegistry(base)
    created = first.create("api_key", "v1", metadata={"env": "test"}, tenant_id="t1")
    assert created.tenant_id == "t1"

    second = SecretRegistry(base)
    loaded = second.get(created.secret_id)
    assert loaded.tenant_id == "t1"
    assert second.get_by_name("api_key").tenant_id == "t1"
    assert [s.secret_id for s in second.list(tenant_id="t1")] == [created.secret_id]
    assert second.list(tenant_id="t2") == []


def test_secret_env_ref_tenant_mismatch_raises(workspace):
    registry = SecretRegistry(workspace / "secrets")
    registry.create("mine", "42", tenant_id="t1")
    registry.create("theirs", "7", tenant_id="t2")

    assert registry.resolve_env_refs({"A": "${mine}"}, tenant_id="t1") == {"A": "42"}
    with pytest.raises(SecretTenantMismatchError) as exc:
        registry.resolve_env_refs({"A": "${theirs}"}, tenant_id="t1")
    assert str(exc.value) == "Secret theirs does not belong to this tenant"
    # Admin bypasses the tenant check.
    assert registry.resolve_env_refs(
        {"A": "${theirs}"}, tenant_id="t1", is_admin=True
    ) == {"A": "7"}
    # Compat mode (tenant_id None) keeps legacy resolution.
    assert registry.resolve_env_refs({"A": "${theirs}"}) == {"A": "7"}
