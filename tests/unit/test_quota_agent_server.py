"""Quota-agent server endpoints + auth + path mapping (E2.6)."""

from __future__ import annotations

import httpx

import envd_service.xfs_quota as xfs_quota
from quota_agent.app import create_app
from quota_agent.config import Settings
from envd_service.xfs_quota import ProjectQuotaError, ProjectQuotaUsage

KEY = "sekret"


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    )


def _facts() -> dict:
    return {
        "fs_type": "xfs",
        "projid32bit": True,
        "prjquota": True,
        "xfs_quota": True,
    }


async def test_detect_requires_internal_key():
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get("/detect", params={"mount": "/srv/sandboxes"})
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

        bad = await client.get(
            "/detect",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": "wrong"},
        )
        assert bad.status_code == 401


async def test_detect_missing_mount_rejected():
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get("/detect")
        assert response.status_code == 422


async def test_detect_returns_server_side_facts(monkeypatch):
    seen: list[str] = []

    def fake_facts(mount):
        seen.append(str(mount))
        return _facts()

    monkeypatch.setattr(xfs_quota, "_local_facts", fake_facts)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get(
            "/detect",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": KEY},
        )
    assert response.status_code == 200
    assert response.json() == _facts()
    assert seen == ["/srv/sandboxes"]


async def test_detect_forwards_server_error_facts(monkeypatch):
    monkeypatch.setattr(xfs_quota, "_local_facts", lambda _mount: {"error": "mount boom"})
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get(
            "/detect",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": KEY},
        )
    assert response.status_code == 200
    assert response.json() == {"error": "mount boom"}


async def test_project_create_runs_provision_on_server(monkeypatch):
    seen: dict = {}

    def fake_provision(**kwargs):
        seen.update(kwargs)
        return 7

    monkeypatch.setattr(xfs_quota, "provision_project", fake_provision)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 7,
                "path": "/srv/sandboxes/sbx_1",
                "limit_mb": 2048,
                "mount": "/srv/sandboxes",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"projid": 7}
    assert seen == {
        "sandbox_id": "quota-agent:7",
        "project_dir": "/srv/sandboxes/sbx_1",
        "mount_point": "/srv/sandboxes",
        "disk_mb": 2048,
        "project_id": 7,
    }


async def test_project_create_failure_returns_500_error(monkeypatch):
    def fake_provision(**kwargs):
        raise ProjectQuotaError("xfs_quota 'limit -p bhard=2048M 7' failed: boom")

    monkeypatch.setattr(xfs_quota, "provision_project", fake_provision)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 7,
                "path": "/srv/sandboxes/sbx_1",
                "limit_mb": 2048,
                "mount": "/srv/sandboxes",
            },
        )
    assert response.status_code == 500
    assert response.json() == {
        "error": "xfs_quota 'limit -p bhard=2048M 7' failed: boom"
    }


async def test_project_create_body_validation():
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        zero = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 0,
                "path": "/srv/sandboxes/sbx_1",
                "limit_mb": 2048,
                "mount": "/srv/sandboxes",
            },
        )
        missing = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={"projid": 7, "path": "/srv/sandboxes/sbx_1", "mount": "/srv"},
        )
    assert zero.status_code == 422
    assert missing.status_code == 422


async def test_project_delete_runs_release_on_server(monkeypatch):
    seen: dict = {}

    def fake_release(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(xfs_quota, "release_project", fake_release)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/project_delete",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 7,
                "path": "/srv/sandboxes/sbx_1",
                "mount": "/srv/sandboxes",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"deleted": 7}
    assert seen == {
        "project_dir": "/srv/sandboxes/sbx_1",
        "mount_point": "/srv/sandboxes",
        "projid": 7,
    }


async def test_report_serializes_project_table(monkeypatch):
    table = {
        0: ProjectQuotaUsage(projid=0, used_blocks=4, soft_blocks=0, hard_blocks=0),
        7: ProjectQuotaUsage(projid=7, used_blocks=40, soft_blocks=0, hard_blocks=1024),
    }
    seen: list[str] = []
    monkeypatch.setattr(
        xfs_quota,
        "project_quota_table",
        lambda mount: seen.append(str(mount)) or table,
    )
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get(
            "/report",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": KEY},
        )
    assert response.status_code == 200
    assert response.json() == {
        "projects": {
            "0": {"used_blocks": 4, "soft_blocks": 0, "hard_blocks": 0},
            "7": {"used_blocks": 40, "soft_blocks": 0, "hard_blocks": 1024},
        }
    }
    assert seen == ["/srv/sandboxes"]


async def test_report_failure_returns_500_error(monkeypatch):
    def fake_table(mount):
        raise ProjectQuotaError("xfs_quota 'report -p' failed: report boom")

    monkeypatch.setattr(xfs_quota, "project_quota_table", fake_table)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.get(
            "/report",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": KEY},
        )
    assert response.status_code == 500
    assert response.json() == {
        "error": "xfs_quota 'report -p' failed: report boom"
    }


async def test_reconcile_delegates_to_local_reconcile(monkeypatch):
    seen: dict = {}

    def fake_reconcile(workspace_base, mount_point, state_base=None):
        seen["workspace_base"] = str(workspace_base)
        seen["mount_point"] = str(mount_point)
        seen["state_base"] = state_base
        return {"cleaned": [7], "skipped": [{"projid": 9, "reason": "busy"}]}

    monkeypatch.setattr(xfs_quota, "_local_reconcile", fake_reconcile)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/reconcile",
            headers={"X-Internal-Key": KEY},
            json={"workspace_base": "/srv/sandboxes", "mount": "/srv/sandboxes"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "cleaned": [7],
        "skipped": [{"projid": 9, "reason": "busy"}],
    }
    assert seen == {
        "workspace_base": "/srv/sandboxes",
        "mount_point": "/srv/sandboxes",
        "state_base": None,
    }


async def test_reconcile_forwards_the_state_base(monkeypatch):
    """N27: the reconciliation reads the records, so it gets the records' base."""
    seen: dict = {}

    def fake_reconcile(workspace_base, mount_point, state_base=None):
        seen["state_base"] = state_base
        return {"cleaned": [], "skipped": []}

    monkeypatch.setattr(xfs_quota, "_local_reconcile", fake_reconcile)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/reconcile",
            headers={"X-Internal-Key": KEY},
            json={
                "workspace_base": "/srv/workspaces",
                "mount": "/srv",
                "state_base": "/srv/state",
            },
        )
    assert response.status_code == 200
    assert seen == {"state_base": "/srv/state"}


async def test_reconcile_fail_closed_when_workspace_base_missing_or_unreadable(
    monkeypatch,
):
    """Missing/unreadable workspace_base -> 500 and no quota entry cleaned."""

    def fake_table(mount):
        return {
            0: ProjectQuotaUsage(
                projid=0, used_blocks=4, soft_blocks=0, hard_blocks=0
            ),
            7: ProjectQuotaUsage(
                projid=7, used_blocks=0, soft_blocks=0, hard_blocks=1024
            ),
        }

    monkeypatch.setattr(xfs_quota, "project_quota_table", fake_table)

    def fake_cleanup(**kwargs):
        raise AssertionError(
            "reconcile must not clean quota entries when workspace_base "
            "is missing or unreadable"
        )

    monkeypatch.setattr(xfs_quota, "cleanup_orphan_project", fake_cleanup)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        # Missing workspace_base: iterdir raises FileNotFoundError (OSError).
        missing = await client.post(
            "/reconcile",
            headers={"X-Internal-Key": KEY},
            json={
                "workspace_base": "/nonexistent/sandboxes",
                "mount": "/srv/sandboxes",
            },
        )
    assert missing.status_code == 500
    assert missing.json() == {
        "error": "reconcile workspace_base missing or unreadable: "
        "/nonexistent/sandboxes (FileNotFoundError)"
    }

    # Unreadable workspace_base: simulate PermissionError on the directory
    # listing (running as root makes a real chmod-000 dir still readable).
    class _PermissionDeniedPath:
        def __init__(self, path):
            self._path = path

        def iterdir(self):
            raise PermissionError("permission denied")

        def __str__(self):
            return str(self._path)

    monkeypatch.setattr(xfs_quota, "Path", _PermissionDeniedPath)
    async with _client(app) as client:
        denied = await client.post(
            "/reconcile",
            headers={"X-Internal-Key": KEY},
            json={
                "workspace_base": "/srv/sandboxes",
                "mount": "/srv/sandboxes",
            },
        )
    assert denied.status_code == 500
    assert denied.json() == {
        "error": "reconcile workspace_base missing or unreadable: "
        "/srv/sandboxes (PermissionError)"
    }


async def test_path_map_rewrites_client_paths_to_server_paths(monkeypatch):
    seen: dict = {}

    def fake_provision(**kwargs):
        seen.update(kwargs)
        return 7

    monkeypatch.setattr(xfs_quota, "provision_project", fake_provision)
    app = create_app(
        settings=Settings(
            token=KEY,
            path_map=(("/mnt/nfs", "/srv/sandboxes"),),
        )
    )
    async with _client(app) as client:
        response = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 7,
                "path": "/mnt/nfs/sbx_1",
                "limit_mb": 2048,
                "mount": "/mnt/nfs",
            },
        )
    assert response.status_code == 200
    assert seen["project_dir"] == "/srv/sandboxes/sbx_1"
    assert seen["mount_point"] == "/srv/sandboxes"


async def test_path_map_longest_prefix_wins():
    app = create_app(
        settings=Settings(
            token=KEY,
            path_map=(
                ("/mnt", "/srv"),
                ("/mnt/nfs", "/srv/sandboxes"),
            ),
        )
    )
    assert app.state.settings.path_map == (
        ("/mnt/nfs", "/srv/sandboxes"),
        ("/mnt", "/srv"),
    )


async def test_token_unconfigured_refuses_requests():
    app = create_app(settings=Settings(token=""))
    async with _client(app) as client:
        response = await client.get(
            "/detect",
            params={"mount": "/srv/sandboxes"},
            headers={"X-Internal-Key": ""},
        )
    assert response.status_code == 500
    assert response.json() == {"error": "quota-agent token not configured"}


async def test_server_side_errno_is_carried_through_untranslated(monkeypatch):
    """N30 (2026-09-26): this service is not where ``EDQUOT`` becomes ``ENOSPC``.

    On the agent line the sandbox's own ``write(2)`` never reaches the worker:
    it goes to the NFS client, which turns the server's ``EDQUOT`` into the
    client-visible ``ENOSPC`` (errno 28 -- docs §5.2 item 2, measured by
    ``deploy/scripts/nfs_quota_probe.sh`` case B), and the kernel hands that
    straight to the sandbox. So the agent's half of the contract is the
    *opposite* of a translation: a server-side errno reaching this admin
    surface has to come back out with its own name. Rewriting it here -- the
    tempting move, "make the caller see what the sandbox will see" -- would
    give §5.2's client-visible shape a second source of truth that exists on
    one deployment form only, and would hide which side of the NFS boundary
    the errno was raised on.
    """
    server_text = "xfs_quota 'limit -p bhard=2048M 7' failed: EDQUOT"

    def fake_provision(**kwargs):
        raise ProjectQuotaError(server_text)

    monkeypatch.setattr(xfs_quota, "provision_project", fake_provision)
    app = create_app(settings=Settings(token=KEY))
    async with _client(app) as client:
        response = await client.post(
            "/project_create",
            headers={"X-Internal-Key": KEY},
            json={
                "projid": 7,
                "path": "/srv/sandboxes/sbx_1",
                "limit_mb": 2048,
                "mount": "/srv/sandboxes",
            },
        )
    assert response.status_code == 500
    assert response.json() == {"error": server_text}
