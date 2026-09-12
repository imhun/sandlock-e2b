"""FUP #6: legacy shared-uid workspace ownership alignment (pure shape).

Pure-sandlock sandboxes (no base image) execute commands directly with the
host RunAs identity (``mediation_run_as='caller'``): there is no chroot /
supervisor mediation tier to create files on the shell's behalf, so a root
worker that provisions the workspace as root:root makes the sandbox shell
unable to write its own root directory (gate-B migration trio). The
alignment helpers below chown such workspaces to the shared RunAs uid —
mirroring what ``apply_sandbox_ownership`` already does for per-sandbox
uids — without touching shared volumes or widening permissions.

The pure decision parts run everywhere; real-chown coverage runs as root
(the Docker runner).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from envd_service.executors.sandlock import SandlockExecutor
from envd_service.uid_pool import (
    LEGACY_SHARED_UID,
    _alignment_target_uid,
    align_shared_uid_workspace,
)


def test_non_root_worker_needs_no_alignment():
    """A non-root worker creates the workspace as its own identity, which is
    also the RunAs identity (S1.2 / E5.1) — nothing to chown."""
    assert _alignment_target_uid(worker_euid=12345, owner_uid=0) is None


def test_root_worker_aligns_root_owned_workspace():
    assert (
        _alignment_target_uid(worker_euid=0, owner_uid=0)
        == LEGACY_SHARED_UID
    )


def test_root_worker_leaves_existing_owner_untouched():
    """A workspace already owned by another identity (previous provision,
    external storage owner) is never fought."""
    assert _alignment_target_uid(worker_euid=0, owner_uid=10000) is None


def test_shared_uid_constant_matches_legacy_run_as_identity(monkeypatch):
    """The provisioning constant must not drift from the executor's RunAs
    decision for the same worker."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getegid", lambda: 0)
    executor = SandlockExecutor(
        workspace_dir=".",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    assert executor._run_as_identity() == (LEGACY_SHARED_UID, LEGACY_SHARED_UID)


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_align_shared_uid_workspace_chowns_root_owned_tree(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    (ws / "sub" / "f.txt").write_text("x", encoding="utf-8")
    os.chown(ws, 0, 0)
    os.chown(ws / "sub", 0, 0)
    os.chown(ws / "sub" / "f.txt", 0, 0)

    align_shared_uid_workspace(ws)
    assert ws.stat().st_uid == LEGACY_SHARED_UID
    assert stat.S_IMODE(ws.stat().st_mode) == 0o770
    assert ws.stat().st_gid == os.getegid()
    assert (ws / "sub").stat().st_uid == LEGACY_SHARED_UID
    assert (ws / "sub" / "f.txt").stat().st_uid == LEGACY_SHARED_UID


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_align_shared_uid_workspace_skips_non_root_owner(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    os.chown(ws, 1234, 1234)

    align_shared_uid_workspace(ws)
    assert ws.stat().st_uid == 1234
