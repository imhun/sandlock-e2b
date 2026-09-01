"""E3.1: one-shot tenant migration script behavior."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

fakeredis = pytest.importorskip("fakeredis")


def _load_script() -> object:
    script = (
        Path(__file__).resolve().parent.parent.parent
        / "deploy"
        / "scripts"
        / "migrate-tenants.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_tenants", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrate_tenants = _load_script()


def _iso(year: int) -> str:
    return datetime(year, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def test_assign_tenant_by_name():
    rules = {"t1": {"names": ["alpha"]}, "t2": {"names": ["beta"]}}
    assert migrate_tenants.assign_tenant("alpha", None, rules, None) == "t1"
    assert migrate_tenants.assign_tenant("beta", None, rules, None) == "t2"


def test_assign_tenant_by_created_before():
    rules = {"t1": {"before": _iso(2026) + ""}, "t2": {"after": _iso(2026)}}
    # before-rule evaluates first, so a 2025 resource lands in t1.
    assert (
        migrate_tenants.assign_tenant(None, _iso(2025), rules, None) == "t1"
    )
    assert (
        migrate_tenants.assign_tenant(None, _iso(2027), rules, None) == "t2"
    )


def test_assign_tenant_falls_back_to_default():
    assert migrate_tenants.assign_tenant("unknown", None, {}, "t2") == "t2"
    assert migrate_tenants.assign_tenant("unknown", None, {}, None) is None


def test_migrate_volume_dir_creates_meta_record(workspace):
    volumes = workspace / "_volumes"
    vol_dir = volumes / "vol_old"
    vol_dir.mkdir(parents=True)
    (vol_dir / "data.txt").write_text("x", encoding="utf-8")

    volume_id, tenant, changed = migrate_tenants.migrate_volume_dir(
        vol_dir,
        volumes / "_meta",
        {},
        "t2",
        dry_run=False,
    )
    assert (volume_id, tenant, changed) == ("vol_old", "t2", True)
    meta = json.loads((volumes / "_meta" / "vol_old.json").read_text(encoding="utf-8"))
    assert meta["tenant_id"] == "t2"
    assert meta["name"] == "vol_old"


def test_migrate_volume_dir_skips_owned(workspace):
    volumes = workspace / "_volumes"
    vol_dir = volumes / "vol_owned"
    vol_dir.mkdir(parents=True)
    meta_dir = volumes / "_meta"
    meta_dir.mkdir(parents=True)
    (meta_dir / "vol_owned.json").write_text(
        json.dumps(
            {
                "volume_id": "vol_owned",
                "name": "vol_owned",
                "token": "tok",
                "node_id": "local",
                "created_at": _iso(2025),
                "per_sandbox_quota_mb": 0,
                "tenant_id": "t1",
            }
        ),
        encoding="utf-8",
    )
    _volume_id, tenant, changed = migrate_tenants.migrate_volume_dir(
        vol_dir, meta_dir, {"t2": {"names": ["vol_owned"]}}, None, dry_run=False
    )
    assert (tenant, changed) == ("t1", False)


def test_migrate_snapshot_json(workspace):
    snap_dir = workspace / "snap_old"
    snap_dir.mkdir(parents=True)
    path = snap_dir / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap_old",
                "names": ["prod-snap"],
                "created_at": _iso(2025),
                "tenant_id": None,
            }
        ),
        encoding="utf-8",
    )
    resource_id, tenant, changed = migrate_tenants.migrate_record_json(
        path, {"t1": {"names": ["prod-snap"]}}, None, dry_run=False
    )
    assert (resource_id, tenant, changed) == ("snap_old", "t1", True)
    assert json.loads(path.read_text(encoding="utf-8"))["tenant_id"] == "t1"


def test_migrate_secret_and_template_json(workspace):
    for section, record_id in (
        ("_secrets", "sec_old"),
        ("_templates", "tpl_old"),
    ):
        base = workspace / section
        (base / record_id).mkdir(parents=True)
        key = "secret_id" if section == "_secrets" else "template_id"
        path = base / record_id / ("secret.json" if section == "_secrets" else "template.json")
        path.write_text(
            json.dumps(
                {
                    key: record_id,
                    "name": f"n-{record_id}",
                    "created_at": _iso(2025),
                    "tenant_id": None,
                }
            ),
            encoding="utf-8",
        )
        _rid, tenant, changed = migrate_tenants.migrate_record_json(
            path, {"t2": {"before": _iso(2026)}}, None, dry_run=False
        )
        assert (tenant, changed) == ("t2", True)


def test_dry_run_does_not_write(workspace):
    vol_dir = workspace / "_volumes" / "vol_dry"
    vol_dir.mkdir(parents=True)
    _volume_id, tenant, changed = migrate_tenants.migrate_volume_dir(
        vol_dir, workspace / "_volumes" / "_meta", {}, "t1", dry_run=True
    )
    assert (tenant, changed) == ("t1", True)
    assert not (workspace / "_volumes" / "_meta" / "vol_dry.json").exists()


def test_redis_sandbox_migration():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    for sandbox_id, started in (
        ("sbx_old1", _iso(2025)),
        ("sbx_old2", _iso(2027)),
    ):
        client.set(
            f"e2b:record:{sandbox_id}",
            json.dumps(
                {
                    "sandbox_id": sandbox_id,
                    "template_id": "base",
                    "client_id": "cli_x",
                    "started_at": started,
                    "tenant_id": None,
                }
            ),
        )
    counts = migrate_tenants.migrate_redis_sandboxes(
        client,
        "e2b",
        {"t1": {"before": _iso(2026)}},
        "t2",
        dry_run=False,
    )
    assert dict(counts) == {"t1": 1, "t2": 1}
    assert json.loads(client.get("e2b:record:sbx_old1"))["tenant_id"] == "t1"
    assert json.loads(client.get("e2b:record:sbx_old2"))["tenant_id"] == "t2"


def test_redis_migration_dry_run_keeps_records():
    client = fakeredis.FakeRedis()
    client.set(
        "e2b:record:sbx_x",
        json.dumps(
            {
                "sandbox_id": "sbx_x",
                "client_id": "cli_x",
                "started_at": _iso(2025),
                "tenant_id": None,
            }
        ),
    )
    migrate_tenants.migrate_redis_sandboxes(
        client, "e2b", {"t1": {"before": _iso(2026)}}, None, dry_run=True
    )
    assert json.loads(client.get("e2b:record:sbx_x"))["tenant_id"] is None


def test_env_guard_refuses_without_tenants_and_admins(monkeypatch, capsys):
    monkeypatch.delenv("E2B_ADMIN_API_KEYS", raising=False)
    monkeypatch.delenv("E2B_TENANTS", raising=False)
    with pytest.raises(SystemExit) as exc:
        migrate_tenants._validate_deployment_config(None)
    assert "E2B_ADMIN_API_KEYS is empty" in str(exc.value)


def test_env_guard_allows_tenant_map(monkeypatch):
    monkeypatch.delenv("E2B_ADMIN_API_KEYS", raising=False)
    migrate_tenants._validate_deployment_config({"t1": ["keyA"]})  # no raise


def test_run_migration_reports_unowned(workspace):
    vol_dir = workspace / "_volumes" / "vol_x"
    vol_dir.mkdir(parents=True)
    report = migrate_tenants.run_migration(
        workspace,
        rules={},
        default_tenant=None,
        dry_run=True,
    )
    assert report["volumes"] == {"<unowned>": 1}
    assert migrate_tenants._unowned_count(report) == 1


def test_main_strict_fails_on_unowned(workspace, monkeypatch, capsys):
    vol_dir = workspace / "_volumes" / "vol_x"
    vol_dir.mkdir(parents=True)
    monkeypatch.setenv("E2B_TENANTS", '{"t1": ["keyA"]}')
    monkeypatch.setenv("E2B_ADMIN_API_KEYS", "")
    code = migrate_tenants.main(
        [
            "--workspace-root",
            str(workspace),
            "--mapping",
            "{}",
            "--strict",
            "--dry-run",
        ]
    )
    assert code == 1
    out = capsys.readouterr().err
    assert "remain unowned" in out
