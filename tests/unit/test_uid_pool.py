"""E3.2: worker host-uid pool allocation, persistence, and reconciliation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from envd_service.runtime.registry import RuntimeRegistry
from envd_service.uid_pool import (
    UidPool,
    UidPoolError,
    apply_sandbox_ownership,
)

POOL_START = 10000
POOL_SIZE = 4
POOL_END = POOL_START + POOL_SIZE


def _pool(workspace: Path) -> UidPool:
    return UidPool(start=POOL_START, size=POOL_SIZE, workspace_base=workspace)


def _write_record(workspace: Path, sandbox_id: str, host_uid: int | None) -> None:
    record = workspace / sandbox_id / "sandbox.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"sandbox_id": sandbox_id, "host_uid": host_uid}),
        encoding="utf-8",
    )


def test_acquire_allocates_sequential_and_exhausts(tmp_path):
    pool = _pool(tmp_path)
    assert [pool.acquire(f"sbx_{i}") for i in range(POOL_SIZE)] == [
        POOL_START + i for i in range(POOL_SIZE)
    ]
    with pytest.raises(
        UidPoolError,
        match=(
            rf"uid pool exhausted \({POOL_START}\.\.{POOL_END - 1}\)"
        ),
    ):
        pool.acquire("sbx_overflow")


def test_acquire_skips_uids_referenced_by_records(tmp_path):
    _write_record(tmp_path, "sbx_other", POOL_START + 1)
    pool = _pool(tmp_path)
    assert pool.acquire("sbx_a") == POOL_START
    assert pool.acquire("sbx_b") == POOL_START + 2


def test_acquire_reuses_persisted_record_uid(tmp_path):
    _write_record(tmp_path, "sbx_a", POOL_START + 2)
    pool = _pool(tmp_path)
    assert pool.acquire("sbx_a") == POOL_START + 2


def test_acquire_honors_preferred_in_range(tmp_path):
    pool = _pool(tmp_path)
    assert pool.acquire("sbx_a", preferred=POOL_START + 2) == POOL_START + 2


def test_acquire_ignores_preferred_outside_pool(tmp_path):
    pool = _pool(tmp_path)
    assert pool.acquire("sbx_a", preferred=20000) == POOL_START


def test_release_reclaims_uid_for_reuse(tmp_path):
    pool = _pool(tmp_path)
    uid = pool.acquire("sbx_a")
    pool.release("sbx_a")
    pool.release("sbx_a")  # idempotent
    assert pool.acquire("sbx_b") == uid


def test_registry_persists_host_uid(tmp_path):
    registry = RuntimeRegistry(tmp_path)
    registry.register(
        sandbox_id="sbx_a",
        access_token="token",
        workspace_dir=str(tmp_path / "sbx_a"),
        host_uid=POOL_START,
    )
    payload = json.loads(
        (tmp_path / "sbx_a" / "sandbox.json").read_text(encoding="utf-8")
    )
    assert payload["host_uid"] == POOL_START
    loaded = RuntimeRegistry(tmp_path).get("sbx_a")
    assert loaded is not None
    assert loaded.host_uid == POOL_START


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_apply_sandbox_ownership_chowns_tree_and_tightens_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    (ws / "sub" / "f.txt").write_text("x", encoding="utf-8")
    apply_sandbox_ownership(ws, POOL_START)
    assert ws.stat().st_uid == POOL_START
    assert stat.S_IMODE(ws.stat().st_mode) == 0o700
    assert (ws / "sub").stat().st_uid == POOL_START
    assert (ws / "sub" / "f.txt").stat().st_uid == POOL_START


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_reconcile_reclaims_orphan_uid_and_chowns_stale_dir(tmp_path):
    stale = tmp_path / "sbx_orphan"
    stale.mkdir()
    leftover = stale / "leftover.txt"
    leftover.write_text("x", encoding="utf-8")
    os.chown(stale, POOL_START, POOL_START)
    os.chown(leftover, POOL_START, POOL_START)
    pool = _pool(tmp_path)
    result = pool.reconcile()
    assert result["referenced"] == []
    assert result["reclaimed"] == [POOL_START]
    assert result["cleaned"] == [
        {"uid": POOL_START, "path": str(stale)}
    ]
    assert stale.stat().st_uid == os.geteuid()
    assert leftover.stat().st_uid == os.geteuid()
    assert pool.acquire("sbx_new") == POOL_START


def test_reconcile_preserves_recorded_uids(tmp_path):
    _write_record(tmp_path, "sbx_live", POOL_START + 1)
    pool = _pool(tmp_path)
    result = pool.reconcile()
    assert result["referenced"] == [POOL_START + 1]
    assert result["reclaimed"] == []
    assert pool.acquire("sbx_a") == POOL_START
    assert pool.acquire("sbx_b") == POOL_START + 2


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_reconcile_ignores_dirs_owned_outside_pool(tmp_path):
    stale = tmp_path / "sbx_x"
    stale.mkdir()
    os.chown(stale, 1234, 1234)
    pool = _pool(tmp_path)
    result = pool.reconcile()
    assert result["reclaimed"] == []
    assert stale.stat().st_uid == 1234


def test_reconcile_missing_base_is_empty(tmp_path):
    pool = UidPool(
        start=POOL_START, size=POOL_SIZE, workspace_base=tmp_path / "missing"
    )
    result = pool.reconcile()
    assert result == {
        "referenced": [],
        "reclaimed": [],
        "cleaned": [],
        "skipped": [],
    }
