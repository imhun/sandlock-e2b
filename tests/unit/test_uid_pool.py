"""E3.2: worker host-uid pool allocation, persistence, and reconciliation."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from envd_service.executors.sandlock import SandlockExecutor
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


def test_acquire_across_pool_instances_does_not_collide(tmp_path):
    """I1 regression: two pool instances sharing one workspace (separate
    workers) must not hand out the same uid while the first create's record
    is not yet durable. acquire reserves atomically via a marker file that
    the other pool's free-set scan must honor."""
    pool_a = _pool(tmp_path)
    pool_b = _pool(tmp_path)
    assert pool_a.acquire("sbx_a") == POOL_START
    # pool_b cannot see pool_a's in-memory state; the on-disk reservation
    # marker must keep it away from 10000 even before any record exists.
    assert pool_b.acquire("sbx_b") == POOL_START + 1
    # Both creates persist their records, then commit drops the markers.
    _write_record(tmp_path, "sbx_a", POOL_START)
    _write_record(tmp_path, "sbx_b", POOL_START + 1)
    pool_a.commit("sbx_a")
    pool_b.commit("sbx_b")
    # Deletion releases both uids (records removed by rmtree); the lowest
    # uid is reusable again by either pool.
    pool_a.release("sbx_a")
    pool_b.release("sbx_b")
    (tmp_path / "sbx_a" / "sandbox.json").unlink()
    (tmp_path / "sbx_b" / "sandbox.json").unlink()
    assert pool_b.acquire("sbx_c") == POOL_START


def test_acquire_concurrent_across_pool_instances_no_collision(tmp_path):
    """I1 concurrency: simultaneous acquires from two pool instances
    (simulating separate workers racing through the acquire window) never
    return the same uid — the shared flock serializes the free-set
    computation and the marker keeps every picked uid out of the other
    pool's view."""
    pool_a = UidPool(start=POOL_START, size=8, workspace_base=tmp_path)
    pool_b = UidPool(start=POOL_START, size=8, workspace_base=tmp_path)
    barrier = threading.Barrier(2)
    uids_a: list[int] = []
    uids_b: list[int] = []
    errors: list[BaseException] = []

    def worker(pool, uids, prefix):
        try:
            for i in range(3):
                barrier.wait(timeout=10)
                uids.append(pool.acquire(f"{prefix}_{i}"))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    ta = threading.Thread(target=worker, args=(pool_a, uids_a, "a"))
    tb = threading.Thread(target=worker, args=(pool_b, uids_b, "b"))
    ta.start()
    tb.start()
    ta.join(20)
    tb.join(20)
    assert not ta.is_alive() and not tb.is_alive()
    assert errors == []
    all_uids = uids_a + uids_b
    assert len(all_uids) == 6
    assert len(all_uids) == len(set(all_uids))


def test_release_without_commit_frees_uid_for_other_pool(tmp_path):
    """I3 pool contract: an abandoned create (release before any record was
    persisted) frees both the reservation marker and the uid, so another
    pool instance can hand the same uid out again."""
    pool_a = _pool(tmp_path)
    pool_b = _pool(tmp_path)
    assert pool_a.acquire("sbx_a") == POOL_START
    pool_a.release("sbx_a")
    assert pool_b.acquire("sbx_b") == POOL_START


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


def test_reconcile_then_delete_releases_preexisting_uid(tmp_path):
    """I2 regression: reconcile rebuilds the sandbox→uid map from disk
    records, so deleting a pre-restart sandbox (no in-process acquire)
    releases its uid instead of leaking it in `_allocated` until restart."""
    _write_record(tmp_path, "sbx_live", POOL_START)
    _write_record(tmp_path, "sbx_other", POOL_START + 1)
    pool = _pool(tmp_path)
    result = pool.reconcile()
    assert result["referenced"] == [POOL_START, POOL_START + 1]
    # Delete sbx_live: unregister -> release, then rmtree drops the record.
    pool.release("sbx_live")
    (tmp_path / "sbx_live" / "sandbox.json").unlink()
    # The freed uid is the lowest one again, not POOL_START + 2.
    assert pool.acquire("sbx_new") == POOL_START
    # sbx_other is still live: its uid must not be handed out.
    assert pool.acquire("sbx_after") == POOL_START + 2


def test_reconcile_clears_stale_reservation_markers(tmp_path):
    """A create that crashed between acquire and register leaves only a
    reservation marker (no record): startup reconcile must free the uid."""
    pool = _pool(tmp_path)
    assert pool.acquire("sbx_crashed") == POOL_START
    # Crash: no record is ever written, marker survives the process.
    marker_dir = tmp_path / ".uid_reservations"
    assert (marker_dir / "sbx_crashed").is_file()
    result = pool.reconcile()
    assert result["referenced"] == []
    assert not (marker_dir / "sbx_crashed").exists()
    assert pool.acquire("sbx_new") == POOL_START


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


def _legacy_executor(workspace: Path) -> SandlockExecutor:
    return SandlockExecutor(
        workspace_dir=str(workspace),
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


def test_legacy_run_as_keeps_1000_for_root(monkeypatch, tmp_path):
    """I4: root workers keep the historical shared host uid 1000."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getegid", lambda: 0)
    assert _legacy_executor(tmp_path)._run_as_identity() == (1000, 1000)


def test_legacy_run_as_falls_back_to_worker_identity_when_non_root(
    monkeypatch, tmp_path
):
    """I4 regression: with the per-sandbox uid switch off, a non-root worker
    must map its own euid/egid (S1.2 maps only the caller's identity) instead
    of hardcoding 1000, which the sandlock fail-closed check would reject."""
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    monkeypatch.setattr(os, "getegid", lambda: 12345)
    assert _legacy_executor(tmp_path)._run_as_identity() == (12345, 12345)


# ---------------------------------------------------------------- the default

def test_per_sandbox_uid_is_the_deployment_default(monkeypatch) -> None:
    """E3.2 identity is the base the rest of the isolation model sits on
    (shared-volume sticky protection, and route B's per-uid supervise slot),
    so it ships on; the env var stays as the explicit opt-out."""
    from envd_service.config import Settings

    monkeypatch.delenv("E2B_PER_SANDBOX_UID", raising=False)
    assert Settings().per_sandbox_uid is True
    monkeypatch.setenv("E2B_PER_SANDBOX_UID", "false")
    assert Settings().per_sandbox_uid is False


def test_non_root_worker_keeps_the_fixed_identity_model(tmp_path, caplog) -> None:
    """Flipping the default must not crash-loop an unprivileged worker.

    A non-root worker cannot map host uids (S1.2) or chown, so the pool is
    never constructed and the reason is said out loud once.
    """
    import os

    from envd_service.app import create_app as create_envd_app
    from envd_service.config import Settings as EnvdSettings
    from envd_service.runtime.registry import RuntimeRegistry

    if os.geteuid() == 0:  # the degrade path is what is under test
        pytest.skip("the non-root worker shape cannot be asserted as root")

    registry = RuntimeRegistry(tmp_path)
    settings = EnvdSettings(
        executor="local", workspace_base=tmp_path, base_image=None
    )
    with caplog.at_level("WARNING", logger="envd_service.app"):
        app = create_envd_app(
            settings=settings,
            runtime_registry=registry,
            workspace_base=tmp_path,
        )
    assert app is not None
    assert registry.uid_pool is None
    assert any(
        "E2B_PER_SANDBOX_UID is enabled but the worker is not running as root"
        in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]


def test_root_worker_gets_the_pool_by_default(tmp_path, monkeypatch) -> None:
    """The other half of the default: a privileged worker really does allocate
    -- `host_uid` only exists when both conditions hold."""
    import os

    from envd_service.app import create_app as create_envd_app
    from envd_service.config import Settings as EnvdSettings
    from envd_service.runtime.registry import RuntimeRegistry

    if os.geteuid() != 0:
        pytest.skip("the root worker shape needs privilege")

    registry = RuntimeRegistry(tmp_path)
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=registry,
        workspace_base=tmp_path,
    )
    assert app is not None
    assert registry.uid_pool is not None
    assert registry.uid_pool.start == EnvdSettings().uid_pool_start
