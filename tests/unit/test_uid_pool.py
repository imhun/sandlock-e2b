"""E3.2: worker host-uid pool allocation, persistence, and reconciliation."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from envd_service import uid_pool
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


def _snapshot_store(base: Path, name: str) -> Path:
    """The shape ``SnapshotRegistry`` writes: ``snapshot.json`` + ``fs/``.

    ``SnapshotRegistry`` is built on the platform's *shared export root*
    (``control_plane/app.py``: ``platform_root = settings.shared_workspace_root``
    ⇒ ``<export>/_snapshots``), **not** the workspace base -- this test just
    hands one root to both, which is the shape where the two coincide. So a
    store can sit at the top level next to the sandbox trees and its name passes
    ``validate_sandbox_id``. The record it carries is the *copied sandbox's*,
    at ``fs/sandbox.json`` -- never at the top level -- and it claims a uid of
    its own, so reading it would pin that uid for the wrong reason.
    """
    store = base / name
    (store / "fs").mkdir(parents=True)
    (store / "snapshot.json").write_text("{}", encoding="utf-8")
    (store / "fs" / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_snapshotted", "host_uid": POOL_START}),
        encoding="utf-8",
    )
    return store


def _pretend_owned(monkeypatch, owners: dict[Path, int]) -> None:
    """Report the given directories as owned by a pool uid.

    The reclaim branch only fires for a directory whose *owner* is a
    pool-range uid, and a non-root test process cannot create one (``chown``
    is root-only), so that single syscall is faked rather than skipping the
    case off root.
    """
    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        uid = owners.get(self)
        if uid is None:
            return result
        fields = list(result)
        fields[4] = uid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", fake_stat)


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
    # The record lives beside the tree (``_runtime/<id>/``) since the split:
    # inside the tree it was a file the sandbox itself could unlink and
    # rewrite, and it must not be able to touch the platform's copy.
    record_path = tmp_path / "_runtime" / "sbx_a" / "sandbox.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload["host_uid"] == POOL_START
    assert not (tmp_path / "sbx_a" / "sandbox.json").exists()
    # Traversable **by name** (0711), not listable: the sandbox's own uid is
    # not the owner, so 0700 kept it out -- and also kept out the route-B slot
    # that has to read the disk-accounting file beside this record. What closes
    # the record is its own mode, not the directory (see
    # ``test_disk_stats_publish.test_only_the_ledger_is_world_readable_in_the_runtime_dir``).
    assert stat.S_IMODE(record_path.parent.stat().st_mode) == 0o711
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    loaded = RuntimeRegistry(tmp_path).get("sbx_a")
    assert loaded is not None
    assert loaded.host_uid == POOL_START


def test_a_uid_is_not_lost_while_the_registry_record_is_rewritten(
    tmp_path, publish_spy
):
    """Two workers share the workspace: one writes the record, one allocates.

    ``uid_pool._recorded_uid`` answers "which host uid does this sandbox
    hold?" from ``_runtime/<id>/sandbox.json``, and it reads an unparsable
    file as *no record* (``JSONDecodeError`` -> ``None``). ``_recorded_uids``
    then builds "this uid is already taken" out of exactly that answer, so a
    worker whose allocator catches a peer's record mid-rewrite can hand the
    same host uid to a second sandbox -- E3.2's per-sandbox isolation gone
    for as long as the window is open, and only visible as two sandboxes that
    can read each other's files.
    """
    (tmp_path / "sbx_a").mkdir()
    registry = RuntimeRegistry(tmp_path)
    registry.register(
        sandbox_id="sbx_a",
        access_token="token-a",
        workspace_dir=str(tmp_path / "sbx_a"),
        host_uid=POOL_START,
    )
    record_path = tmp_path / "_runtime" / "sbx_a" / "sandbox.json"
    assert uid_pool._recorded_uid(tmp_path, "sbx_a") == POOL_START
    publish_spy.reset()

    with publish_spy.hold_next_publish() as in_window:
        writer = threading.Thread(
            target=registry.register,
            kwargs=dict(
                sandbox_id="sbx_a",
                access_token="token-b",
                workspace_dir=str(tmp_path / "sbx_a"),
                host_uid=POOL_START + 1,
            ),
            daemon=True,
        )
        writer.start()
        publish_spy.await_publish(in_window, "a runtime record")
        # The allocator on the *other* worker, standing in the window: it has
        # to still see sbx_a's uid as taken (and as the one it was told).
        assert uid_pool._recorded_uid(tmp_path, "sbx_a") == POOL_START
        assert uid_pool._recorded_uids(tmp_path, POOL_START, POOL_SIZE) == {
            POOL_START
        }
    writer.join(timeout=10)
    assert not writer.is_alive()
    assert uid_pool._recorded_uid(tmp_path, "sbx_a") == POOL_START + 1
    assert uid_pool._recorded_uids(tmp_path, POOL_START, POOL_SIZE) == {
        POOL_START + 1
    }

    # And why that window is a *silent* failure rather than a degraded mode:
    # half a document is indistinguishable from "sbx_a holds no uid", so the
    # allocator would report the uid as free.
    record_path.write_text('{"sandbox_id": "sbx_a", "host_uid": 10', encoding="utf-8")
    assert uid_pool._recorded_uid(tmp_path, "sbx_a") is None
    assert uid_pool._recorded_uids(tmp_path, POOL_START, POOL_SIZE) == set()


@pytest.mark.skipif(os.geteuid() != 0, reason="chown requires root")
def test_apply_sandbox_ownership_chowns_tree_and_tightens_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sub").mkdir()
    (ws / "sub" / "f.txt").write_text("x", encoding="utf-8")
    apply_sandbox_ownership(ws, POOL_START)
    assert ws.stat().st_uid == POOL_START
    assert stat.S_IMODE(ws.stat().st_mode) == 0o770
    assert ws.stat().st_gid == os.getegid()
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


def test_reconcile_spares_the_store_the_copy_and_the_infrastructure_namespace(
    tmp_path, monkeypatch
):
    """Only a directory the shared shape predicate calls a sandbox tree can be
    an orphan-uid reclaim target.

    ``snap_*`` and ``_*`` names pass ``validate_sandbox_id`` (``_`` is a legal
    id character), and the snapshot store's base is the platform's *shared
    export root* (``control_plane/app.py``) -- which this test hands in as the
    same root as the trees -- so the store lands in this scan next to the real
    trees. Reclaiming the uid a pool-owned *non-tree* happens to carry also
    **recursively chowns** that tree to the worker -- handing a foreign tree's
    ownership to the next allocation -- so both prefixed shapes stay out while
    the real ``sbx_*`` orphan is still reclaimed and handed back to the worker
    (uid accounting and chown targets both unchanged for it).
    """
    stranded = tmp_path / "sbx_stranded"
    stranded.mkdir()
    leftover = stranded / "leftover.txt"
    leftover.write_text("x", encoding="utf-8")
    store = _snapshot_store(tmp_path, "snap_0040ce7e44f6365f")
    # A whole-tree copy under a snapshot id: the copied top-level record names
    # the source sandbox, so this shape is the store *and* a foreign record.
    copy = tmp_path / "snap_copy0000000000000"
    copy.mkdir()
    (copy / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_src", "host_uid": POOL_START + 2}),
        encoding="utf-8",
    )
    volumes = tmp_path / "_volumes"
    volumes.mkdir()
    _pretend_owned(
        monkeypatch,
        {
            stranded: POOL_START,
            store: POOL_START + 1,
            copy: POOL_START + 2,
            volumes: POOL_START + 3,
        },
    )
    chowned: list[Path] = []
    monkeypatch.setattr(
        uid_pool, "_chown_tree", lambda path, uid, gid: chowned.append(path)
    )
    pool = _pool(tmp_path)
    result = pool.reconcile()
    # The copy's own top-level record is the only recorded uid; the store's
    # nested record (claiming POOL_START) is not read.
    assert result["referenced"] == [POOL_START + 2]
    assert result["reclaimed"] == [POOL_START]
    assert result["cleaned"] == [{"uid": POOL_START, "path": str(stranded)}]
    assert result["skipped"] == []
    assert chowned == [stranded]
    assert (store / "snapshot.json").read_text(encoding="utf-8") == "{}"
    assert (store / "fs" / "sandbox.json").is_file()
    assert (copy / "sandbox.json").is_file()
    # Neither the store's uid nor the namespace's uid is reclaimable here.
    assert POOL_START + 1 not in result["reclaimed"]
    assert POOL_START + 3 not in result["reclaimed"]
    # Unchanged ``sbx_*`` behavior: the reclaimed uid is allocatable again.
    assert pool.acquire("sbx_new") == POOL_START


def test_uid_accounting_reads_records_regardless_of_prefix(tmp_path):
    """The two record-driven scans only decide *which records get read*.

    A store carries no top-level record, so it contributes nothing: the
    record inside its ``fs/`` copy belongs to the copied sandbox and must not
    pin its uid. A prefixed directory that *does* carry one is, by shape, a
    sandbox tree (a client-chosen id, or a whole-tree copy), and its uid stays
    referenced -- the accounting side keeps its fail-safe "any record owns its
    uid" rule, unchanged by this filter, so a stray copy can still only
    over-protect a uid, never free one.
    """
    store = _snapshot_store(tmp_path, "snap_0040ce7e44f6365f")
    copy = tmp_path / "snap_copy0000000000000"
    copy.mkdir()
    (copy / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_src", "host_uid": POOL_START + 1}),
        encoding="utf-8",
    )
    pool = _pool(tmp_path)
    result = pool.reconcile()
    assert result["referenced"] == [POOL_START + 1]
    assert result["reclaimed"] == []
    # The nested record is really there -- it just is not the store's own.
    assert (store / "fs" / "sandbox.json").is_file()
    # POOL_START is what the record inside the store's ``fs/`` copy claims; it
    # pins nothing, so the lowest uid is handed out.
    assert pool.acquire("sbx_a") == POOL_START
    assert pool.acquire("sbx_b") == POOL_START + 2


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


# ------------------------------------------------------- capability gating

def test_has_effective_cap_reads_the_mask_bits(monkeypatch) -> None:
    """The probe is about the effective mask, not euid: the same root worker can
    be hardened (bit dropped) or not."""
    import envd_service.uid_pool as uid_pool

    monkeypatch.setattr(uid_pool, "_cap_eff", lambda: 0x00000000A80425FB)
    assert uid_pool.has_effective_cap(uid_pool.CAP_SYS_PTRACE) is False
    assert uid_pool.has_effective_cap(uid_pool.CAP_SYS_PTRACE - 1) is True  # SYS_PTRACE neighbour
    monkeypatch.setattr(uid_pool, "_cap_eff", lambda: 0x0000003FFFFFFF)
    assert uid_pool.has_effective_cap(uid_pool.CAP_SYS_PTRACE) is True
    monkeypatch.setattr(uid_pool, "_cap_eff", lambda: None)  # no /proc (macOS dev)
    assert uid_pool.has_effective_cap(uid_pool.CAP_SYS_PTRACE) is False


def test_root_worker_without_sys_ptrace_says_so(tmp_path, monkeypatch, caplog) -> None:
    """A root worker with a hardened cap set can allocate host uids but cannot
    map them in-process; that must be reported with the remedy, not discovered
    as a wall of `sandlock_create failed`."""
    import logging
    import os

    import envd_service.app as app_module
    import envd_service.uid_pool as uid_pool
    from envd_service.config import Settings as EnvdSettings
    from envd_service.runtime.registry import RuntimeRegistry

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(uid_pool, "_cap_eff", lambda: 0x00000000A80425FB)
    with caplog.at_level(logging.WARNING, logger="envd_service.app"):
        app = app_module.create_app(
            settings=EnvdSettings(executor="local", workspace_base=tmp_path),
            runtime_registry=RuntimeRegistry(tmp_path),
            workspace_base=tmp_path,
        )
    assert app is not None
    assert any(
        r.message == app_module.PER_UID_NO_PTRACE_WARNING for r in caplog.records
    ), [r.message for r in caplog.records]
    # The pool itself is still built: the disclosure is about what the
    # in-process mediator can no longer do, not about allocation.
    assert app.state.runtime_registry is not None


def test_commit_keeps_the_marker_when_the_record_is_not_durable(tmp_path):
    """Fail-safe, pinned: no record on disk ⇒ the marker stays.

    The marker is what keeps a uid out of every other worker's free set while
    the record that is supposed to pin it is missing; dropping it there would
    hand the same uid to the next create.
    """
    pool = _pool(tmp_path)
    pool.acquire("sbx_a")
    marker = pool._marker_path("sbx_a")
    assert marker.is_file()
    pool.commit("sbx_a")  # no record written yet
    assert marker.is_file()


def test_commit_drops_the_marker_the_acquire_path_wrote(tmp_path):
    """The pre-existing behaviour, unchanged by the marker-first check."""
    pool = _pool(tmp_path)
    uid = pool.acquire("sbx_a")
    _write_record(tmp_path, "sbx_a", uid)
    marker = pool._marker_path("sbx_a")
    assert marker.is_file()
    pool.commit("sbx_a")
    assert not marker.exists()


def test_commit_does_not_read_the_record_when_there_is_no_marker(
    tmp_path, monkeypatch
):
    """A control-plane-allocated uid has no marker, so commit is a no-op.

    ``claim`` (OBS-9's path: the control plane allocated the uid and the record
    is the fleet ledger's echo) writes no reservation marker, so there is
    nothing for ``commit`` to drop. Asking the disk anyway cost every create
    one NFS read plus one negative ``unlink`` -- measured 5-25 ms on the
    deployment's NAS (2026-10-01). Pinned by refusing the read outright: if
    the implementation goes back to reading first, this test fails.
    """
    pool = _pool(tmp_path)
    pool.claim("sbx_a", POOL_START + 1)

    def _forbidden(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError(
            "commit() read the sandbox record although the uid came from the "
            "control plane and no reservation marker exists"
        )

    monkeypatch.setattr(uid_pool, "_recorded_uid", _forbidden)
    pool.commit("sbx_a")
    assert not pool._marker_path("sbx_a").exists()
