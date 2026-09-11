"""F6: the create-sandbox agent route must not collapse worker faults into 401.

``agent_create_sandbox`` used to wrap ``_require_internal_key`` and the whole
provisioning call in one ``try``/``except PermissionError``. An EPERM/EACCES
raised *while provisioning* (a root-owned cold shared volume is the observed
case) therefore answered exactly like a bad internal key: 401 with an empty
body, and the control plane's ``Node ... failed to provision: <resp.text>``
showed nothing at all. These tests pin the two apart:

* provisioning ``PermissionError`` -> 500 whose body carries the reason;
* auth failure -> 401 with no reason on the wire.
"""

from __future__ import annotations

import httpx

import envd_service.agent as agent_module
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry

# The realistic shape of the observed fault: the worker cannot materialize the
# sandbox directory the shared volume handed it.
PROVISION_REASON = (
    "cold volume /var/lib/e2b-sandboxes/sbx_perm is root-owned, uid 65534 "
    "has no write access (EACCES on mkdir)"
)


def _make_worker(workspace):
    runtime_registry = RuntimeRegistry(workspace)
    # Pin the settings' workspace to the fixture too: create_app falls back to
    # ``workspace_base or settings.workspace_base``, and the agent routes read
    # settings.workspace_base directly -- leaving them different would write
    # into the repo's tmp/sandboxes instead of the fixture.
    settings = EnvdSettings(executor="local", workspace_base=workspace)
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return app, settings


async def _post_create(worker, key: str | None, sandbox_id: str):
    app, _settings = worker
    headers = {} if key is None else {"X-Internal-Key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes", json={"sandboxID": sandbox_id}, headers=headers
        )


async def test_create_permission_error_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    worker = _make_worker(workspace)
    _app, settings = worker

    def _boom(**_kwargs):
        raise PermissionError(PROVISION_REASON)

    monkeypatch.setattr(agent_module, "build_volume_mounts", _boom)

    resp = await _post_create(worker, settings.internal_api_key, "sbx_perm")

    assert resp.status_code == 500
    assert resp.status_code != 401
    assert resp.text == PROVISION_REASON


async def test_create_wrong_key_is_401_without_reason(workspace) -> None:
    worker = _make_worker(workspace)
    _app, _settings = worker

    resp = await _post_create(worker, "not-the-internal-key", "sbx_badkey")

    assert resp.status_code == 401
    assert resp.text == ""


async def test_create_missing_key_is_401_without_reason(workspace) -> None:
    worker = _make_worker(workspace)
    _app, _settings = worker

    resp = await _post_create(worker, None, "sbx_nokey")

    assert resp.status_code == 401
    assert resp.text == ""


async def test_create_snapshot_permission_error_is_500_with_reason(
    workspace, monkeypatch
) -> None:
    """Same F6 shape on the snapshot route: copytree EACCES is not a 401."""
    worker = _make_worker(workspace)
    app, settings = worker
    sandbox_dir = settings.workspace_base / "sbx_snap_src"
    (sandbox_dir / "workspace").mkdir(parents=True)

    def _boom(*_args, **_kwargs):
        raise PermissionError(PROVISION_REASON)

    monkeypatch.setattr(agent_module.shutil, "copytree", _boom)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            "/agent/snapshots",
            json={"snapshotID": "snap_perm", "sandboxID": "sbx_snap_src"},
            headers={"X-Internal-Key": settings.internal_api_key},
        )

    assert resp.status_code == 500
    assert resp.status_code != 401
    assert resp.text == PROVISION_REASON
