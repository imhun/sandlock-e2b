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

import io
import os
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


def test_the_unpack_streams_instead_of_listing_every_member(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unpack must not build the whole member list (``getmembers``).

    A real snapshot is far larger than this fixture's, and the agent's
    ``maint`` container was OOM-killed once during Task 1's measurement
    (``docs/create-local-first-design.md`` §3.0): the reader walks the tar one
    member at a time instead of materialising an index of it.
    """
    entries = {f"f-{i:03d}.txt": f"filler {i}\n" for i in range(64)}
    archive_path = _tar(workspace / "fs.tar", entries)
    dest = workspace / "dest"
    dest.mkdir()

    def _boom(self):  # pragma: no cover - only reached by the defect
        raise AssertionError("the unpack listed every member up front")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _boom)

    assert archive.extract_sandbox_archive(archive_path, dest) == len(entries)
    assert (dest / "f-063.txt").read_text(encoding="utf-8") == "filler 63\n"
