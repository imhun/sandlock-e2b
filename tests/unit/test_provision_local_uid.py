"""#13 / OBS-9: how the local provisioner gets a per-sandbox host uid.

The snapshot-fork local branch now delegates to ``_provision_local`` (the
create path) instead of duplicating provisioning without a uid allocation;
this pins the acquire -> ownership -> register(host_uid) -> commit contract
that branch previously skipped, *and* the newer contract that supersedes it
when the record already carries a uid: the registry allocates it fleet-wide
(OBS-9) and the provisioner only claims it, because the tree it used to read
the uid from is writable by any root on any mounting node.
"""

from __future__ import annotations

from pathlib import Path


class _PoolStub:
    def __init__(self) -> None:
        self.acquired: tuple[str, object] | None = None
        self.claimed: tuple[str, int] | None = None
        self.committed = False
        self.released = False

    def acquire(self, sandbox_id: str, preferred=None) -> int:
        self.acquired = (sandbox_id, preferred)
        return 20000

    def claim(self, sandbox_id: str, uid: int) -> int:
        self.claimed = (sandbox_id, uid)
        return uid

    def commit(self, sandbox_id: str) -> None:
        self.committed = True

    def release(self, sandbox_id: str) -> None:
        self.released = True


class _RuntimeStub:
    def __init__(self, pool: _PoolStub) -> None:
        self.uid_pool = pool
        self.registered: dict = {}
        self._existing = None

    def get(self, sandbox_id: str):
        return self._existing

    def register(self, **kwargs) -> None:
        self.registered = kwargs


class _Record:
    sandbox_id = "sbx_forked"
    envd_access_token = "tok"
    workspace_dir: Path | None = None
    env_vars: dict = {}
    base_image = None
    memory_mb = 1024
    cpu_count = 1
    disk_size_mb = 1024
    max_processes = 64
    allow_internet_access = False
    max_command_timeout = 600
    mcp = None
    network = {}
    iam_tokens = {}
    allow_public_traffic = False
    volume_mounts: list[dict] = []
    volume_projects: list = []
    #: ``None`` = a record from before the registry allocated uids, i.e. the
    #: worker's own pool still decides (the fallback contract below).
    host_uid: int | None = None


class _AllocatedRecord(_Record):
    """A record whose uid the registry already allocated (OBS-9)."""

    host_uid = 20001


def _make_request(tmp_path: Path, pool: _PoolStub):
    from types import SimpleNamespace

    runtime = _RuntimeStub(pool)
    settings = SimpleNamespace(
        workspace_base=tmp_path,
        shared_volume_root=tmp_path / "shared-volumes",
        max_command_timeout=600,
    )
    state = SimpleNamespace(
        settings=settings,
        workspace_base=tmp_path / "workspaces",
        volumes={},
        snapshots=SimpleNamespace(expand_to=lambda snapshot, target: None),
        runtime_registry=runtime,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return request, runtime


def test_provision_local_acquires_applies_commits_uid(tmp_path, monkeypatch):
    from control_plane.api import sandboxes
    from control_plane.api.sandboxes import _provision_local

    pool = _PoolStub()
    request, runtime = _make_request(tmp_path, pool)
    record = _Record()
    applied: list[int] = []

    monkeypatch.setattr(sandboxes.os, "geteuid", lambda: 0)

    def _build_volume_mounts(**kwargs):
        return [], []

    monkeypatch.setattr(
        "envd_service.volumes.build_volume_mounts", _build_volume_mounts
    )
    monkeypatch.setattr(
        "envd_service.uid_pool.apply_sandbox_ownership",
        lambda workspace_dir, host_uid: applied.append(host_uid),
    )

    _provision_local(
        request,
        record,
        snapshot=None,
        volume_mounts=[],
        settings=request.app.state.settings,
    )

    assert pool.acquired == ("sbx_forked", None), pool.acquired
    assert applied == [20000], applied
    assert runtime.registered["host_uid"] == 20000
    assert runtime.registered["sandbox_id"] == "sbx_forked"
    assert pool.committed is True
    assert pool.released is False


def test_provision_local_claims_the_uid_the_registry_allocated(tmp_path, monkeypatch):
    """OBS-9: an allocated uid is claimed as-is -- no pool scan, no marker.

    The record's uid comes from the registry's shared store, so the worker must
    not re-derive it (that is the forgeable path) and must not spend a second
    one from its own pool.
    """
    from control_plane.api import sandboxes
    from control_plane.api.sandboxes import _provision_local

    pool = _PoolStub()
    request, runtime = _make_request(tmp_path, pool)
    record = _AllocatedRecord()
    applied: list[int] = []

    monkeypatch.setattr(sandboxes.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        "envd_service.volumes.build_volume_mounts", lambda **kwargs: ([], [])
    )
    monkeypatch.setattr(
        "envd_service.uid_pool.apply_sandbox_ownership",
        lambda workspace_dir, host_uid: applied.append(host_uid),
    )

    _provision_local(
        request,
        record,
        snapshot=None,
        volume_mounts=[],
        settings=request.app.state.settings,
    )

    assert pool.claimed == ("sbx_forked", 20001), pool.claimed
    assert pool.acquired is None, "the fallback allocator must not run"
    assert applied == [20001], applied
    assert runtime.registered["host_uid"] == 20001


def test_provision_local_releases_uid_on_failure(tmp_path, monkeypatch):
    from control_plane.api import sandboxes
    from control_plane.api.sandboxes import _provision_local
    from control_plane.api.errors import OfficialError

    pool = _PoolStub()
    request, runtime = _make_request(tmp_path, pool)
    record = _Record()
    monkeypatch.setattr(sandboxes.os, "geteuid", lambda: 0)

    def _boom(**kwargs):
        raise ValueError("bad mount")

    monkeypatch.setattr(
        "envd_service.volumes.build_volume_mounts", _boom
    )

    try:
        _provision_local(
            request,
            record,
            snapshot=None,
            volume_mounts=[],
            settings=request.app.state.settings,
        )
    except OfficialError:
        pass
    else:  # pragma: no cover - the stub must fail
        raise AssertionError("build_volume_mounts stub did not raise OfficialError")

    assert pool.acquired == ("sbx_forked", None)
    assert pool.released is True, "I3: a failed provision must return the uid"
    assert pool.committed is False
