"""Task 2: a snapshot payload is **one tar**, and both payload shapes restore.

The writer side (``envd_service.agent``) used to ``copytree`` the sandbox tree
into ``<snapshot dir>/fs/``. On the shared NAS that is one metadata round trip
per entry -- measured 2026-10-02 at 26-29 ms per entry, 203 entries = 5.7 s for
a create-from-snapshot and 8.1 s for the capture itself
(``docs/create-local-first-design.md`` §1.4 + §7.6). One ``fs.tar`` turns the
payload into a single sequential file, so the *reader* is the shape that has to
change with it.

Two properties this file exists for, and neither is optional:

* **the writer** leaves exactly ``fs.tar`` + ``.complete`` in the store, written
  aside -> ``fsync`` -> ``rename``, with ``.complete`` last (a half-written
  payload must be a temp file, never a complete-looking snapshot);
* **the reader** is the *one* extractor both images share. The member filter and
  the destination-containment check used to live twice (``control_plane``'s and
  ``envd_service``'s own ``_extract_sandbox_archive``); two copies of a
  path-escape guard is exactly the drift this pins shut -- every caller imports
  the same function object, and neither service keeps a private one.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent_module
from control_plane.api import sandboxes as cp_sandboxes
from envd_service import agent as envd_agent
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import archive

SANDBOX = "sbx_snap01"
SNAPSHOT = "snap_0123456789abcdef"
KEY = "internal-key-0123456789"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFY_PROBE = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "local_first_snapshot_verify.py"
CAPACITY_PROBE = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "local_first_capacity_account.py"


def _load_probe(name: str, path: Path):
    """Import one acceptance probe by path (the repo's own pattern)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _worker(workspace: Path):
    runtime_registry = RuntimeRegistry(workspace)
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace,
        internal_api_key=KEY,
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return app, settings


async def _capture(worker, *, sandbox_id: str = SANDBOX, snapshot_id: str = SNAPSHOT):
    app, _settings = worker
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/snapshots",
            json={"snapshotID": snapshot_id, "sandboxID": sandbox_id},
            headers={"X-Internal-Key": KEY},
        )


def _tree(settings: EnvdSettings, sandbox_id: str = SANDBOX) -> Path:
    root = settings.workspace_base / sandbox_id
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")
    return root


def _tar(path: Path, entries: dict[str, bytes | str]) -> Path:
    """Build one payload tar the way the writer does: entries at the tar root."""
    stage = path.parent / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    for name, value in entries.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
    with tarfile.open(path, "w") as tar:
        for child in sorted(stage.iterdir(), key=lambda item: item.name):
            tar.add(child, arcname=child.name, recursive=True)
    return path


# ------------------------------------------------------------------ the writer


@pytest.mark.asyncio
async def test_a_snapshot_is_one_tar(workspace: Path) -> None:
    """The store holds ``fs.tar`` + ``.complete``, and no exploded ``fs/``."""
    worker = _worker(workspace)
    _app, settings = worker
    root = _tree(settings)
    (root / "link").symlink_to("workspace/kept.txt")

    resp = await _capture(worker)

    assert resp.status_code == 201
    store = settings.workspace_base / "_snapshots" / SNAPSHOT
    assert sorted(path.name for path in store.iterdir()) == [".complete", "fs.tar"]
    assert (store / "fs").exists() is False

    # The payload is the tree **root** (the same fact the v1 landmine turned
    # into ``workspace/workspace/kept.txt``): the tar's members are the tree's
    # own entries, one for one, symlink included and not dereferenced.
    with tarfile.open(store / "fs.tar") as tar:
        members = sorted((member.name, member.issym()) for member in tar)
    assert members == [("link", True), ("workspace", False), ("workspace/kept.txt", False)]


@pytest.mark.asyncio
async def test_a_half_written_tar_is_not_a_snapshot(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture that fails mid-write leaves nothing behind, not half a payload.

    The bytes are staged under a temp name and renamed, so the store never
    holds a partial ``fs.tar``; the whole snapshot directory goes away with the
    failed attempt (the control plane writes no record for it).
    """
    worker = _worker(workspace)
    _app, settings = worker
    _tree(settings)

    def _boom(src: Path, dst: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(agent_module, "_write_snapshot_tar", _boom)

    resp = await _capture(worker)

    assert resp.status_code == 500
    store = settings.workspace_base / "_snapshots" / SNAPSHOT
    assert sorted(path.name for path in settings.workspace_base.glob("_snapshots/*/*")) == []
    assert store.exists() is False


# ------------------------------------------------- the one shared extractor


def test_there_is_exactly_one_extractor_the_callers_share() -> None:
    """Ruling: the member filter and the containment check live in one place.

    The agent is a different image from the control plane, so "import it from
    ``control_plane``" was never available; the shared module is the fix, and
    this pins that no caller grew a private copy back.
    """
    from c3_agent import materialize

    assert cp_sandboxes.extract_sandbox_archive is archive.extract_sandbox_archive
    assert envd_agent.extract_sandbox_archive is archive.extract_sandbox_archive
    assert materialize.extract_sandbox_archive is archive.extract_sandbox_archive
    assert not hasattr(cp_sandboxes, "_extract_sandbox_archive")
    assert not hasattr(envd_agent, "_extract_sandbox_archive")


def test_the_control_planes_gzipped_migration_archive_still_unpacks(
    workspace: Path,
) -> None:
    """The other caller's shape: a ``tar.gz`` with a ``.`` root member.

    ``_export_sandbox_archive`` writes ``tar.add(workspace, arcname=".")`` and
    compresses it; sharing one extractor must not cost that path anything, so
    the compression autodetection and the ``.`` member are pinned here.
    """
    stage = workspace / "stage"
    (stage / "workspace").mkdir(parents=True)
    (stage / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")
    archive_path = workspace / "sbx.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(stage, arcname=".")
    dest = workspace / "dest"
    dest.mkdir()

    written = archive.extract_sandbox_archive(archive_path, dest)

    assert written == 3
    assert (dest / "workspace" / "kept.txt").read_text(encoding="utf-8") == "kept\n"


def test_a_truncated_tar_is_refused(workspace: Path) -> None:
    """A payload that stops mid-member is a corrupt payload, refused by name."""
    archive_path = _tar(workspace / "fs.tar", {"big.bin": b"x" * (256 * 1024)})
    payload = archive_path.read_bytes()
    archive_path.write_bytes(payload[: len(payload) // 2])
    dest = workspace / "dest"
    dest.mkdir()

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_sandbox_archive(archive_path, dest)

    assert caught.value.reason == archive.ARCHIVE_IS_CORRUPT


def test_an_absolute_link_member_is_dropped(workspace: Path) -> None:
    """The migration filter's rule, kept: absolute links are host paths.

    Volume mounts are archived as symlinks to paths that only exist on the
    source node, so the member is skipped rather than recreated (the control
    plane's ``_extract_sandbox_archive`` has done this since N57; sharing the
    implementation is what keeps the snapshot reader on the same rule).
    """
    archive_path = _tar(workspace / "fs.tar", {"kept.txt": "kept\n"})
    with tarfile.open(archive_path, "a") as tar:
        info = tarfile.TarInfo("mount")
        info.type = tarfile.SYMTYPE
        info.linkname = "/var/lib/volumes/data/sbx"
        tar.addfile(info)
    dest = workspace / "dest"
    dest.mkdir()

    archive.extract_sandbox_archive(archive_path, dest)

    assert (dest / "kept.txt").read_text(encoding="utf-8") == "kept\n"
    assert os.path.lexists(dest / "mount") is False


def test_a_member_that_escapes_the_destination_is_refused(workspace: Path) -> None:
    """``..`` in a member name is a refusal, not a write next to the tree."""
    archive_path = _tar(workspace / "fs.tar", {"kept.txt": "kept\n"})
    with tarfile.open(archive_path, "a") as tar:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"nope"))
    dest = workspace / "dest"
    dest.mkdir()

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_sandbox_archive(archive_path, dest)

    assert caught.value.reason == archive.MEMBER_ESCAPES
    assert (workspace / "outside.txt").exists() is False


def test_the_unpack_never_builds_its_own_member_list(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """We walk the members ourselves; we never pre-list them.

    **What this can falsify**: our own second list. ``getmembers()`` (an index
    in memory) plus ``extractall(members=...)`` (a second pass over it) is
    exactly the shape the implementation this task replaced had, and both calls
    raise here -- an author who restores that shape gets a red test.

    **What it cannot falsify**, and ``gateway_common/archive.py`` now says so:
    CPython's ``TarFile.next()`` appends every ``TarInfo`` to ``TarFile.members``
    no matter who iterates, so the *index* is stdlib's and survives any caller
    shape (review round 1, 2026-10-02: 20 万成员 ⇒ ``len(tar.members)`` 20 万、
    峰值 88.8 MB ≈ 444 B/成员). What this task really changed is the member
    **data**: it goes through tarfile's own 64 KiB buffer one member at a time
    instead of the payload being walked/copied whole, which is the driver of
    the OOM recorded in ``docs/create-local-first-design.md`` §3.0.
    """
    entries = {f"f-{i:03d}.txt": f"filler {i}\n" for i in range(64)}
    archive_path = _tar(workspace / "fs.tar", entries)
    dest = workspace / "dest"
    dest.mkdir()

    def _boom(*_args, **_kwargs):  # pragma: no cover - only reached by the defect
        raise AssertionError("the unpack pre-listed its members")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)
    monkeypatch.setattr(tarfile.TarFile, "extractall", _boom)

    assert archive.extract_sandbox_archive(archive_path, dest) == len(entries)
    assert (dest / "f-063.txt").read_text(encoding="utf-8") == "filler 63\n"


# ------------------------------------ the acceptance probes that read the store
#
# Task 2 changed the payload on disk from an exploded ``fs/`` directory to one
# ``fs.tar``, and two read-only acceptance probes classify payload presence by
# looking at the store directly. ``local_first_snapshot_verify.py`` is the one
# ``docs/create-local-first-design.md`` §7.5 tells an operator to run after a
# deploy; a probe that still named ``fs`` would file every *new* snapshot under
# "record only" -- the §4.2 *data-defect* class -- and report
# ``payload_bytes: null`` for a perfectly healthy store.


def _store_entry(workspace: Path, name: str = "snap_tar") -> Path:
    entry = workspace / "_snapshots" / name
    entry.mkdir(parents=True)
    return entry


def _tar_member(entry: Path, *, name: str = "workspace/kept.txt") -> None:
    with tarfile.open(entry / "fs.tar", "w") as tar:
        info = tarfile.TarInfo(name)
        payload = b"kept\n"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))


def test_the_store_verification_probe_reads_a_tar_payload(workspace: Path) -> None:
    """A ``fs.tar`` snapshot is ``record+payload``, with its bytes counted."""
    probe = _load_probe("local_first_snapshot_verify", VERIFY_PROBE)
    entry = _store_entry(workspace)
    (entry / "snapshot.json").write_text(
        json.dumps({"status": "completed", "created_at": "2026-10-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (entry / ".complete").write_text("complete\n", encoding="utf-8")
    _tar_member(entry)

    row = probe.classify(entry)

    assert row["record"] is True
    assert row["payload"] is True
    assert row["payload_shape"] == "tar"
    assert row["payload_entries"] == 1
    assert row["payload_bytes"] == (entry / "fs.tar").stat().st_size
    assert row["status"] == "completed"


def test_the_store_verification_probe_still_reads_the_legacy_directory(
    workspace: Path,
) -> None:
    """The pre-tar shape keeps its own reading (and its own name)."""
    probe = _load_probe("local_first_snapshot_verify", VERIFY_PROBE)
    entry = _store_entry(workspace, "snap_dir")
    (entry / "fs" / "workspace").mkdir(parents=True)
    (entry / "fs" / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")

    row = probe.classify(entry)

    assert row["payload"] is True
    assert row["payload_shape"] == "dir"
    assert row["payload_entries"] == 2
    assert row["payload_bytes"] == 5


def test_the_capacity_account_reads_a_tar_payload(workspace: Path) -> None:
    """The capacity probe's per-id rows see the tar too."""
    probe = _load_probe("local_first_capacity_account", CAPACITY_PROBE)
    entry = _store_entry(workspace)
    _tar_member(entry)

    rows = probe.snapshot_records(workspace)

    assert rows == [
        {
            "id": "snap_tar",
            "record": False,
            "payload": True,
            "payload_shape": "tar",
            "created_at": None,
            "status": None,
            "payload_bytes": None,
            "payload_mtime": int(entry.stat().st_mtime),
        }
    ]


def test_the_payload_shape_helper_prefers_the_tar(workspace: Path) -> None:
    """One place decides "which shape is this snapshot": tar first, then ``fs/``.

    Both existing is not a shape the writer produces; the tar is the one it
    *does* produce, and the control plane derives ``copy_from`` at it, so the
    tar is the payload when the two are seen together.
    """
    from gateway_common import paths

    entry = _store_entry(workspace, "snap_both")
    (entry / "fs").mkdir()
    _tar_member(entry)

    assert paths.snapshot_payload(entry) == ("tar", entry / "fs.tar")
    assert paths.snapshot_payload(_store_entry(workspace, "snap_none")) is None


# --------------------------------------------- the containment guard's cost
#
# A guard that re-resolves every member is a metadata round trip per entry on
# the shared NAS (a snapshot's payload is a tree of thousands of files), and the
# restore leg is where a snapshot's cost lands. The measurement that caught it
# (2026-10-02, one sandbox, 203 members, same NAS as the trees):
#
#   copytree (the pre-tar reader)                12.8 s
#   stdlib extractall(filter="data")             11.0 s
#   this module, resolving every member          13.0 s   <-- the regression
#
# The cached guard below is the fix, and the two cases after it pin the parts
# the cache may **not** stop catching.


def _many_members_tar(path: Path, *, directories: int = 1, files: int = 200) -> None:
    with tarfile.open(path, "w") as tar:
        for index in range(directories):
            info = tarfile.TarInfo(f"dir-{index}/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for index in range(files):
            info = tarfile.TarInfo(f"dir-0/f-{index:03d}.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"data"))


def test_the_parent_chain_is_checked_once_per_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This module's guard costs one ``lstat`` per **directory**, not per member.

    Counting every ``lstat`` in a real extraction cannot isolate this module:
    ``tarfile``'s own ``data`` filter resolves each member against the
    destination, so *its* cost is per-member no matter what we do (measured
    2026-10-02 inside one sandbox: ``extractall(filter="data")`` took 11.0 s for
    203 members). What our guard must not do is add a second per-member walk on
    top -- which is exactly the ~2 ms/entry regression it shipped with (13.0 s
    measured) and this test pins shut at the helper level: 200 members under one
    directory pay **one** ``lstat``, the second directory pays one more, and a
    third member under the first directory pays none.
    """
    dest = workspace / "dest"
    (dest / "workspace").mkdir(parents=True)
    (dest / "nested").mkdir()
    counter = {"n": 0}
    real_lstat = os.lstat

    def _counting_lstat(path, *args, **kwargs):
        counter["n"] += 1
        return real_lstat(path, *args, **kwargs)

    safe: set[str] = set()
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", _counting_lstat)
        for index in range(200):
            archive._guard_member(dest, f"workspace/f-{index:03d}.txt", safe)
        assert counter["n"] == 1
        archive._guard_member(dest, "nested/deep.txt", safe)
        assert counter["n"] == 2
        archive._guard_member(dest, "workspace/one-more.txt", safe)
        assert counter["n"] == 2

    # One entry per directory actually walked: a top-level member (no parent
    # components) would add "." without any ``lstat`` at all.
    assert safe == {"workspace", "nested"}


def test_a_link_where_the_snapshot_wants_a_directory_is_refused_named(
    workspace: Path,
) -> None:
    """A directory member's own path is checked, not just its parents."""
    archive_path = workspace / "fs.tar"
    with tarfile.open(archive_path, "w") as tar:
        info = tarfile.TarInfo("workspace/")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
    dest = workspace / "dest"
    outside = workspace / "outside"
    outside.mkdir()
    dest.mkdir()
    (dest / "workspace").symlink_to(outside)

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_sandbox_archive(archive_path, dest)

    assert caught.value.reason == archive.DESTINATION_IS_A_SYMLINK
    assert sorted(os.listdir(outside)) == []


def test_a_link_the_archive_itself_creates_is_still_caught(
    workspace: Path,
) -> None:
    """The cache may not become the only check: the stdlib filter stays the backstop.

    ``sub/`` is cached as a safe parent from the first member, and the archive
    then carries a link member that walks out of the destination. Nothing in
    this module re-checks that member's target (a link member is not a
    directory member, and its parent chain is the cached one), so the refusal
    has to come from ``tarfile``'s own ``data`` filter, translated by name --
    the "two layers, neither replaces the other" rule (C3 §14.4). The link is
    relative on purpose: an **absolute** link member is dropped by this
    module's volume-mount rule before any extractor sees it.
    """
    archive_path = workspace / "fs.tar"
    outside = workspace / "outside"
    outside.mkdir()
    with tarfile.open(archive_path, "w") as tar:
        info = tarfile.TarInfo("sub/")
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        first = tarfile.TarInfo("sub/first.txt")
        first.size = 4
        tar.addfile(first, io.BytesIO(b"data"))
        link = tarfile.TarInfo("sub")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        tar.addfile(link)
    dest = workspace / "dest"
    dest.mkdir()

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_sandbox_archive(archive_path, dest)

    assert caught.value.reason == archive.DESTINATION_IS_A_SYMLINK
    assert sorted(os.listdir(outside)) == []
