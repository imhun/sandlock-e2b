"""Task 15: a refused unpack leaves no half tree, and the unpack has a budget.

Two things are pinned here, and they are one design (N68 + N67):

* **N68** -- ``gateway_common.archive.extract_sandbox_archive`` writes into the
  directory it is handed, so a refusal halfway through a *live* tree leaves the
  members it already wrote there. Both worker call sites (``envd_service``'s
  snapshot restore and ``c3_agent``'s materialization) unpacked straight into
  the tree root, so a payload refused at the member cap left up to 1.5 M files
  in a sandbox's own tree -- with no record for the TTL sweeper to see.
  ``extract_into_place`` unpacks into ``<tree>.importing`` (same parent, so the
  publish is a rename within one filesystem) and publishes only a complete
  tree; a refusal removes the staging tree and re-raises, unchanged.

* **N67** -- the member cap bounds the unpack's *index*, not its *time* (1.5 M
  members measured ~12 min of CPU). ``E2B_ARCHIVE_MAX_SECONDS`` bounds the walk
  itself, sampled rather than per member.

The tests below are the nails for both, plus the ``_materialize_status`` mapping
Task 11 left behind (the member cap and the new time budget are "the payload is
what is wrong", so the agent's own surface answers 502, not 400).
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from c3_agent import materialize
from c3_agent.app import _materialize_status
from c3_agent.app import create_app as create_agent_app
from c3_agent.config import Settings as AgentSettings
from envd_service import agent as envd_agent
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import archive

SANDBOX = "sbx_staging01"
SNAPSHOT = "snap_0123456789abcdef"
KEY = "internal-key-0123456789"


class _RecordingRunner:
    """``materialize``'s privilege seam: records instead of exec'ing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, env) -> str:
        self.calls.append(list(argv))
        return ""


def _tar(path: Path, entries: dict[str, str]) -> Path:
    """One payload tar built from a real tree, so the source can be compared.

    A ``str`` value is a file, ``"-> <target>"`` is a symlink, and the members
    are the tree root's own entries (``arcname=child.name``) -- the shape
    ``_write_snapshot_tar`` emits and the shape both call sites read.
    """
    stage = path.parent / f"{path.name}.stage"
    for name, value in entries.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if value.startswith("-> "):
            target.symlink_to(value[3:])
        else:
            target.write_text(value, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for child in sorted(stage.iterdir(), key=lambda item: item.name):
            tar.add(child, arcname=child.name, recursive=True)
    return path


def _entries(root: Path) -> list[tuple[str, str]]:
    """Every entry under ``root`` as ``(relative path, kind)``, sorted."""
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            path = base / name
            if path.is_symlink():
                kind = "link"
            elif path.is_dir():
                kind = "dir"
            else:
                kind = "file"
            found.append((str(path.relative_to(root)), kind))
    return sorted(found)


def _fixed_clock(*ticks: float):
    """A monotonic stand-in: the given ticks, then the last one forever."""
    remaining = list(ticks)

    def clock() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return clock


def _many_members_tar(path: Path, count: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for index in range(count):
            info = tarfile.TarInfo(f"f-{index:05d}.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return path


# ------------------------------------------------------ the publish is a rename
#
# The staging directory sits beside the tree (same parent, same filesystem), so
# publishing is ``rename(2)`` and never ``EXDEV``. The one shape rename cannot
# take is a target that is itself a mount point (``rename(2)`` onto it is
# ``EBUSY``); none of this repo's shapes puts one there -- ``workspace-root``
# is mounted at ``E2B_WORKSPACE_BASE`` and a sandbox tree is a subdirectory of
# it, while volume slices are bound *inside* the tree (``build_volume_mounts``
# mounts each at ``<workspace>/<mount path>``). This probe proves the
# same-parent rename itself, on whatever filesystem the project's own ``tmp/``
# is.


def test_a_sibling_rename_publishes_the_staged_tree(workspace: Path) -> None:
    """Same parent, same device: ``rename(staging, target)`` lands the tree."""
    target = workspace / SANDBOX
    staging = workspace / f"{SANDBOX}.importing"
    (staging / "workspace").mkdir(parents=True)
    (staging / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")

    assert os.stat(workspace).st_dev == os.stat(staging).st_dev

    os.rename(staging, target)

    assert staging.exists() is False
    assert (target / "workspace" / "kept.txt").read_text(encoding="utf-8") == "kept\n"


# --------------------------------------------------------- N68: no half tree


def test_a_refused_unpack_leaves_no_tree_and_no_staging(workspace: Path) -> None:
    """The nail: the target root does not exist after a refusal.

    Today the members written before the cap are already in the tree the
    caller named; with staging they are in ``<tree>.importing``, which the
    refusal removes.
    """
    archive_path = _tar(
        workspace / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    target = workspace / SANDBOX

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_into_place(archive_path, target, max_members=2)

    assert caught.value.reason == archive.TOO_MANY_MEMBERS
    assert target.exists() is False
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_a_refused_unpack_keeps_an_existing_tree_as_it_was(workspace: Path) -> None:
    """The merge branch: an existing tree is untouched, not half-overwritten."""
    archive_path = _tar(
        workspace / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    target = workspace / SANDBOX
    (target / "workspace").mkdir(parents=True)
    (target / "keep.txt").write_text("kept\n", encoding="utf-8")

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_into_place(archive_path, target, max_members=2)

    assert caught.value.reason == archive.TOO_MANY_MEMBERS
    assert _entries(target) == [("keep.txt", "file"), ("workspace", "dir")]
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_a_successful_unpack_lands_every_entry_and_removes_the_staging(
    workspace: Path,
) -> None:
    """The success path is entry-for-entry the source, links included."""
    archive_path = _tar(
        workspace / "fs.tar",
        {
            "workspace/kept.txt": "kept\n",
            "nested/deep.txt": "deep\n",
            "real.txt": "hello\n",
            "link": "-> workspace/kept.txt",
        },
    )
    source = archive_path.parent / "fs.tar.stage"
    target = workspace / SANDBOX

    written = archive.extract_into_place(archive_path, target)

    assert written == 6
    assert _entries(target) == _entries(source)
    assert os.readlink(target / "link") == "workspace/kept.txt"
    assert (target / "workspace" / "kept.txt").read_text(encoding="utf-8") == "kept\n"
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_a_leftover_staging_tree_is_replaced_not_merged(workspace: Path) -> None:
    """A residue from a run that died mid-unpack is removed before this one."""
    archive_path = _tar(workspace / "fs.tar", {"workspace/kept.txt": "kept\n"})
    target = workspace / SANDBOX
    leftover = workspace / f"{SANDBOX}.importing"
    (leftover / "workspace").mkdir(parents=True)
    (leftover / "workspace" / "stale.txt").write_text("stale\n", encoding="utf-8")

    archive.extract_into_place(archive_path, target)

    assert _entries(target) == [("workspace", "dir"), ("workspace/kept.txt", "file")]
    assert leftover.exists() is False


def test_a_staging_cleanup_that_fails_is_a_named_warning(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Best effort, loudly: the refusal still propagates, the marker is logged."""
    archive_path = _tar(
        workspace / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    staging = workspace / f"{SANDBOX}.importing"

    def _boom(path, *args, **kwargs):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(archive.shutil, "rmtree", _boom)
    with caplog.at_level(logging.WARNING, logger="gateway_common.archive"):
        with pytest.raises(archive.ArchiveRefusal) as caught:
            archive.extract_into_place(
                archive_path, workspace / SANDBOX, max_members=2
            )

    assert caught.value.reason == archive.TOO_MANY_MEMBERS
    assert [record.message for record in caplog.records] == [
        f"archive-staging-leftover: cannot remove the staging tree {staging}: "
        "[Errno 39] Directory not empty"
    ]


def test_the_merge_keeps_what_the_existing_tree_already_had(workspace: Path) -> None:
    """``dirs_exist_ok`` semantics: a migrated tree keeps its files."""
    archive_path = _tar(
        workspace / "fs.tar", {"same.txt": "fresh\n", "nested/deep.txt": "deep\n"}
    )
    target = workspace / SANDBOX
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("from before\n", encoding="utf-8")
    (target / "same.txt").write_text("stale\n", encoding="utf-8")

    archive.extract_into_place(archive_path, target)

    assert (target / "keep.txt").read_text(encoding="utf-8") == "from before\n"
    assert (target / "same.txt").read_text(encoding="utf-8") == "fresh\n"
    assert (target / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep\n"


def test_the_destination_guard_still_runs_against_the_live_tree(
    workspace: Path,
) -> None:
    """A link in the *tree* is refused by name, staging or not (§4.3.1)."""
    archive_path = _tar(workspace / "fs.tar", {"sub/file.txt": "payload\n"})
    target = workspace / SANDBOX
    outside = workspace / "outside"
    outside.mkdir()
    target.mkdir()
    (target / "sub").symlink_to(outside)

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_into_place(archive_path, target)

    assert caught.value.reason == archive.DESTINATION_IS_A_SYMLINK
    assert sorted(os.listdir(outside)) == []
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_a_merge_that_fails_partway_names_where_it_stopped(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N76: the merge into a live tree is not atomic, so a failure says where.

    The per-entry ``os.replace`` cannot be made one step without
    ``RENAME_EXCHANGE`` (Linux-only, unverified over the deployment's NFS), so
    the accepted trade is a *named* stop: the refusal carries what was already
    published into the live tree. That is the operator's next question when a
    half-merged tree shows up.
    """
    archive_path = _tar(
        workspace / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    target = workspace / SANDBOX
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("from before\n", encoding="utf-8")

    real_replace = os.replace
    replaced: list[str] = []

    def _flaky(source, destination):
        replaced.append(Path(destination).name)
        if len(replaced) == 2:
            raise OSError(5, "Input/output error")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", _flaky)
    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_into_place(archive_path, target)

    assert caught.value.reason == archive.PARTIAL_UNPACK
    assert caught.value.detail.startswith("publishing ")
    assert (
        f"(merge into {target}: published so far = [a.txt]; "
        "re-running finishes the merge)"
    ) in caught.value.detail
    # The accepted half-state: one member landed, everything else is the old
    # tree, and the staging tree is gone (there is nothing left to clean up).
    assert (target / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (target / "b.txt").exists() is False
    assert (target / "keep.txt").read_text(encoding="utf-8") == "from before\n"
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_the_same_materialization_converges_after_a_failed_merge(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N76: re-running finishes the merge instead of doubling it.

    Every entry is an idempotent ``os.replace``, so the one recovery action the
    operators do not have to invent is "do the same thing again".
    """
    archive_path = _tar(
        workspace / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    target = workspace / SANDBOX
    target.mkdir(parents=True)
    (target / "b.txt").write_text("stale\n", encoding="utf-8")

    real_replace = os.replace
    replaced: list[str] = []

    def _flaky(source, destination):
        replaced.append(Path(destination).name)
        if len(replaced) == 2:
            raise OSError(5, "Input/output error")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", _flaky)
    with pytest.raises(archive.ArchiveRefusal):
        archive.extract_into_place(archive_path, target)
    monkeypatch.undo()

    archive.extract_into_place(archive_path, target)

    assert [
        (target / name).read_text(encoding="utf-8")
        for name in ("a.txt", "b.txt", "c.txt")
    ] == ["a\n", "b\n", "c\n"]


def test_the_two_call_sites_share_the_one_staged_implementation() -> None:
    """The helper is the shared function object, not a per-image copy."""
    assert envd_agent.extract_into_place is archive.extract_into_place
    assert materialize.extract_into_place is archive.extract_into_place


def test_the_agents_materialization_leaves_no_half_tree(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live call site: the tree holds none of the refused payload's members.

    Run through the real ``materialize_tree``. The create made the tree root
    and its ``workspace/`` subdir before the payload lands, so the assertion is
    "the members did not land" -- that empty scaffold is not a half tree.
    """
    monkeypatch.setenv("E2B_ARCHIVE_MAX_MEMBERS", "2")
    base = workspace / "workspaces"
    base.mkdir()
    store = base / "_snapshots" / SNAPSHOT
    store.mkdir(parents=True)
    payload = _tar(
        store / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    settings = AgentSettings(
        token=KEY,
        node_id="node-a",
        workspace_base=str(base),
        state_base=str(workspace / "state"),
        shared_volume_root=str(workspace / "volumes"),
    )
    plan = {
        "sandbox_id": SANDBOX,
        "tree": {
            "path": str(base / SANDBOX),
            "subdir": "workspace",
            "mode": "0770",
            "uid": 10007,
            "gid": 65534,
            "copy_from": str(payload),
        },
        "slices": [],
    }
    runner = _RecordingRunner()

    with pytest.raises(materialize.MaterializeRefusal) as caught:
        materialize.materialize_tree(plan, settings=settings, runner=runner)

    assert caught.value.reason == "archive-too-many-members"
    tree = base / SANDBOX
    assert (tree / "a.txt").exists() is False
    assert (tree / "b.txt").exists() is False
    assert (tree / "c.txt").exists() is False
    assert (base / f"{SANDBOX}.importing").exists() is False
    # A refused materialization must not have spent any privilege.
    assert runner.calls == []


@pytest.mark.asyncio
async def test_the_workers_restore_leaves_no_tree(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other live call site: a refused restore leaves no tree root at all."""
    monkeypatch.setenv("E2B_ARCHIVE_MAX_MEMBERS", "2")
    workspace_base = workspace / "workspaces"
    workspace_base.mkdir()
    store = workspace_base / "_snapshots" / SNAPSHOT
    store.mkdir(parents=True)
    _tar(store / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        internal_api_key=KEY,
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace_base),
        workspace_base=workspace_base,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            "/agent/sandboxes",
            json={"sandboxID": SANDBOX, "snapshotID": SNAPSHOT, "hostUID": 1000},
            headers={"X-Internal-Key": KEY},
        )

    assert resp.status_code == 400
    assert (workspace_base / SANDBOX).exists() is False
    assert (workspace_base / f"{SANDBOX}.importing").exists() is False


# ---------------------------------------------------------- N67: time budget


def test_the_time_budget_refuses_a_slow_unpack(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nail: a walk past its budget refuses by name and leaves no tree.

    The clock is injected rather than raced against a real one: the walk reads
    it once at the start and once every 4096 members, so two ticks are enough
    to put the second read 500 s past the first -- deterministic, and it pins
    the *sampled* check (the refusal lands on the 4096th member).
    """
    archive_path = _many_members_tar(workspace / "fs.tar", 4096)
    monkeypatch.setattr(archive, "_now", _fixed_clock(0.0, 0.0, 500.0))
    target = workspace / SANDBOX

    with pytest.raises(archive.ArchiveRefusal) as caught:
        archive.extract_into_place(archive_path, target, max_seconds=0.001)

    assert caught.value.reason == archive.TIME_BUDGET_EXCEEDED
    assert caught.value.detail == (
        f"{archive_path} exceeded its 0.001s unpack budget after 500.000s "
        "(4096 members seen; E2B_ARCHIVE_MAX_SECONDS, 0 disables it)"
    )
    assert target.exists() is False
    assert (workspace / f"{SANDBOX}.importing").exists() is False


def test_the_time_budget_is_sampled_not_read_per_member(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One clock read to start, one at the first member: not one per member."""
    archive_path = _many_members_tar(workspace / "fs.tar", 200)
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0

    monkeypatch.setattr(archive, "_now", clock)
    monkeypatch.delenv("E2B_ARCHIVE_MAX_SECONDS", raising=False)

    written = archive.extract_into_place(archive_path, workspace / SANDBOX)

    assert written == 200
    assert calls["n"] == 2


def test_a_zero_budget_never_consults_the_clock(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_seconds=0`` is "off": no clock read at all, and the tar lands."""
    archive_path = _many_members_tar(workspace / "fs.tar", 64)

    def _boom() -> float:
        raise AssertionError("the unpack read the clock with the budget off")

    monkeypatch.setattr(archive, "_now", _boom)
    dest = workspace / "dest"
    dest.mkdir()

    written = archive.extract_sandbox_archive(archive_path, dest, max_seconds=0)

    assert written == 64
    assert sorted(path.name for path in dest.iterdir()) == [
        f"f-{index:05d}.txt" for index in range(64)
    ]


def test_the_time_budget_env_knob_is_read_and_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``E2B_ARCHIVE_MAX_SECONDS``: read at call time; only ``0`` disables."""
    assert archive.DEFAULT_ARCHIVE_MAX_SECONDS == 180.0

    monkeypatch.setenv("E2B_ARCHIVE_MAX_SECONDS", "5.5")
    assert archive.resolve_time_budget() == 5.5

    monkeypatch.setenv("E2B_ARCHIVE_MAX_SECONDS", "0")
    assert archive.resolve_time_budget() == 0.0

    monkeypatch.setenv("E2B_ARCHIVE_MAX_SECONDS", "-1")
    assert archive.resolve_time_budget() == archive.DEFAULT_ARCHIVE_MAX_SECONDS

    monkeypatch.setenv("E2B_ARCHIVE_MAX_SECONDS", "abc")
    assert archive.resolve_time_budget() == archive.DEFAULT_ARCHIVE_MAX_SECONDS

    monkeypatch.delenv("E2B_ARCHIVE_MAX_SECONDS")
    assert archive.resolve_time_budget() == archive.DEFAULT_ARCHIVE_MAX_SECONDS


def test_a_small_archive_is_unaffected_by_the_default_budget(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default budget never touches an ordinary payload."""
    monkeypatch.delenv("E2B_ARCHIVE_MAX_SECONDS", raising=False)
    archive_path = _tar(workspace / "fs.tar", {"workspace/kept.txt": "kept\n"})
    source = archive_path.parent / "fs.tar.stage"
    target = workspace / SANDBOX

    written = archive.extract_into_place(archive_path, target)

    assert written == 2
    assert _entries(target) == _entries(source)


# ------------------------------------------- the agent's own status mapping


def test_the_member_cap_and_the_time_budget_answer_502() -> None:
    """The family rule (Task 11, N75): "the step could not be carried out" is 502.

    400 is left to the refusals that describe the *derived plan* itself
    (``bad-plan``). A link the live tree holds (N75) is not one of those -- the
    plan named a clean member -- so it answers 502 with the other
    step-failed reasons.
    """
    assert _materialize_status(materialize.TOO_MANY_MEMBERS) == 502
    assert _materialize_status(materialize.TIME_BUDGET_EXCEEDED) == 502
    assert _materialize_status(materialize.PARTIAL_COPY) == 502
    assert _materialize_status(materialize.DESTINATION_IS_A_SYMLINK) == 502
    assert _materialize_status(materialize.TREE_TOO_LARGE) == 413
    assert _materialize_status(materialize.BAD_PLAN) == 400


@pytest.mark.asyncio
async def test_the_agents_own_surface_answers_502_for_the_cap(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status reaches the wire, not only the helper."""
    monkeypatch.setenv("E2B_ARCHIVE_MAX_MEMBERS", "2")
    base = workspace / "workspaces"
    base.mkdir()
    store = base / "_snapshots" / SNAPSHOT
    store.mkdir(parents=True)
    payload = _tar(
        store / "fs.tar", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"}
    )
    settings = AgentSettings(
        token=KEY,
        node_id="node-a",
        workspace_base=str(base),
        state_base=str(workspace / "state"),
        shared_volume_root=str(workspace / "volumes"),
        uid_pool_start=10000,
        uid_pool_size=1000,
        maint_path="/usr/lib/e2b-priv/e2b-maint",
    )
    app = create_agent_app(
        settings=settings, maint_runner=_RecordingRunner(), inventory=None
    )
    plan = {
        "worker": {"uid": 65534, "gid": 65534},
        "sandbox_id": SANDBOX,
        "tree": {
            "path": str(base / SANDBOX),
            "subdir": "workspace",
            "mode": "0770",
            "uid": 10007,
            "gid": 65534,
            "copy_from": str(payload),
        },
        "slices": [],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent"
    ) as client:
        resp = await client.post(
            "/internal/nodes/node-a/agent/materialize",
            json=plan,
            headers={"X-Internal-Key": KEY},
        )

    assert resp.status_code == 502
    # The create made the tree root (and its ``workspace/`` subdir) before the
    # payload lands, so the members are what must not be there.
    tree = base / SANDBOX
    assert (tree / "a.txt").exists() is False
    assert (tree / "b.txt").exists() is False
    assert (tree / "c.txt").exists() is False
    assert (base / f"{SANDBOX}.importing").exists() is False
