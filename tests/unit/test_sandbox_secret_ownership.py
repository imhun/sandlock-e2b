"""C1 / Task 3: the order the executor uses to publish a secret file.

``_materialize_http_inject`` writes ``<secrets>/<sandbox>/<name>.secret`` in
mode 0600 and then hands it to the sandbox's pooled host uid. The **slot** is
what reads it (route B runs ``sandlock-supervise`` as that same uid), so a file
left owned by the worker is one the supervisor cannot open: the route-B policy
then fails validation (``invalid sandbox: credential file ... Permission
denied``) and the sandbox never starts.

Until this task the hand-over was ``if os.geteuid() == 0 and identity`` -- on a
non-root worker it was skipped in silence, which is the shape the production
non-root deployment uses (per-sandbox uid + route B).

**Ordering is the property these cases pin**, not just the call shape: once the
uid has been handed over the worker is neither the owner nor ``CAP_FOWNER``, so
a ``chmod`` that runs *after* the hand-over is ``EPERM`` on a real host (the
review T3-1 finding: the first version of this fix moved the failure from
supervise to create instead of removing it). :class:`_Host` therefore records
one ordered event list *and* refuses a late ``chmod`` the way the kernel does,
so "same calls, wrong order" goes red. The mode is also pinned to land on a
file that is already 0600-readable by nobody but the reader it is meant for:
a refusal must not leave a umask-mode (0644) credential file behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from envd_service import priv_helpers
from envd_service.executors.sandlock import SandlockExecutor
from gateway_common.network import sandlock_network_policy

SANDBOX_UID = 21001
WORKER_UID = 65534


class _Host:
    """The worker's syscalls plus the brokers, one ordered event list.

    Only the executor's *own* calls are recorded -- the filesystem helpers it
    shares with the rest of the platform are not -- and the events carry the
    arguments the executor chose, so the assertion is on the sequence the
    reader (``supervise``) depends on.
    """

    def __init__(self, *, euid: int, covers: bool) -> None:
        self.euid = euid
        self.covers = covers
        self.events: list[tuple] = []
        self._handed_over = False
        self._real_chmod = os.chmod
        self._real_chown = os.chown
        self._real_unlink = os.unlink

    # ------------------------------------------------------- syscalls

    def geteuid(self) -> int:
        return self.euid

    def chmod(self, path, mode, *args, **kwargs):
        if self._handed_over and self.euid != 0:
            # What the kernel answers once the file belongs to the sandbox
            # uid: the worker is not the owner and holds no CAP_FOWNER.
            raise PermissionError(1, "Operation not permitted", str(path))
        self.events.append(("chmod", str(path), mode))
        return self._real_chmod(path, mode, *args, **kwargs)

    def chown(self, path, uid, gid, *args, **kwargs):
        self.events.append(("chown", str(path), uid, gid))
        return self._real_chown(path, uid, gid, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        self.events.append(("unlink", str(path)))
        return self._real_unlink(path)

    # -------------------------------------------------------- brokers

    def helpers_cover(self, path: str | Path) -> bool:
        return self.covers

    def broker_chown(
        self,
        uid: int,
        path: str | Path,
        *,
        recursive: bool = True,
        gid: int | None = None,
    ) -> None:
        self.events.append(("broker_chown", uid, str(path), recursive))
        self._handed_over = True


def _install(monkeypatch, host: _Host) -> None:
    monkeypatch.setattr(os, "geteuid", host.geteuid)
    monkeypatch.setattr(os, "chmod", host.chmod)
    monkeypatch.setattr(os, "chown", host.chown)
    monkeypatch.setattr(os, "unlink", host.unlink)
    monkeypatch.setattr(priv_helpers, "helpers_cover", host.helpers_cover)
    monkeypatch.setattr(priv_helpers, "broker_chown", host.broker_chown)


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


def test_nonroot_worker_sets_the_mode_then_hands_the_file_over(tmp_path, monkeypatch):
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host)

    out = _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert host.events == [
        ("chmod", str(path), 0o600),
        ("broker_chown", SANDBOX_UID, str(path), False),
    ]
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


def test_root_worker_sets_the_mode_then_chowns(tmp_path, monkeypatch):
    host = _Host(euid=0, covers=True)
    _install(monkeypatch, host)

    _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert host.events == [
        ("chmod", str(path), 0o600),
        # root chowns only the uid and passes -1 for the gid, exactly as today.
        ("chown", str(path), SANDBOX_UID, -1),
    ]
    # The end state root produced all along: 0600, owned by the sandbox uid.
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.stat().st_uid == SANDBOX_UID


def test_legacy_shared_uid_shape_only_sets_the_mode(tmp_path, monkeypatch):
    """No per-sandbox uid: no identity to hand the file to, as before."""
    worker_uid = os.geteuid()
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host)

    _executor(tmp_path, host_uid=None)._materialize_http_inject(
        _http_inject_entries()
    )

    path = _expected_secret_path(tmp_path)
    assert host.events == [("chmod", str(path), 0o600)]
    assert path.stat().st_uid == worker_uid


def test_helpers_not_covering_the_path_fails_loudly(tmp_path, monkeypatch):
    """A whitelist that does not reach the secret path must refuse the create.

    Silently skipping the hand-over is the defect: the file stays worker-owned
    and the sandbox dies later, at supervise, with a permission error naming
    neither the path nor the missing whitelist root. A refusal also has to take
    the credential file with it -- a 0644 (umask) leftover in the shared image
    cache is readable by every other tenant on the host.
    """
    host = _Host(euid=WORKER_UID, covers=False)
    _install(monkeypatch, host)

    path = _expected_secret_path(tmp_path)
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    assert host.events == [
        ("chmod", str(path), 0o600),
        ("unlink", str(path)),
    ]
    assert str(excinfo.value) == (
        f"cannot hand {path} to sandbox uid {SANDBOX_UID} on a non-root "
        "worker: the file-capability broker whitelist does not contain it "
        "(E2B_IMAGE_CACHE_DIR must be one of the broker's roots)"
    )
    assert path.exists() is False
