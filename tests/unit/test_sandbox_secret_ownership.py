"""C1 / Task 3: who owns the secret file a non-root worker writes.

``_materialize_http_inject`` writes ``<secrets>/<sandbox>/<name>.secret`` in
mode 0600 and then hands it to the sandbox's pooled host uid. The **slot** is
what reads it (route B runs ``sandlock-supervise`` as that same uid), so a file
left owned by the worker is one the supervisor cannot open: the route-B policy
then fails validation (``invalid sandbox: credential file ... Permission
denied``) and the sandbox never starts.

Until this task the hand-over was ``if os.geteuid() == 0 and identity`` -- on a
non-root worker it was skipped in silence, which is the shape the production
non-root deployment uses (per-sandbox uid + route B). The cases below pin the
three branches on the *shape of the call*: what the brokers are asked to do,
that the 0600 mode still lands, and that a whitelist which does not cover the
secret path fails loudly instead of leaving the file worker-owned.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from envd_service import priv_helpers
from envd_service.executors.sandlock import SandlockExecutor
from gateway_common.network import sandlock_network_policy

SANDBOX_UID = 21001


class _Brokers:
    """A recording stand-in for the ``priv_helpers`` module functions.

    The executor only ever reaches the brokers through the module-level
    helpers (``helpers_cover`` / ``broker_chown``), so the stub is installed on
    the module and records the *arguments the executor chose* -- the uid, the
    path and the recursion flag -- rather than any broker behaviour.
    """

    def __init__(self, *, covers: bool) -> None:
        self._covers = covers
        self.chowns: list[tuple[int, Path, bool]] = []

    def helpers_cover(self, path: str | Path) -> bool:
        return self._covers

    def broker_chown(
        self,
        uid: int,
        path: str | Path,
        *,
        recursive: bool = True,
        gid: int | None = None,
    ) -> None:
        self.chowns.append((uid, Path(path), recursive))


def _install(monkeypatch, brokers: _Brokers) -> None:
    monkeypatch.setattr(priv_helpers, "helpers_cover", brokers.helpers_cover)
    monkeypatch.setattr(priv_helpers, "broker_chown", brokers.broker_chown)


def _executor(tmp_path: Path, *, host_uid: int | None = SANDBOX_UID) -> SandlockExecutor:
    """The real executor, built the way ``factory.py`` builds one.

    ``secrets_dir`` is ``settings.image_cache_dir / "secrets"`` there, so the
    secret really lives under ``<image_cache_dir>/secrets/<sandbox>/``.
    """
    return SandlockExecutor(
        workspace_dir=str(tmp_path / "sbx_1"),
        base_image=None,
        image_rootfs=None,
        host_uid=host_uid,
        per_sandbox_uid=host_uid is not None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=True,
        secrets_dir=tmp_path / "secrets",
    )


def _http_inject_entries() -> list[dict]:
    """One literal ``transform.headers`` entry, mapped by the real producer."""
    policy = sandlock_network_policy(
        {
            "allowOut": ["api.example.com"],
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-API-Key": "sk-literal"}}}
                ]
            },
        },
        allow_internet_access=False,
        enable_network=True,
    )
    entries = policy["http_inject"]
    assert entries == [
        {
            "matcher": "api.example.com",
            "auth": "header:X-API-Key",
            "value": "sk-literal",
            "name": "hdr_api_example_com_x_api_key",
            "on_existing": "replace",
        }
    ]
    return entries


def _expected_secret_path(tmp_path: Path) -> Path:
    return tmp_path / "secrets" / "sbx_1" / "hdr_api_example_com_x_api_key.secret"


def _record_chmod(monkeypatch) -> list[tuple[Path, int]]:
    """Record ``os.chmod`` while still applying it, so the mode is real."""
    calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def _chmod(path, mode, *args, **kwargs):
        calls.append((Path(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _chmod)
    return calls


def test_nonroot_worker_hands_the_secret_to_the_sandbox_uid(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    brokers = _Brokers(covers=True)
    _install(monkeypatch, brokers)
    chmod_calls = _record_chmod(monkeypatch)

    out = _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert brokers.chowns == [(SANDBOX_UID, path, False)]
    assert chmod_calls == [(path, 0o600)]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert out == [
        {
            "matcher": "api.example.com",
            "auth": "header:X-API-Key",
            "name": "hdr_api_example_com_x_api_key",
            "on_existing": "replace",
            "secret": f"file:{path}",
        }
    ]


def test_root_worker_keeps_the_direct_chown(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    brokers = _Brokers(covers=True)
    _install(monkeypatch, brokers)
    chmod_calls = _record_chmod(monkeypatch)
    chown_calls: list[tuple[Path, int, int]] = []
    real_chown = os.chown

    def _chown(path, uid, gid, *args, **kwargs):
        chown_calls.append((Path(path), uid, gid))
        return real_chown(path, uid, gid, *args, **kwargs)

    monkeypatch.setattr(os, "chown", _chown)

    _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert brokers.chowns == []
    # root chowns only the uid and passes -1 for the gid, exactly as today.
    assert chown_calls == [(path, SANDBOX_UID, -1)]
    assert chmod_calls == [(path, 0o600)]


def test_legacy_shared_uid_shape_asks_nobody(tmp_path, monkeypatch):
    """No per-sandbox uid: no identity to hand the file to, as before."""
    worker_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    brokers = _Brokers(covers=True)
    _install(monkeypatch, brokers)
    chmod_calls = _record_chmod(monkeypatch)

    _executor(tmp_path, host_uid=None)._materialize_http_inject(
        _http_inject_entries()
    )

    path = _expected_secret_path(tmp_path)
    assert brokers.chowns == []
    assert chmod_calls == [(path, 0o600)]
    assert path.stat().st_uid == worker_uid


def test_helpers_not_covering_the_path_fails_loudly(tmp_path, monkeypatch):
    """A whitelist that does not reach the secret path must refuse the create.

    Silently skipping the hand-over is the defect: the file stays worker-owned
    and the sandbox dies later, at supervise, with a permission error naming
    neither the path nor the missing whitelist root.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    brokers = _Brokers(covers=False)
    _install(monkeypatch, brokers)

    path = _expected_secret_path(tmp_path)
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    assert str(excinfo.value) == (
        f"cannot hand {path} to sandbox uid {SANDBOX_UID} on a non-root "
        "worker: the file-capability broker whitelist does not contain it "
        "(E2B_IMAGE_CACHE_DIR must be one of the broker's roots)"
    )
    assert brokers.chowns == []
