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
one ordered event list *and* refuses what the kernel would refuse once a path
belongs to a pooled uid -- a late ``chmod`` is ``EPERM``, and a later
``open(w)`` is ``EACCES`` (the second review's regression: the *same* secret is
written again on every policy rebuild, so handing it over once used to make
every reopen -- idle expiry, a dead machinery, a fresh ceiling -- fail at
``open``).

The mode is also pinned to land while the worker is still the owner, and a
refusal (a broker whitelist that does not cover the path) must not leave a
umask-mode credential file behind.

Three more properties are pinned here. *Nothing lands on disk before every
entry has resolved its value*: a later entry that cannot resolve (a missing IAM
token) leaves the earlier entry's file uncreated, so a failed build never
leaves a half-published set behind. The file is *created* at 0600 by the very
``open`` (no window between a 0666 create and a compensating ``chmod``). And the
reclaim of a name a previous build handed to a pooled uid checks its parent
directory first -- owned by this worker, no sticky bit -- so a parent someone
re-chowned or made sticky is named instead of surfacing later as an opaque
``EACCES``.

Every expectation therefore starts with ``("unlink", path)``: publishing a
secret always begins by reclaiming the name (``unlink(missing_ok=True)``), which
is the only step left to a worker once a previous build handed that file to a
pooled uid -- and a pure no-op syscall on the first build.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from envd_service import priv_helpers
from envd_service.executors import sandlock as sandlock_module
from envd_service.executors.sandlock import SandlockExecutor
from gateway_common.network import sandlock_network_policy

SANDBOX_UID = 21001
WORKER_UID = 65534

#: One literal ``transform.headers`` rule: the producer turns this into the
#: ``http_inject`` entry whose value the executor writes to a secret file.
_HEADER_NETWORK = {
    "allowOut": ["api.example.com"],
    "rules": {
        "api.example.com": [{"transform": {"headers": {"X-API-Key": "sk-literal"}}}]
    },
}


class _Host:
    """The worker's syscalls plus the brokers, one ordered event list.

    Only the calls the executor itself makes are recorded, and they carry the
    arguments it chose -- so the assertion is on the sequence the reader
    (``supervise``) depends on. On top of that, the two rules the kernel
    enforces once a file belongs to a pooled uid are emulated for a non-root
    worker: ``chmod`` is ``EPERM`` (no ownership, no ``CAP_FOWNER``) and
    ``open(w)`` is ``EACCES`` (no ownership, no ``CAP_DAC_OVERRIDE``).
    """

    def __init__(self, *, euid: int, covers: bool) -> None:
        self.euid = euid
        self.covers = covers
        self.events: list[tuple] = []
        #: The one directory tree whose reported owner is *this* worker's: the
        #: emulated non-root worker is the one that creates it, while on disk
        #: it belongs to whoever runs the suite. Set by ``_install``.
        self.owned_prefix: str | None = None
        #: Per-directory uid overrides -- how a hostile case says "somebody
        #: chowned this parent away from the worker" without needing a real
        #: second uid (the suite runs as root).
        self.forced_dir_uid: dict[str, int] = {}
        self._foreign: set[str] = set()
        self._real_chmod = os.chmod
        self._real_chown = os.chown
        self._real_unlink = os.unlink
        self._real_open = open
        self._real_os_open = os.open
        self._real_stat = os.stat

    # ------------------------------------------------------- syscalls

    def geteuid(self) -> int:
        return self.euid

    def _not_ours(self, path) -> bool:
        """True when the path belongs to a pooled uid this worker is not."""
        return self.euid != 0 and str(path) in self._foreign

    def open(self, path, mode="r", *args, **kwargs):
        if self._not_ours(path) and any(c in mode for c in "wax+"):
            raise PermissionError(13, "Permission denied", str(path))
        return self._real_open(path, mode, *args, **kwargs)

    def os_open(self, path, flags, mode=0o777, *args, **kwargs):
        """``os.open`` with the mode recorded: the create itself sets 0600."""
        if self._not_ours(path) and flags & (os.O_WRONLY | os.O_RDWR):
            raise PermissionError(13, "Permission denied", str(path))
        if flags & os.O_CREAT:
            self.events.append(("open", str(path), mode))
        return self._real_os_open(path, flags, mode, *args, **kwargs)

    def stat(self, path, *args, **kwargs):
        """``os.stat`` with directory ownership emulated for this worker.

        Only directories are rewritten: the emulated uid is a fiction for the
        suite's own process, and the executor's ownership question is always
        about the *parent directory* a write/reclaim lands in.
        """
        result = self._real_stat(path, *args, **kwargs)
        if not stat.S_ISDIR(result.st_mode):
            return result
        forced = self.forced_dir_uid.get(str(path))
        if (
            forced is None
            and self.owned_prefix is not None
            and str(path).startswith(self.owned_prefix)
        ):
            forced = self.euid
        if forced is None:
            return result
        fields = list(result)
        fields[4] = forced  # st_uid
        return os.stat_result(fields)

    def chmod(self, path, mode, *args, **kwargs):
        if self._not_ours(path):
            raise PermissionError(1, "Operation not permitted", str(path))
        self.events.append(("chmod", str(path), mode))
        return self._real_chmod(path, mode, *args, **kwargs)

    def chown(self, path, uid, gid, *args, **kwargs):
        self.events.append(("chown", str(path), uid, gid))
        return self._real_chown(path, uid, gid, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        self.events.append(("unlink", str(path)))
        self._foreign.discard(str(path))
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
        self._foreign.add(str(path))


def _install(monkeypatch, host: _Host, secrets_root: Path) -> None:
    host.owned_prefix = str(secrets_root)
    monkeypatch.setattr(os, "geteuid", host.geteuid)
    monkeypatch.setattr(os, "chmod", host.chmod)
    monkeypatch.setattr(os, "chown", host.chown)
    monkeypatch.setattr(os, "unlink", host.unlink)
    monkeypatch.setattr(os, "open", host.os_open)
    monkeypatch.setattr(os, "stat", host.stat)
    # The executor creates the file with ``os.open`` (recorded above) and
    # hands the descriptor to ``os.fdopen``. The builtin ``open`` is shadowed
    # through the executor's module globals as well, so a reverted
    # ``open(path, "w")`` still meets the kernel rules above.
    monkeypatch.setattr(sandlock_module, "open", host.open, raising=False)
    monkeypatch.setattr(priv_helpers, "helpers_cover", host.helpers_cover)
    monkeypatch.setattr(priv_helpers, "broker_chown", host.broker_chown)


def _executor(
    tmp_path: Path,
    *,
    host_uid: int | None = SANDBOX_UID,
    network: dict | None = None,
) -> SandlockExecutor:
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
        network=network,
        secrets_dir=tmp_path / "secrets",
    )


def _http_inject_entries() -> list[dict]:
    """One literal ``transform.headers`` entry, mapped by the real producer."""
    entries = sandlock_network_policy(
        _HEADER_NETWORK,
        allow_internet_access=False,
        enable_network=True,
    )["http_inject"]
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
    _install(monkeypatch, host, tmp_path / "secrets")

    out = _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert host.events == [
        ("unlink", str(path)),
        ("open", str(path), 0o600),
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


def test_a_rebuilt_policy_rewrites_the_secret_it_handed_over(tmp_path, monkeypatch):
    """The second build of the same secret is the normal path, not a corner.

    ``_policy_ceiling()`` is not cached: every route-B open and every reopen
    (idle expiry, a dead machinery, a fresh ceiling) materializes the
    ``http_inject`` entries again. The file on disk is the *pool uid's* by then,
    so a worker that only knows how to ``open(w)`` it would die with EACCES on
    the first reopen -- one instance lifetime per sandbox.
    """
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")
    executor = _executor(tmp_path, network=_HEADER_NETWORK)
    path = _expected_secret_path(tmp_path)

    executor._policy_ceiling()

    assert host.events == [
        ("unlink", str(path)),
        ("open", str(path), 0o600),
        ("broker_chown", SANDBOX_UID, str(path), False),
    ]

    host.events.clear()
    executor._policy_ceiling()

    assert host.events == [
        ("unlink", str(path)),
        ("open", str(path), 0o600),
        ("broker_chown", SANDBOX_UID, str(path), False),
    ]
    assert path.read_text(encoding="utf-8") == "sk-literal"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_root_worker_sets_the_mode_then_chowns(tmp_path, monkeypatch):
    host = _Host(euid=0, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")

    _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert host.events == [
        ("unlink", str(path)),
        ("open", str(path), 0o600),
        # root chowns only the uid and passes -1 for the gid, exactly as today.
        ("chown", str(path), SANDBOX_UID, -1),
    ]
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_legacy_shared_uid_shape_only_sets_the_mode(tmp_path, monkeypatch):
    """No per-sandbox uid: no identity to hand the file to, as before."""
    worker_uid = os.geteuid()
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")

    _executor(tmp_path, host_uid=None)._materialize_http_inject(
        _http_inject_entries()
    )

    path = _expected_secret_path(tmp_path)
    assert host.events == [
        ("unlink", str(path)),
        ("open", str(path), 0o600),
    ]
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
    _install(monkeypatch, host, tmp_path / "secrets")

    path = _expected_secret_path(tmp_path)
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    assert host.events == [
        ("unlink", str(path)),  # the reclaim every build starts with
        ("open", str(path), 0o600),
        ("unlink", str(path)),  # ...and the refusal taking the file with it
    ]
    assert str(excinfo.value) == (
        f"cannot hand {path} to sandbox uid {SANDBOX_UID} on a non-root "
        "worker: the file-capability broker whitelist does not contain it "
        "(E2B_IMAGE_CACHE_DIR must be one of the broker's roots)"
    )
    assert path.exists() is False


def _two_entries() -> list[dict]:
    """A literal header and a second one whose placeholder cannot resolve."""
    return [
        {
            "matcher": "api.example.com",
            "auth": "header:X-First-Key",
            "value": "sk-first",
            "name": "hdr_first",
            "on_existing": "replace",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Second-Key",
            "value": "${e2b.identity.tokens.NOBODY}",
            "name": "hdr_second",
            "on_existing": "replace",
        },
    ]


def test_a_later_entry_that_cannot_resolve_lands_nothing_on_disk(
    tmp_path, monkeypatch
):
    """Resolve every value first, publish afterwards: all or nothing.

    Entry 1 is a literal that would publish cleanly and entry 2 names an IAM
    token nothing backs. Publishing per entry (the old order) left entry 1's
    file on disk *and already chowned to the sandbox uid* before entry 2
    raised -- a credential left behind by a create that failed before the
    sandbox ever existed. The failure has to happen while nothing has landed.
    """
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")
    monkeypatch.delenv("E2B_IDENTITY_TOKEN_NOBODY", raising=False)

    first = tmp_path / "secrets" / "sbx_1" / "hdr_first.secret"
    with pytest.raises(RuntimeError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_two_entries())

    assert host.events == []
    assert first.exists() is False
    assert str(excinfo.value) == (
        "header transform for api.example.com references identity token "
        "'NOBODY' but E2B_IDENTITY_TOKEN_NOBODY is not set (and no iam token "
        "named 'NOBODY' was registered)"
    )


def test_the_secret_is_created_at_0600_without_a_umask_window(tmp_path, monkeypatch):
    """The create itself carries 0600 -- there is no ``open`` then ``chmod``.

    ``open(path, "w")`` creates at ``0666 & ~umask`` (0644 under the usual
    022) and the following ``chmod`` is what made it 0600, so a credential
    file existed, world-readable, for as long as those two calls were apart.
    Handing the descriptor to ``os.fdopen`` with the mode on ``os.open``
    removes the window: the mode is a creation argument, and the event list
    carries the create (with its mode) instead of a late ``chmod``.
    """
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")

    _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    path = _expected_secret_path(tmp_path)
    assert ("open", str(path), 0o600) in host.events
    assert [event for event in host.events if event[0] == "chmod"] == []
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_parent_directory_owned_by_someone_else_refuses_the_reclaim(
    tmp_path, monkeypatch
):
    """``unlink`` only works because the parent is the worker's own.

    Reclaiming a name a previous build handed to a pooled uid asks for write
    permission on the *parent*, which is ``<secrets>/<sandbox>`` -- the
    worker's own non-sticky directory. If something re-chowned it (a stray
    root step, a manual fix), the ``unlink`` no longer holds and the next
    ``open`` would fail as a bare ``EACCES`` naming neither the directory nor
    the contract. Refuse first, and name who broke it.
    """
    secret_dir = tmp_path / "secrets" / "sbx_1"
    secret_dir.mkdir(parents=True)
    secret_dir.chmod(0o755)
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")
    host.forced_dir_uid[str(secret_dir)] = 0

    path = _expected_secret_path(tmp_path)
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    assert host.events == []
    assert path.exists() is False
    assert str(excinfo.value) == (
        f"refusing to reclaim {path}: the parent directory {secret_dir} is "
        "not this worker's own non-sticky directory (owner uid 0, mode "
        "0o755): reclaiming a handed-over name needs a parent owned by the "
        "worker with no sticky bit -- who chowned it or set its mode?"
    )


def test_a_sticky_parent_directory_refuses_the_reclaim(tmp_path, monkeypatch):
    """A sticky parent is the other half of the same contract.

    The worker owns the directory here, but the sticky bit means another uid
    could still own a file in it that this worker may not remove -- so the
    reclaim precondition is "owned by us *and* not sticky", checked, not
    assumed.
    """
    secret_dir = tmp_path / "secrets" / "sbx_1"
    secret_dir.mkdir(parents=True)
    secret_dir.chmod(0o1777)
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")

    path = _expected_secret_path(tmp_path)
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        _executor(tmp_path)._materialize_http_inject(_http_inject_entries())

    assert host.events == []
    assert str(excinfo.value) == (
        f"refusing to reclaim {path}: the parent directory {secret_dir} is "
        "not this worker's own non-sticky directory (owner uid "
        f"{WORKER_UID}, mode 0o1777): reclaiming a handed-over name needs a "
        "parent owned by the worker with no sticky bit -- who chowned it or "
        "set its mode?"
    )


def _mixed_entries() -> list[dict]:
    """env -> file -> env -> file: both sources, alternating."""
    return [
        {
            "matcher": "api.example.com",
            "auth": "header:X-First-Key",
            "value": "${e2b.identity.tokens.FIRST}",
            "name": "hdr_0_env",
            "on_existing": "replace",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Second-Key",
            "value": "sk-second",
            "name": "hdr_1_file",
            "on_existing": "replace",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Third-Key",
            "value": "${e2b.identity.tokens.THIRD}",
            "name": "hdr_2_env",
            "on_existing": "replace",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Fourth-Key",
            "value": "sk-fourth",
            "name": "hdr_3_file",
            "on_existing": "replace",
        },
    ]


def test_env_and_file_entries_keep_their_input_order(tmp_path, monkeypatch):
    """Resolving first must not reorder what the caller configured.

    The two passes exist to make publishing all-or-nothing, and an env-backed
    entry is finished in the first pass (it becomes ``env:<VAR>`` and never
    touches the disk). Appending it there while file-backed entries wait for
    the second pass turned ``[env, file, env, file]`` into
    ``[env, env, file, file]`` -- the same values, in an order the caller did
    not ask for, so two rules for one matcher/header would flip which one wins.
    """
    host = _Host(euid=WORKER_UID, covers=True)
    _install(monkeypatch, host, tmp_path / "secrets")
    monkeypatch.setenv("E2B_IDENTITY_TOKEN_FIRST", "jwt-first")
    monkeypatch.setenv("E2B_IDENTITY_TOKEN_THIRD", "jwt-third")

    out = _executor(tmp_path)._materialize_http_inject(_mixed_entries())

    secret_dir = tmp_path / "secrets" / "sbx_1"
    assert [entry["name"] for entry in out] == [
        "hdr_0_env",
        "hdr_1_file",
        "hdr_2_env",
        "hdr_3_file",
    ]
    assert out == [
        {
            "matcher": "api.example.com",
            "auth": "header:X-First-Key",
            "name": "hdr_0_env",
            "on_existing": "replace",
            "secret": "env:E2B_IDENTITY_TOKEN_FIRST",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Second-Key",
            "name": "hdr_1_file",
            "on_existing": "replace",
            "secret": f"file:{secret_dir / 'hdr_1_file.secret'}",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Third-Key",
            "name": "hdr_2_env",
            "on_existing": "replace",
            "secret": "env:E2B_IDENTITY_TOKEN_THIRD",
        },
        {
            "matcher": "api.example.com",
            "auth": "header:X-Fourth-Key",
            "name": "hdr_3_file",
            "on_existing": "replace",
            "secret": f"file:{secret_dir / 'hdr_3_file.secret'}",
        },
    ]
    assert host.events == [
        ("unlink", str(secret_dir / "hdr_1_file.secret")),
        ("open", str(secret_dir / "hdr_1_file.secret"), 0o600),
        ("broker_chown", SANDBOX_UID, str(secret_dir / "hdr_1_file.secret"), False),
        ("unlink", str(secret_dir / "hdr_3_file.secret")),
        ("open", str(secret_dir / "hdr_3_file.secret"), 0o600),
        ("broker_chown", SANDBOX_UID, str(secret_dir / "hdr_3_file.secret"), False),
    ]
