"""Task 4: the node-local half of the platform state (``E2B_NODE_STATE_BASE``).

The create's ``prepare`` phase writes three small things that only this node
ever reads -- the ``.creating`` marker, the ``statfs(2)`` accounting seed and
the uid pool's lock and reservation markers. Until this task all three lived
under the **shared** state base, so each one paid a NAS metadata round trip
(~13 ms) inside the phase the create's latency floor is measured on.

The split has two halves that must not be confused, and each has a test here:

1. the three chips move to ``<node state base>/_runtime/<id>/`` (and the pool's
   own files to ``<node state base>/.uid_pool.lock`` / ``.uid_reservations/``);
2. the **record** (``_runtime/<id>/sandbox.json``) and the checkpoint store
   stay on the shared base, because the fleet-wide uid ledger enumerates them
   -- which is the third test, and the reason the ledger's index changes from
   *tree directory names* to *the shared record directory*.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from envd_service import agent as agent_module
from envd_service import uid_pool
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import paths

SANDBOX = "sbx_split"
KEY = "internal-key"
POOL_START = 10000
POOL_SIZE = 10


def _write_platform_record(state: Path, sandbox_id: str, uid: int) -> None:
    """One sandbox's record where the platform writes it (``_runtime/<id>/``).

    The pre-split in-tree location is deliberately *not* written: since OBS-9
    this is the location every worker reads, and ``RuntimeRegistry`` adopts the
    legacy copy away.
    """
    record = state / "_runtime" / sandbox_id
    record.mkdir(parents=True, exist_ok=True)
    (record / "sandbox.json").write_text(
        json.dumps({"sandbox_id": sandbox_id, "host_uid": uid}), encoding="utf-8"
    )


def test_the_uid_ledger_sees_every_nodes_records_from_the_shared_index(
    tmp_path: Path,
) -> None:
    """The fleet-wide "which uids are taken" index is the shared record dir.

    Review Focus 3: ``_recorded_uids`` used to infer the fleet's taken uids by
    listing **tree directory names** under the workspace base and reading each
    tree's record. Once the trees are node-local (Task 3) that sees one node's
    trees and nothing else, while the records -- the thing that actually pins a
    uid -- stay shared. The replacement index enumerates
    ``<state base>/_runtime/*/sandbox.json`` exactly once, on every node.

    The pin is falsifiable in both directions: the three records below are
    written for three *different* nodes (only ``sbx_here`` has a tree on this
    one), the tree-names index answers ``{10000}`` and the record index answers
    all three. A re-added tree walk (the "fallback" the ruling forbids) turns
    the last assertion red again, because the legacy in-tree record below is
    deliberately not part of the index.
    """
    trees = tmp_path / "node-a" / "workspaces"
    state = tmp_path / "export" / "state"
    # This node's own tree -- the only name the tree-directory index can see.
    (trees / "sbx_here").mkdir(parents=True)
    for sandbox_id, uid in (
        ("sbx_here", POOL_START),
        ("sbx_elsewhere", POOL_START + 1),
        ("sbx_third_node", POOL_START + 2),
    ):
        _write_platform_record(state, sandbox_id, uid)
    # A pre-split record that only lives inside a tree: the index does not walk
    # trees any more, and the registry is what adopts these (so it must not pin
    # a uid here).
    legacy = trees / "sbx_legacy"
    legacy.mkdir()
    (legacy / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_legacy", "host_uid": POOL_START + 3}),
        encoding="utf-8",
    )

    assert uid_pool._recorded_uids(
        trees, POOL_START, POOL_SIZE, state_base=state
    ) == {POOL_START, POOL_START + 1, POOL_START + 2}

    # ...and the allocation path consults that index, so the next free uid is
    # the first one no node's record references.
    pool = uid_pool.UidPool(
        trees, start=POOL_START, size=POOL_SIZE, state_base=state
    )
    assert pool.acquire("sbx_new") == POOL_START + 3


def test_the_uid_pools_own_files_live_on_the_node_local_base(tmp_path: Path) -> None:
    """The pool's lock and reservation markers move with the create's chips.

    ``acquire`` takes a ``flock`` and writes a marker inside the create's
    ``prepare`` phase, so on this deployment both were NAS metadata round trips
    (the lock's ``open``, the marker's write + rename, then ``commit``'s
    negative ``unlink``). Both are read by *this node's* processes only: the
    fleet-wide statement is the shared record the uid ends up in, which is what
    ``_recorded_uids`` indexes. Unnamed, the pool is byte-for-byte what it was.
    """
    trees = tmp_path / "workspaces"
    state = tmp_path / "export" / "state"
    node = tmp_path / "node" / "state"
    # The deployment's init container creates and owns the node-local base
    # (``workspace-root-init``); the pool deliberately does not.
    node.mkdir(parents=True)

    pool = uid_pool.UidPool(
        trees,
        start=POOL_START,
        size=POOL_SIZE,
        state_base=state,
        node_state_base=node,
    )
    assert pool.lock_path == node / ".uid_pool.lock"

    assert pool.acquire("sbx_a") == POOL_START

    assert (node / ".uid_reservations" / "sbx_a").read_text(
        encoding="utf-8"
    ) == f"{POOL_START}\n"
    assert (state / ".uid_reservations").exists() is False
    # ...and with no node state base named, the shared state base again.
    assert (
        uid_pool.UidPool(
            trees, start=POOL_START, size=POOL_SIZE, state_base=state
        ).lock_path
        == state / ".uid_pool.lock"
    )


def _worker(tmp_path: Path) -> tuple[object, EnvdSettings, RuntimeRegistry]:
    """A worker whose three bases are three different directories."""
    workspace_base = tmp_path / "workspaces"
    state_base = tmp_path / "export" / "state"
    node_state_base = tmp_path / "node" / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        node_state_base=node_state_base,
        shared_volume_root=None,
        internal_api_key=KEY,
    )
    registry = RuntimeRegistry(workspace_base, state_base=state_base)
    app = create_envd_app(
        settings=settings, runtime_registry=registry, workspace_base=workspace_base
    )
    return app, settings, registry


def _node_dir(settings: EnvdSettings, sandbox_id: str = SANDBOX) -> Path:
    return Path(settings.node_state_base) / "_runtime" / sandbox_id


def _shared_dir(settings: EnvdSettings, sandbox_id: str = SANDBOX) -> Path:
    return Path(settings.state_base) / "_runtime" / sandbox_id


async def _post(app, payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes", json=payload, headers={"X-Internal-Key": KEY}
        )


async def _delete(app, sandbox_id: str = SANDBOX) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.delete(
            f"/agent/sandboxes/{sandbox_id}", headers={"X-Internal-Key": KEY}
        )


async def _park(app, sandbox_id: str = SANDBOX) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            f"/agent/untrusted/{sandbox_id}/park", headers={"X-Internal-Key": KEY}
        )


@pytest.mark.asyncio
async def test_the_create_marker_lives_on_the_node_local_base(tmp_path: Path) -> None:
    """The create's marker and the ``statfs`` seed are written node-local.

    Both are read by *this* node only: the marker by a ``DELETE`` that races
    the create (``_await_inflight_create``) and the accounting seed by the
    sandbox's own slot process -- so both belong on the node's own disk, and
    what the create pays for them must not be a NAS round trip.

    The two halves of the assertion are deliberately in one test: the chips
    land on the node base **and** they are gone from the shared one. A change
    that merely adds a second copy (the silent shape: two readers disagreeing
    about which file is real) fails the second half.
    """
    app, settings, _registry = _worker(tmp_path)

    # The unset rule, first: no node state base named means today's layout,
    # byte for byte (compose, tests, ``local://``).
    assert paths.sandbox_creating_marker(
        settings.workspace_base, SANDBOX, state_base=settings.state_base
    ) == _shared_dir(settings) / paths.CREATING_MARKER_NAME
    assert paths.sandbox_disk_stats_path(
        settings.workspace_base, SANDBOX, state_base=settings.state_base
    ) == _shared_dir(settings) / "disk-stats"

    # ...and the base, once named, carries them.
    assert paths.sandbox_creating_marker(
        settings.workspace_base,
        SANDBOX,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    ) == _node_dir(settings) / paths.CREATING_MARKER_NAME
    assert paths.sandbox_disk_stats_path(
        settings.workspace_base,
        SANDBOX,
        state_base=settings.state_base,
        node_state_base=settings.node_state_base,
    ) == _node_dir(settings) / "disk-stats"

    # Behaviour, not path algebra: the real ``prepare`` phase.
    resp = await _post(
        app,
        {
            "sandboxID": SANDBOX,
            "phase": "prepare",
            "diskMB": 1024,
        },
    )

    assert resp.status_code == 200
    assert (_node_dir(settings) / paths.CREATING_MARKER_NAME).is_file()
    assert (_node_dir(settings) / "disk-stats").read_text(
        encoding="utf-8"
    ) == f"{1024 * 1024 * 1024} 0\n"
    assert (_shared_dir(settings) / paths.CREATING_MARKER_NAME).exists() is False
    assert (_shared_dir(settings) / "disk-stats").exists() is False

    # ...and the prepared half is undone where it was written: ``cancel`` takes
    # both chips and the directory that held them, so a create that never
    # finishes leaves nothing on the node's disk either.
    assert (
        await _post(app, {"sandboxID": SANDBOX, "phase": "cancel"})
    ).status_code == 204
    assert _node_dir(settings).exists() is False


async def _await_record(settings: EnvdSettings, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if (_shared_dir(settings) / "sandbox.json").is_file():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_the_record_stays_on_the_shared_base(tmp_path: Path) -> None:
    """The record (and everything the fleet reads with it) stays shared.

    Ruling 3 of the plan: ``_runtime/<id>/sandbox.json`` is the fleet's uid
    ledger -- every node's pool enumerates it -- and the checkpoint store
    beside it is read by whichever node resumes a paused sandbox. Both keep
    living on ``E2B_STATE_BASE``; only the node-local chips move.

    Read the *whole* directory: what this pins is not "the record is there"
    but "the split is complete in one direction". A create whose accounting
    seed is left behind on the shared base is exactly the shape that keeps
    paying the NAS round trip while every unit test still passes.
    """
    app, settings, registry = _worker(tmp_path)

    resp = await _post(app, {"sandboxID": SANDBOX})

    assert resp.status_code == 201
    assert await _await_record(settings) is True
    # The shared half: the record, where every node's ledger reads it.
    assert (_shared_dir(settings) / "sandbox.json").is_file()
    assert _shared_dir(settings) / "disk-stats" not in set(
        _shared_dir(settings).iterdir()
    )
    # The node-local half: the record is not copied here, and the marker the
    # create wrote came off on this side only.
    assert (_node_dir(settings) / "sandbox.json").exists() is False
    assert (_node_dir(settings) / paths.CREATING_MARKER_NAME).exists() is False
    assert registry.get(SANDBOX) is not None


@pytest.mark.asyncio
async def test_the_teardown_takes_the_node_local_chips_with_it(tmp_path: Path) -> None:
    """Nothing else collects the node-local directory, so the teardown must.

    The shared half of ``_runtime/<id>`` has always gone with the tree -- the
    repo's "paired 收尾" rule (N12/N24). The node-local half is the same
    directory one base over, and it is in **no** scan: the orphan GC and the
    quota scans walk the tree root, and the platform-disk account measures the
    shared base. One directory per deleted sandbox would therefore accumulate
    on the node's disk for ever, i.e. exactly what the plan's "本节点 state
    ≪ 1 G" account assumes cannot happen.
    """
    app, settings, _registry = _worker(tmp_path)
    assert (await _post(app, {"sandboxID": SANDBOX})).status_code == 201
    assert await _await_record(settings) is True
    assert (_node_dir(settings) / "disk-stats").is_file()

    assert (await _delete(app)).status_code == 204

    assert _node_dir(settings).exists() is False
    assert _shared_dir(settings).exists() is False


@pytest.mark.asyncio
async def test_the_park_path_drops_the_node_local_chips_too(tmp_path: Path) -> None:
    """Park is the third exit, and it is the one that cannot be an ``rmdir``.

    A parked tree is one the worker **refuses** to act on -- reached exactly
    because its record contradicts the tree it was found next to, or because
    there is no record at all (the eviction / TTL / unreachable-worker case
    this route exists for). Either way it is a *completed* create: the shared
    half is moved into ``_untrusted.trees/<id>`` entry by entry, and the
    node-local half holds the ``statfs`` seed the create left behind (the
    marker came off when the record went durable), so the directory is **not
    empty**. An ``rmdir`` there raises ``ENOTEMPTY``; swallowing it -- which is
    what this path did until fix round 1 -- leaves one seed file per parked
    sandbox on the node's disk for ever, in a directory no scan covers.

    Fix round 1 (review Important 1): the cleanup is an ``rmtree`` now, and
    this test is the pin that was missing -- it builds precisely that shape
    (``disk-stats`` alone in the node-local directory) so the regression cannot
    pass as a "best effort".
    """
    app, settings, _registry = _worker(tmp_path)
    tree = Path(settings.workspace_base) / SANDBOX
    (tree / "workspace").mkdir(parents=True)
    node_dir = _node_dir(settings)
    node_dir.mkdir(parents=True)
    (node_dir / "disk-stats").write_text("1024 0\n", encoding="utf-8")
    # No record anywhere: the control plane released it (eviction / TTL / a
    # worker that was unreachable), which is what makes this tree one the
    # worker refuses -- and what an operator reaches for this route with.
    assert _shared_dir(settings).exists() is False

    assert (await _park(app)).status_code == 200

    assert _node_dir(settings).exists() is False
    assert _shared_dir(settings).exists() is False
    # The tree is *kept* -- park moves it, never deletes it.
    assert (Path(settings.workspace_base) / "_untrusted.trees" / SANDBOX).is_dir()


@pytest.mark.asyncio
async def test_a_park_never_eats_the_evidence_in_the_one_base_shape(
    tmp_path: Path, monkeypatch
) -> None:
    """No node-local base named ⇒ the two directories are the same one.

    park keeps a refused tree's platform files as **evidence**: it moves them
    into ``_untrusted.trees/<id>/`` entry by entry, and when a move fails the
    leftover is exactly what an operator needs -- the record. (The function's
    own comment names the EXDEV case: a deployment that gives the state base a
    mount of its own.) The node-local cleanup Task 4 added must therefore only
    ever run on a directory of *its own*: with the base unnamed it is the same
    path, and an unconditional ``rmtree`` there destroys the evidence the loop
    just declined to move. Same "unset = today's shape" rule as every other
    helper in ``gateway_common.paths``.

    The move is made to fail the way the deployment would: ``os.replace`` of a
    state-base entry raises EXDEV.
    """
    workspace_base = tmp_path / "workspaces"
    state_base = tmp_path / "export" / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        node_state_base=None,
        shared_volume_root=None,
        internal_api_key=KEY,
    )
    registry = RuntimeRegistry(workspace_base, state_base=state_base)
    app = create_envd_app(
        settings=settings, runtime_registry=registry, workspace_base=workspace_base
    )
    (workspace_base / SANDBOX / "workspace").mkdir(parents=True)
    # ...and a record that contradicts it by pointing at another tree that
    # really exists -- the W7 shape, and what makes this tree a refused one.
    (workspace_base / "sbx_other" / "workspace").mkdir(parents=True)
    record_dir = state_base / "_runtime" / SANDBOX
    record_dir.mkdir(parents=True)
    record = record_dir / "sandbox.json"
    record_body = json.dumps(
        {
            "sandbox_id": SANDBOX,
            "access_token": "tok",
            "workspace_dir": str(workspace_base / "sbx_other"),
        }
    )
    record.write_text(record_body, encoding="utf-8")

    real_replace = agent_module.os.replace

    def exdev_for_the_state_base(source, destination, *args, **kwargs):
        if str(source).startswith(str(state_base)):
            raise OSError(18, "Cross-device link", str(source))
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(agent_module.os, "replace", exdev_for_the_state_base)

    assert (await _park(app)).status_code == 200

    # The tree moved, the record did not -- and it is still there to be read.
    assert (
        workspace_base / "_untrusted.trees" / SANDBOX
    ).is_dir()
    assert record.read_text(encoding="utf-8") == record_body


@pytest.mark.asyncio
async def test_a_failed_node_local_cleanup_is_named_not_swallowed(
    tmp_path: Path, caplog
) -> None:
    """The cleanup must not be able to go quiet again (review nit, round 1).

    The bug this round fixed *was* a silent no-op, so the replacement may not
    hide its own failure behind ``ignore_errors=True``: every other best-effort
    step in the park path logs a warning, and so does this one. The failure is
    injected the way a real one happens -- the node-local directory's parent is
    read-only, so the final ``rmdir`` is ``EACCES`` -- rather than by mocking
    ``shutil``.
    """
    app, settings, _registry = _worker(tmp_path)
    (Path(settings.workspace_base) / SANDBOX / "workspace").mkdir(parents=True)
    node_dir = _node_dir(settings)
    node_dir.mkdir(parents=True)
    (node_dir / "disk-stats").write_text("1024 0\n", encoding="utf-8")
    parent = node_dir.parent
    parent.chmod(0o500)
    try:
        assert (await _park(app)).status_code == 200
    finally:
        parent.chmod(0o755)

    messages = [record.getMessage() for record in caplog.records]
    assert [
        message
        for message in messages
        if "could not remove its node-local" in message
    ] != []
