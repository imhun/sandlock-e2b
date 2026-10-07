"""C3 Task 4 / ruling D20: **one** naming rule for the route-B slot directory.

The slot's documents live in ``<route-b root>/<uid>/<instance name>/``. The
worker's executor names that directory (``instance_name``), and the control
plane has to address it for ``scope-slot-document`` -- the step that makes the
``policy.json`` carrying the egress-proxy credentials unreadable to every other
tenant. Until the second review the two sides each had their own copy of the
rule: the worker's executor used the sandbox id (or ``sbx_<sha256[:16]>`` for a
long one), the control plane guessed ``rb-<id>`` -- so the op pointed at a
directory that exists nowhere and the slot could not start.

These tests are pinned to the **peer**, not to either side's assumption: they
drive the real executor's route-B acquire (a real ``W1SlotPool`` with a faked
spawner/channel, the production ``name=`` value) and then compare the directory
that actually appears on disk with what ``control_plane.file_ops`` derives --
for a normal sandbox id and for one longer than the rule's byte ceiling. A
self-consistent test on either side would have passed on both sides of the bug.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import envd_service.executors.sandlock as sl
import envd_service.own_identity as rb
from control_plane import file_ops
from control_plane.file_ops import ControlPaths
from envd_service.executors.base import ExecConfig
from envd_service.own_identity import OwnIdentityConfig, W1SlotPool
from gateway_common.paths import own_identity_instance_name

HOST_UID = 20007
#: Longer than ``OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES``: the rule replaces it with
#: ``sbx_<sha256[:16]>``, which is exactly the case a hand-written second copy
#: of the rule gets wrong.
LONG_ID = "sbx_" + "a" * 80


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return 0


class _FakeChannel:
    """Answers the two verbs the exchange needs (readiness + one exec)."""

    def __init__(self, handle, replies) -> None:
        self._replies = replies

    def request(self, verb, args=None, fds=()):
        reply = self._replies.get(verb, {})
        if isinstance(reply, BaseException):  # pragma: no cover - not used here
            raise reply
        return reply

    def close(self) -> None:  # pragma: no cover - nothing to close
        pass


@pytest.fixture(autouse=True)
def _own_identity_capable(monkeypatch, tmp_path):
    """The same off-Linux stand-ins ``test_sandlock_executor_route_b`` uses."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    binary = tmp_path / "sandlock-supervise"
    binary.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(rb, "default_supervise_bin", lambda: binary)
    monkeypatch.setattr(rb, "fd_client_available", lambda: True)
    monkeypatch.setattr(
        sl, "sandlock", type("FakeSandlock", (), {"minimal_dev": staticmethod(lambda: {})})
    )
    monkeypatch.setattr(sl, "ExecStdio", type("Fake", (), {"INHERIT": 0, "PIPED": 1}))
    monkeypatch.setattr(sl, "SandboxInstance", type("FakeInstance", (), {}))
    monkeypatch.setattr(sl, "_minimal_dev_mounts", lambda: {})
    rb.reset_slot_pools()
    yield
    rb.reset_slot_pools()


def _pool(tmp_path: Path) -> W1SlotPool:
    return W1SlotPool(
        uid_start=HOST_UID,
        size=2,
        # Inside the platform's state base -- the shipped shapes' invariant
        # (``<state base>/.route-b``), and the reason the CP's root check can
        # accept the derived path at all (N39's startup self-check refuses any
        # other layout).
        tmp_root=_slot_tmp_root(tmp_path),
        supervise_bin=tmp_path / "sandlock-supervise",
        spawner=lambda **kwargs: _FakeProcess(),
        channel_factory=lambda handle: _FakeChannel(
            handle,
            {
                "stats": {"launched": True, "pid": 5100},
                "exec": {"child_id": 11, "pid": 5150},
            },
        ),
        # The pool is built to exercise the *path naming* contract; the identity
        # grant is a no-op stand-in -- agent-grant is the only mode left (N52).
        identity_reporter=lambda *a: {},
        socket_timeout_s=2.0,
    )


def _executor(sandbox_id: str, tmp_path: Path):
    return sl.SandlockExecutor(
        workspace_dir=str(tmp_path / "ws" / sandbox_id),
        base_image="python:3.11-slim",
        image_rootfs=tmp_path / "rootfs",
        host_uid=HOST_UID,
        per_sandbox_uid=True,
        memory_mb=1024,
        cpu_percent=100,
        disk_mb=2048,
        max_processes=128,
        max_open_files=1024,
        allow_internet_access=False,
        enable_network=False,
        sandbox_id=sandbox_id,
        own_identity=OwnIdentityConfig(
            mode="auto",
            slots=0,
            uid_start=HOST_UID,
            uid_size=2,
            tmp_root=_slot_tmp_root(tmp_path),
            # agent-grant is the only slot-identity mode left (N52); this case
            # is about the slot's *path*, so the grant is a no-op stand-in.
            identity_reporter=lambda *a: {},
        ),
    )


def _slot_tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / ".route-b"


def _cp_paths(tmp_path: Path) -> ControlPaths:
    return ControlPaths(
        workspace_base=tmp_path / "workspaces",
        state_base=tmp_path / "state",
        slot_tmp_root=_slot_tmp_root(tmp_path),
    )


@pytest.mark.parametrize("sandbox_id", ["sbx_docs", LONG_ID], ids=["normal", "over-64-bytes"])
async def test_the_slot_directory_is_what_the_control_plane_derives(
    sandbox_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The peer pin: disk (worker's own acquire) vs derivation (CP), per id."""
    pool = _pool(tmp_path)
    monkeypatch.setattr(sl, "slot_pool_for", lambda cfg: pool)
    executor = _executor(sandbox_id, tmp_path)

    await executor.start(
        ExecConfig(
            cmd=["/bin/true"], env={}, cwd=str(tmp_path), stdin_enabled=False
        )
    )

    uid_dir = _slot_tmp_root(tmp_path) / str(HOST_UID)
    created = sorted(entry.name for entry in uid_dir.iterdir())
    assert created == [own_identity_instance_name(sandbox_id)]
    slot_dir = uid_dir / created[0]
    assert sorted(entry.name for entry in slot_dir.iterdir()) == [
        "policy.json",
        "program.json",
    ]

    instruction = file_ops.derive(
        file_ops.spec_for("scope-slot-document"),
        {"sandbox_id": sandbox_id, "name": "policy.json"},
        paths=_cp_paths(tmp_path),
        host_uid=HOST_UID,
        node_id="node_a",
        worker_gid=65534,
    )
    # The whole point: the path the control plane would hand the agent is a
    # path the worker actually created.
    assert instruction.path == str(slot_dir / "policy.json")
    assert Path(instruction.path).is_file()


def test_the_name_less_fallback_is_refused_in_the_agent_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20's check: the CP derives one name, so the worker must look at that one.

    ``W1SlotPool.acquire_sync`` still accepts a caller that names no instance
    (an embedder, a test), and that caller's directory is ``rb-<id>``. In the
    agent shape such a slot's documents are not where the control plane will
    look, so scoping them would either hand over the wrong file or point the
    agent at nothing -- refused by name instead, which is the one outcome that
    cannot be silent.
    """
    from envd_service import agent_fileops, priv_helpers

    class _Stub:
        calls: list = []

        def scope_slot_document(self, sandbox_id, name):  # pragma: no cover
            self.calls.append((sandbox_id, name))

    monkeypatch.setattr(agent_fileops, "_ACTIVE", [_Stub()])
    # The ``rb-`` spelling: what a name-less caller's directory is called.
    document = _slot_tmp_root(tmp_path) / str(HOST_UID) / "rb-sbx_docs" / "policy.json"
    document.parent.mkdir(parents=True)
    document.write_text("{}", encoding="utf-8")
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        W1SlotPool._scope_slot_document(document, HOST_UID, sandbox_id="sbx_docs")
    assert str(excinfo.value) == (
        f"the route-B slot directory for sandbox sbx_docs is "
        f"{HOST_UID}/rb-sbx_docs, but this slot's identity is "
        f"{HOST_UID}/sbx_docs: the control plane derives the slot documents' "
        "path from the shared naming rule and the leased uid, so this "
        "deployment would scope the wrong path -- refusing"
    )


def test_a_copy_under_another_uids_directory_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m-2: the leaf alone is not the address -- the leased uid is part of it.

    A stale copy of the same instance name under *another* uid's directory used
    to pass the leaf check, so the agent would have been pointed at that copy
    while the live document stayed unscoped: the same "wrong path, no error"
    shape the shared rule was introduced to remove.
    """
    from envd_service import agent_fileops, priv_helpers

    class _Stub:
        calls: list = []

        def scope_slot_document(self, sandbox_id, name):  # pragma: no cover
            self.calls.append((sandbox_id, name))

    monkeypatch.setattr(agent_fileops, "_ACTIVE", [_Stub()])
    stale = _slot_tmp_root(tmp_path) / str(HOST_UID + 1) / "sbx_docs" / "policy.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        W1SlotPool._scope_slot_document(stale, HOST_UID, sandbox_id="sbx_docs")
    assert str(excinfo.value) == (
        f"the route-B slot directory for sandbox sbx_docs is "
        f"{HOST_UID + 1}/sbx_docs, but this slot's identity is "
        f"{HOST_UID}/sbx_docs: the control plane derives the slot documents' "
        "path from the shared naming rule and the leased uid, so this "
        "deployment would scope the wrong path -- refusing"
    )
