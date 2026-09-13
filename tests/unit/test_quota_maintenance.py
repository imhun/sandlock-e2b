"""E2.4: orphan project reconciliation + disk watermark monitoring."""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path

import httpx
import pytest

import envd_service.agent as agent
import envd_service.app as app_module
import envd_service.quota_maintenance as quota_maintenance
import envd_service.xfs_quota as xfs_quota
from envd_service import xfs_quotactl
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.nodes import NodeRegistry
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.quota_maintenance import QuotaMonitor
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import (
    ProjectDirectoryGone,
    ProjectDirectoryUnreadable,
    ProjectQuotaError,
    ProjectQuotaUsage,
    _parse_project_usage,
    project_quota_table,
    reconcile_orphan_projects,
)

MOUNT = "/srv/sandboxes"


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _report(*projects: str) -> str:
    lines = [
        f"Project quota on {MOUNT} (/dev/loop0)",
        "                               Blocks",
        "Project ID       Used       Soft       Hard    Warn/Time",
    ]
    lines.extend(projects)
    return "\n".join(lines) + "\n"


def _fake_subprocess(monkeypatch, quota_responses, lsattr_stdout: str = ""):
    """Patch subprocess.run; quota commands keyed by the -c command string."""
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "lsattr":
            return _FakeProc(0, stdout=lsattr_stdout)
        command = args[args.index("-c") + 1]
        returncode, stdout, stderr = quota_responses.get(command, (0, "", ""))
        return _FakeProc(returncode, stdout, stderr)

    monkeypatch.setattr(xfs_quota.subprocess, "run", fake_run)
    return calls


def _write_record(workspace: Path, sandbox_id: str, project_id: int | None) -> None:
    path = workspace / sandbox_id / "sandbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sandbox_id": sandbox_id, "project_id": project_id}),
        encoding="utf-8",
    )


class _DiskUsage:
    def __init__(self, used: int, total: int) -> None:
        self.used = used
        self.total = total


# ---------------------------------------------------------------- parsing


def test_parse_project_usage_extracts_used_soft_hard_blocks():
    rows = _parse_project_usage(
        _report(
            "#0                  4          0          0    00 [--------]",
            "#100               40          0       1024    00 [--------]",
            "123                512         0       4096    00 [--------]",
        )
    )
    assert rows == {
        0: ProjectQuotaUsage(0, 4, 0, 0),
        100: ProjectQuotaUsage(100, 40, 0, 1024),
        123: ProjectQuotaUsage(123, 512, 0, 4096),
    }


def test_parse_project_usage_ignores_header_and_blank_rows():
    rows = _parse_project_usage(_report())
    assert rows == {}


def test_project_quota_table_runs_report_locally(monkeypatch):
    calls = _fake_subprocess(
        monkeypatch,
        {"report -p": (0, _report("#0 4 0 0 00 [--------]"), "")},
    )
    table = project_quota_table(MOUNT)
    assert table == {0: ProjectQuotaUsage(0, 4, 0, 0)}
    assert calls == [["xfs_quota", "-x", "-c", "report -p", MOUNT]]


def test_project_quota_table_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def report(**kwargs):
        seen.update(kwargs)
        return {
            "projects": {
                "10": {"used_blocks": 1, "soft_blocks": 2, "hard_blocks": 3}
            }
        }

    monkeypatch.setattr(xfs_quota, "agent_ops", {"report": report})
    table = project_quota_table(MOUNT, via_agent=True)
    assert seen == {"mount_point": MOUNT}
    assert table == {10: ProjectQuotaUsage(10, 1, 2, 3)}


def test_project_quota_table_via_agent_invalid_rows_raise(monkeypatch):
    monkeypatch.setattr(
        xfs_quota,
        "agent_ops",
        {"report": lambda **_kw: {"projects": {"10": {"bogus": 1}}}},
    )
    with pytest.raises(ProjectQuotaError) as excinfo:
        project_quota_table(MOUNT, via_agent=True)
    assert str(excinfo.value) == (
        "quota-agent report returned invalid rows: 'used_blocks'"
    )


def test_project_quota_table_via_agent_invalid_shape_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", {"report": lambda **_kw: {"nope": 1}})
    with pytest.raises(ProjectQuotaError) as excinfo:
        project_quota_table(MOUNT, via_agent=True)
    assert str(excinfo.value) == (
        "quota-agent report returned invalid data: {'nope': 1}"
    )


# ----------------------------------------------------- orphan reconciliation


def test_reconcile_cleans_zero_usage_orphan_and_keeps_recorded(tmp_path, monkeypatch):
    _write_record(tmp_path, "sbx_keep", project_id=100)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#100                0          0       1024    00 [--------]",
                    "#200                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {"cleaned": [200], "skipped": []}
    # Zero-usage orphans need no directory scan: only the limit reset runs.
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 200", MOUNT],
    ]


def test_scan_project_dirs_keeps_sandbox_trees_only(tmp_path, monkeypatch):
    """The shared workspace filter: reserved roots, symlinks and plain files
    are never candidates of the quota orphan scan.

    ``_`` is a legal sandbox-id character, so ``validate_sandbox_id`` alone
    would hand the snapshot / migration / volume / template / secrets stores
    to the scan. The backend selector is pinned to the subprocess form so the
    assertion is the same on every host (the fd backend probe is Linux-only).
    """
    keep = tmp_path / "sbx_keep"
    keep.mkdir()
    for name in ("_snapshots", "_migrate", "_cow", "_volumes", "_templates", "_secrets"):
        (tmp_path / name).mkdir()
    (tmp_path / "sbx_symlinked").symlink_to(tmp_path / "_snapshots")
    (tmp_path / "sbx_file").write_text("not a tree", encoding="utf-8")
    monkeypatch.setattr(xfs_quota, "_use_quotactl", lambda mount_point: False)
    calls = _fake_subprocess(
        monkeypatch, {}, lsattr_stdout=f"     700 ---------------- {keep}\n"
    )
    assert xfs_quota._scan_project_dirs(tmp_path) == {700: keep}
    assert calls == [["lsattr", "-p", "-d", str(keep)]]


def test_scan_project_dirs_separates_the_store_from_a_prefixed_tree(
    tmp_path, monkeypatch
):
    """M1 rework: ``snap_`` is separated by shape, not by name.

    ``SnapshotRegistry``'s base is the workspace base, so a snapshot store sits
    at the top level next to the ``sbx_*`` trees and passes
    ``validate_sandbox_id`` (``_`` is a legal id character). But the prefix is
    not reserved on the create side — ``X-Sandbox-Id`` goes through
    ``validate_sandbox_id`` alone — so ``snap_client1`` is a legal *sandbox* id
    whose tree carries its own top-level ``sandbox.json``. Under the old
    name-only exclusion that tree never entered this mapping, while
    ``_recorded_projids`` (record-driven, it reads ``sandbox.json`` directly)
    kept pinning its row: the row could never be reclaimed (review M1).

    The consumer pin here is the direction the quota scan must keep:

    * the store — ``snapshot.json`` plus the copied filesystem, with the
      sandbox record of that copy at ``fs/sandbox.json`` — carries no
      *top-level* ``sandbox.json`` and stays out of the mapping and off the
      disk read;
    * a prefixed directory that *does* carry one is a sandbox tree and enters
      the mapping, so an orphan's row can be released against its directory.
    """
    keep = tmp_path / "sbx_keep"
    keep.mkdir()
    store = tmp_path / "snap_0040ce7e44f6365f"
    (store / "fs").mkdir(parents=True)
    (store / "snapshot.json").write_text("{}", encoding="utf-8")
    (store / "fs" / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_snapshotted", "project_id": 702}),
        encoding="utf-8",
    )
    chosen = tmp_path / "snap_client1"
    chosen.mkdir()
    (chosen / "sandbox.json").write_text(
        json.dumps({"sandbox_id": chosen.name, "project_id": 701}),
        encoding="utf-8",
    )
    monkeypatch.setattr(xfs_quota, "_use_quotactl", lambda mount_point: False)
    calls = _fake_subprocess(
        monkeypatch,
        {},
        lsattr_stdout=f"     700 ---------------- {keep}\n"
        f"     701 ---------------- {chosen}\n",
    )
    assert xfs_quota._scan_project_dirs(tmp_path) == {700: keep, 701: chosen}
    # The store never reaches the disk read; the prefixed tree does, because it
    # is a sandbox tree.
    assert calls == [["lsattr", "-p", "-d", str(keep), str(chosen)]]


# ------------------------------------ reading one directory's project id (FU-1)
#
# The 12 production WARNINGs of 2026-09-12: ``DELETE /volumes/<id>`` rmtree'd
# six volume roots (and their slices) 5h40m before worker-1 started, so every
# slice the teardown asked about was already gone. On that line of code one
# probe answered three different questions -- "can this mount administer
# quotas", "is this directory reachable", "may this process read it" -- and
# the only visible outcome was the same WARNING a real permission problem
# produces, wrapped around a missing ``lsattr`` (the production image has no
# e2fsprogs). The read must answer them separately: the fd read needs
# ``FS_IOC_FSGETXATTR`` only, the backend decision is keyed on the mount, and
# the failure is classified as gone / unreadable / cannot-ask.


def _explode_lsattr(monkeypatch) -> list[list[str]]:
    """Reproduce the production image: every ``lsattr`` run fails to exec."""
    calls: list[list[str]] = []

    def missing_binary(argv, *args, **kwargs):
        calls.append(list(argv))
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "lsattr")

    monkeypatch.setattr(xfs_quota.subprocess, "run", missing_binary)
    return calls


def test_a_deleted_slice_is_gone_rather_than_a_warning(tmp_path, monkeypatch, caplog):
    """FU-1 / RED-1: a slice the control plane already deleted is not an anomaly.

    The volume deletion removes the slice with the volume root, so the read
    fails with ENOENT before any project state can be involved. The caller
    semantics must not change (no release: there is no verified
    ``(directory, projid)`` pair; the tree is still reclaimed, and the record's
    claim stays in ``expected`` so an unreclaimed row is still reported), but
    the line is INFO -- "nothing to verify" -- and ``lsattr`` is never
    consulted, on this shape, by any backend.
    """
    slice_dir = tmp_path / "_volumes" / "vol_cdbb" / "sbx_slice"
    claimed = 152695729
    lsattr_calls = _explode_lsattr(monkeypatch)
    try:
        os.open(slice_dir, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError as probe:
        gone_reason = str(probe)
    else:  # pragma: no cover - the fixture never creates the slice
        raise AssertionError("the slice directory must not exist")

    expected: set[int] = set()
    caplog.set_level(logging.INFO)
    caplog.clear()

    assert agent._verified_project_id(slice_dir, claimed, expected) == (None, None)

    assert [(record.levelname, record.message) for record in caplog.records] == [
        (
            "INFO",
            f"reconcile: {slice_dir} is gone from the disk ({gone_reason}); "
            "nothing to verify, its quota row is left to the fail-safe reconcile",
        ),
    ]
    # Fail-safe bookkeeping is unchanged: the row the record claims is reported
    # even though nothing could be released for it.
    assert expected == {claimed}
    assert lsattr_calls == []


@pytest.mark.parametrize("fd_backend", [True, False])
def test_a_missing_directory_is_gone_on_both_backends(
    tmp_path, monkeypatch, fd_backend
):
    """FU-1: the class must not depend on which read backend ran.

    ``lsattr`` reports "no such file" and "permission denied" as the same kind
    of tool failure and the fd backend folds both into ``open``, so a
    path-state read -- not the backend's message -- decides the class. The
    read must not even try ``lsattr`` for a path that is not there.
    """
    missing = tmp_path / "sbx_gone"
    lsattr_calls = _explode_lsattr(monkeypatch)
    monkeypatch.setattr(
        xfs_quota, "containing_mount_point", lambda path: str(tmp_path)
    )
    monkeypatch.setattr(xfs_quotactl, "can_read_projid", lambda mount: fd_backend)

    def failing_projid_of(path):
        raise xfs_quotactl.QuotactlError(
            f"cannot open {path}: [Errno 2] No such file or directory: '{path}'"
        )

    monkeypatch.setattr(xfs_quotactl, "projid_of", failing_projid_of)
    try:
        os.open(missing, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError as probe:
        gone_reason = str(probe)
    else:  # pragma: no cover - the fixture never creates the slice
        raise AssertionError("the slice directory must not exist")

    with pytest.raises(ProjectDirectoryGone) as excinfo:
        xfs_quota.directory_project_id(missing)

    assert str(excinfo.value) == (
        f"{missing} is gone from the disk ({gone_reason})"
    )
    assert lsattr_calls == []


def test_a_projid_read_is_gated_on_the_mount_not_on_quota_administration(
    tmp_path, monkeypatch
):
    """FU-1 / RED-2: the read needs the ioctl, not quota administration.

    Measured on a real XFS mount without ``prjquota``: ``state()`` fails
    ENOSYS and ``available()`` is False while ``projid_of(dir)`` still reads
    the stored id (tmp/fu1-06-gate-vs-read.log). The backend decision must
    therefore be keyed on the *mount* -- the fd backend probe opens whatever
    it is given, so asking it about the directory being read is what turned
    "this slice is gone" into "this backend does not work".
    """
    tree = tmp_path / "sbx_tree"
    tree.mkdir()
    probed: list[str] = []
    lsattr_calls = _explode_lsattr(monkeypatch)
    monkeypatch.setattr(
        xfs_quota, "containing_mount_point", lambda path: str(tmp_path)
    )
    monkeypatch.setattr(
        xfs_quotactl,
        "can_read_projid",
        lambda mount: probed.append(str(mount)) or True,
    )
    monkeypatch.setattr(xfs_quotactl, "available", lambda mount: False)
    monkeypatch.setattr(xfs_quotactl, "projid_of", lambda path: 4242)

    assert xfs_quota.directory_project_id(tree) == 4242
    # The mount answered the backend question, the directory never did.
    assert probed == [str(tmp_path)]
    assert lsattr_calls == []
    # ... and this is a mount whose quotas this worker cannot administer.
    assert xfs_quota._use_quotactl(tmp_path) is False


def test_a_directory_the_worker_may_not_read_keeps_its_own_diagnosis(
    tmp_path, monkeypatch
):
    """FU-1 / RED-3: EACCES is neither "gone" nor "cannot ask the disk".

    A slice that exists but cannot be read (a tenant chmod'ing its own slice
    0700, or a 0710 directory) is a real anomaly and stays a WARNING with its
    own reason, so an operator does not have to stat the path by hand to find
    out which of the three shapes they are looking at.
    """
    tree = tmp_path / "sbx_private"
    tree.mkdir()
    denial = PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(tree))
    monkeypatch.setattr(
        xfs_quota, "_directory_read_error", lambda directory: denial
    )
    monkeypatch.setattr(
        xfs_quota, "containing_mount_point", lambda path: str(tmp_path)
    )
    monkeypatch.setattr(xfs_quotactl, "can_read_projid", lambda mount: True)

    def denied(path):
        raise xfs_quotactl.QuotactlError(
            f"cannot open {path}: {denial}"
        )

    monkeypatch.setattr(xfs_quotactl, "projid_of", denied)

    with pytest.raises(ProjectDirectoryUnreadable) as excinfo:
        xfs_quota.directory_project_id(tree)

    assert str(excinfo.value) == (
        f"{tree} exists but this worker cannot read it ({denial})"
    )


# ------------------------------------ follow-up 2: cache lifetime + mount key
#
# The FU-1 read gate is keyed on the mount, and both backend verdicts were
# cached for the life of the process. Two shapes followed from that:
#
# * a *transient* probe failure -- a mount the container has not finished
#   setting up, a mount point this process cannot open at that instant -- was
#   remembered as "this mount has no fd backend", which pinned every later
#   read on it to ``lsattr`` (absent from the production image, so every read
#   degraded to "cannot ask the disk") until the process restarted;
# * a mount point this worker may not open condemned the directories under it
#   even when those directories are perfectly readable, because the probe
#   opened the mount root while the read opens the directory.
#
# Only the positive verdict is remembered now, and the directory being read
# gets the last word when the mount cannot answer.


def test_a_transient_read_probe_failure_is_not_remembered(tmp_path, monkeypatch):
    """follow-up 2: a probe that failed once is asked again on the next read.

    The mount is unreadable at the first read (the container-start race) and
    readable at the second one, in the same process. Remembering the first
    answer would keep the fd backend -- the only one production has, since the
    image ships no ``lsattr`` -- out of reach until a restart; the second read
    must therefore go back to it.
    """
    tree = tmp_path / "sbx_tree"
    tree.mkdir()
    readable = {"mount": False}
    probed: list[str] = []
    monkeypatch.setattr(
        xfs_quota, "containing_mount_point", lambda path: str(tmp_path)
    )

    def can_read_projid(path):
        probed.append(str(path))
        return readable["mount"] if str(path) == str(tmp_path) else False

    monkeypatch.setattr(xfs_quotactl, "can_read_projid", can_read_projid)
    monkeypatch.setattr(xfs_quotactl, "projid_of", lambda path: 4242)
    lsattr_calls = _explode_lsattr(monkeypatch)
    xfs_quota._PROJID_READ_CACHE.clear()

    # The mount cannot answer yet: the mount is asked, then the directory,
    # and the read degrades to the fallback the same way it always did.
    with pytest.raises(ProjectQuotaError) as excinfo:
        xfs_quota.directory_project_id(tree)
    assert str(excinfo.value) == (
        f"cannot ask the disk for the project id of {tree} "
        f"(lsattr could not run for {tree}: [Errno 2] No such file or "
        f"directory: 'lsattr')"
    )

    # The very next read, once the mount answers, uses the fd backend: the
    # failure was not remembered and ``lsattr`` is not consulted again.
    readable["mount"] = True
    assert xfs_quota.directory_project_id(tree) == 4242

    assert probed == [str(tmp_path), str(tree), str(tmp_path)]
    assert lsattr_calls == [["lsattr", "-p", "-d", str(tree)]]
    assert xfs_quota._PROJID_READ_CACHE == {str(tmp_path): "quotactl"}


def test_a_transient_administration_probe_failure_is_not_remembered(
    tmp_path, monkeypatch
):
    """follow-up 2: the same rule for the management backend verdict.

    ``set_limit``/``release``/the orphan scan pick their backend through
    ``_use_quotactl``, whose verdict was cached the same way. A mount that
    could not be asked at startup must not keep the subprocess path forever.
    A verdict of "yes", on the other hand, is a property of the mounted
    filesystem and is still remembered: the fd backend is not re-detected on
    every management call.
    """
    mount = tmp_path / "mnt"
    answers = iter([False, True, True])
    probed: list[str] = []

    def available(mount_point):
        probed.append(str(mount_point))
        return next(answers)

    monkeypatch.delenv(xfs_quota.XFS_QUOTA_BACKEND_ENV, raising=False)
    monkeypatch.setattr(xfs_quotactl, "available", available)
    xfs_quota._BACKEND_CACHE.clear()

    assert xfs_quota._use_quotactl(mount) is False
    assert xfs_quota._use_quotactl(mount) is True     # re-probed, not pinned
    assert xfs_quota._use_quotactl(mount) is True     # the yes is remembered
    assert probed == [str(mount), str(mount)]


def test_an_unreadable_mount_root_does_not_condemn_a_readable_directory(
    tmp_path, monkeypatch
):
    """follow-up 2: the mount answers first, the directory has the last word.

    ``/var/lib`` at ``0711`` (owner root, others may traverse but not open)
    with a readable workspace base inside it is the shape: the mount-level
    probe opens ``/var/lib`` and fails, while the directory the read actually
    opens is readable and carries its project id. Reading the mount's answer
    as final turned that into "cannot ask the disk" for every tree under it.
    """
    mount_root = tmp_path / "var-lib"
    tree = mount_root / "e2b-sandboxes" / "sbx_tree"
    tree.mkdir(parents=True)
    probed: list[str] = []
    monkeypatch.setattr(
        xfs_quota, "containing_mount_point", lambda path: str(mount_root)
    )

    def can_read_projid(path):
        probed.append(str(path))
        return Path(path) == tree

    monkeypatch.setattr(xfs_quotactl, "can_read_projid", can_read_projid)
    monkeypatch.setattr(xfs_quotactl, "projid_of", lambda path: 4242)
    lsattr_calls = _explode_lsattr(monkeypatch)
    xfs_quota._PROJID_READ_CACHE.clear()

    # The mount root alone answers "no" -- the shape that read as "cannot ask
    # the disk" before the directory was given the last word.
    assert xfs_quota._use_quotactl_read(mount_root) is False
    assert xfs_quota.directory_project_id(tree) == 4242
    assert probed == [str(mount_root), str(mount_root), str(tree)]
    assert lsattr_calls == []


def test_reconcile_cleans_orphan_with_leftover_dir_but_keeps_dir(
    tmp_path, monkeypatch, caplog
):
    orphan_dir = tmp_path / "sbx_orphan"
    orphan_dir.mkdir()
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#300               512         0       4096    00 [--------]"),
                "",
            )
        },
        lsattr_stdout=f"     300 ---------------- {orphan_dir}\n",
    )
    caplog.set_level(logging.INFO)
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {"cleaned": [300], "skipped": []}
    # Directory project state is cleared, the quota record dropped, but the
    # directory itself is kept (orphan cleanup never deletes user files).
    assert orphan_dir.is_dir()
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["lsattr", "-p", "-d", str(orphan_dir)],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -C -p {orphan_dir} 300",
            MOUNT,
        ],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 300", MOUNT],
    ]
    assert [r.message for r in caplog.records] == ["cleaned orphan project 300"]


def test_reconcile_keeps_normal_projids_and_project_zero(tmp_path, monkeypatch):
    _write_record(tmp_path, "sbx_a", project_id=100)
    _write_record(tmp_path, "sbx_b", project_id=200)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#100                0          0       1024    00 [--------]",
                    "#200                0          0       1024    00 [--------]",
                    "#300                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    # Only the unrecorded projid is cleaned; recorded ones and project 0 stay.
    assert result == {"cleaned": [300], "skipped": []}
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["xfs_quota", "-x", "-c", "limit -p bsoft=0 bhard=0 300", MOUNT],
    ]


def test_reconcile_skips_used_orphan_without_dir(tmp_path, monkeypatch):
    # A sandbox-shaped directory exists but carries a different projid, so
    # the scan runs and finds no directory for the orphaned id.
    (tmp_path / "sbx_unrelated").mkdir()
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#400                10          0       1024    00 [--------]"),
                "",
            )
        },
        lsattr_stdout="",
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {
        "cleaned": [],
        "skipped": [
            {
                "projid": 400,
                "reason": (
                    "10 used blocks but no project directory; "
                    "entry left for manual review"
                ),
            }
        ],
    }
    # No destructive command ran: report + read-only dir scan only.
    assert calls == [
        ["xfs_quota", "-x", "-c", "report -p", MOUNT],
        ["lsattr", "-p", "-d", str(tmp_path / "sbx_unrelated")],
    ]


def test_reconcile_cleanup_failure_skips_with_warning(tmp_path, monkeypatch, caplog):
    _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#200                0          0       2048    00 [--------]"),
                "",
            ),
            "limit -p bsoft=0 bhard=0 200": (1, "", "limit boom"),
        },
    )
    caplog.set_level(logging.WARNING)
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    assert result == {
        "cleaned": [],
        "skipped": [
            {
                "projid": 200,
                "reason": (
                    "xfs_quota 'limit -p bsoft=0 bhard=0 200' failed: limit boom"
                ),
            }
        ],
    }
    assert [r.message for r in caplog.records] == [
        "orphan project 200 cleanup failed: "
        "xfs_quota 'limit -p bsoft=0 bhard=0 200' failed: limit boom",
    ]


def test_reconcile_ignores_malformed_and_none_records(tmp_path, monkeypatch):
    bad = tmp_path / "sbx_bad"
    bad.mkdir()
    (bad / "sandbox.json").write_text("{not json", encoding="utf-8")
    _write_record(tmp_path, "sbx_none", project_id=None)
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report("#777                0          0       1024    00 [--------]"),
                "",
            )
        },
    )
    result = reconcile_orphan_projects(
        workspace_base=tmp_path, mount_point=MOUNT
    )
    # Neither a malformed record nor project_id=None protects a projid.
    assert result == {"cleaned": [777], "skipped": []}
    assert calls[-1] == [
        "xfs_quota",
        "-x",
        "-c",
        "limit -p bsoft=0 bhard=0 777",
        MOUNT,
    ]


def test_reconcile_fail_closed_when_workspace_base_missing(tmp_path, monkeypatch):
    # A zero-usage projid that would be cleaned if the missing base were read
    # as "no recorded projects"; the base is missing so reconcile must refuse.
    calls = _fake_subprocess(
        monkeypatch,
        {
            "report -p": (
                0,
                _report(
                    "#0                  4          0          0    00 [--------]",
                    "#200                0          0       2048    00 [--------]",
                ),
                "",
            )
        },
    )
    missing = tmp_path / "no-such-workspace"
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(workspace_base=missing, mount_point=MOUNT)
    assert str(excinfo.value) == (
        "reconcile workspace_base missing or unreadable: "
        f"{missing} (FileNotFoundError)"
    )
    # Read-only report only: no limit reset ran (fail-closed, nothing wiped).
    assert calls == [["xfs_quota", "-x", "-c", "report -p", MOUNT]]


def test_reconcile_via_agent_dispatches(monkeypatch):
    seen: dict = {}

    def reconcile(**kwargs):
        seen.update(kwargs)
        return {"cleaned": [7], "skipped": []}

    monkeypatch.setattr(xfs_quota, "agent_ops", {"reconcile": reconcile})
    result = reconcile_orphan_projects(
        workspace_base="/nfs/sandboxes",
        mount_point="/nfs",
        via_agent=True,
    )
    assert seen == {"workspace_base": "/nfs/sandboxes", "mount_point": "/nfs"}
    assert result == {"cleaned": [7], "skipped": []}


def test_reconcile_via_agent_not_configured_raises(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(
            workspace_base="/nfs/sandboxes",
            mount_point="/nfs",
            via_agent=True,
        )
    assert str(excinfo.value) == "quota-agent not configured (E2.6)"


def test_reconcile_via_agent_invalid_data_raises(monkeypatch):
    monkeypatch.setattr(
        xfs_quota, "agent_ops", {"reconcile": lambda **_kw: ["not", "a", "dict"]}
    )
    with pytest.raises(ProjectQuotaError) as excinfo:
        reconcile_orphan_projects(
            workspace_base="/nfs/sandboxes",
            mount_point="/nfs",
            via_agent=True,
        )
    assert str(excinfo.value) == (
        "quota-agent reconcile returned invalid data: ['not', 'a', 'dict']"
    )


# ------------------------------------------------------------ quota monitor


def _monitor_with_quota(monkeypatch, table, disk_usage=None):
    monitor = QuotaMonitor(
        workspace_base=MOUNT,
        mount_point=MOUNT,
        quota_warn_ratio=0.9,
        disk_warn_ratio=0.9,
        disk_error_ratio=0.98,
    )
    monkeypatch.setattr(
        quota_maintenance, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(
        quota_maintenance, "project_quota_table", lambda _mp, via_agent=False: table
    )
    monkeypatch.setattr(
        quota_maintenance.shutil,
        "disk_usage",
        lambda _path: disk_usage or _DiskUsage(used=10, total=1000),
    )
    return monitor


def test_monitor_reports_over_and_near_limit(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {
            10: ProjectQuotaUsage(10, 100, 0, 100),
            20: ProjectQuotaUsage(20, 95, 0, 100),
            30: ProjectQuotaUsage(30, 50, 0, 100),
            40: ProjectQuotaUsage(40, 0, 0, 0),
        },
    )
    caplog.set_level(logging.WARNING)
    summary = monitor.inspect_once()
    assert summary["over_limit"] == [10]
    assert summary["near_limit"] == [20]
    assert monitor.over_limit_count == 1
    assert monitor.near_limit_count == 1
    assert monitor.metrics() == {
        "quotaOverLimit": [10],
        "quotaNearLimit": [20],
        "quotaOverLimitCount": 1,
        "quotaNearLimitCount": 1,
        "diskWarnCount": 0,
        "diskErrorCount": 0,
    }
    assert [r.message for r in caplog.records] == [
        "sandbox quota over limit: projid 10 used 100 blocks hard 100",
        "sandbox quota near limit: projid 20 used 95 blocks hard 100",
    ]
    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.WARNING]


def test_monitor_second_scan_accumulates_counts(monkeypatch):
    monitor = _monitor_with_quota(
        monkeypatch,
        {10: ProjectQuotaUsage(10, 100, 0, 100)},
    )
    monitor.inspect_once()
    monitor.inspect_once()
    assert monitor.over_limit_count == 2
    assert monitor.over_limit == [10]


def test_monitor_disk_watermark_warning(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {},
        disk_usage=_DiskUsage(used=950, total=1000),
    )
    caplog.set_level(logging.WARNING)
    summary = monitor.inspect_once()
    assert summary["disk_used_ratio"] == 0.95
    assert monitor.disk_warn_count == 1
    assert monitor.disk_error_count == 0
    assert [r.message for r in caplog.records] == [
        "workspace disk watermark warning: 95.0% used (950/1000 bytes)",
    ]


def test_monitor_disk_watermark_critical(monkeypatch, caplog):
    monitor = _monitor_with_quota(
        monkeypatch,
        {},
        disk_usage=_DiskUsage(used=990, total=1000),
    )
    caplog.set_level(logging.WARNING)
    monitor.inspect_once()
    assert monitor.disk_warn_count == 0
    assert monitor.disk_error_count == 1
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


def test_monitor_skips_quota_when_unsupported(monkeypatch):
    monitor = QuotaMonitor(
        workspace_base=MOUNT,
        mount_point=MOUNT,
        disk_warn_ratio=0.9,
        disk_error_ratio=0.98,
    )
    monkeypatch.setattr(
        quota_maintenance,
        "xfs_project_supported",
        lambda _mp, via_agent=False: (False, "filesystem is ext4, not xfs"),
    )
    monkeypatch.setattr(
        quota_maintenance,
        "project_quota_table",
        lambda *_a, **_kw: pytest.fail("quota table must not be read"),
    )
    monkeypatch.setattr(
        quota_maintenance.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=10, total=1000),
    )
    summary = monitor.inspect_once()
    assert summary["over_limit"] == []
    assert summary["near_limit"] == []
    assert summary["disk_used_ratio"] == 0.01


def test_monitor_rejects_invalid_ratios():
    with pytest.raises(ValueError):
        QuotaMonitor(
            workspace_base=MOUNT,
            mount_point=MOUNT,
            quota_warn_ratio=0.0,
        )
    with pytest.raises(ValueError):
        QuotaMonitor(
            workspace_base=MOUNT,
            mount_point=MOUNT,
            disk_warn_ratio=0.99,
            disk_error_ratio=0.5,
        )


# ----------------------------------------- heartbeat payload / control plane


def test_heartbeat_usage_payload_includes_disk_and_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=512 * 1024 * 1024, total=1024 * 1024 * 1024),
    )
    payload = agent._heartbeat_usage_payload(
        EnvdSettings(workspace_base=tmp_path),
        lambda: {
            "quotaOverLimit": [10],
            "quotaNearLimit": [20],
            "quotaOverLimitCount": 3,
            "quotaNearLimitCount": 2,
            "diskWarnCount": 1,
            "diskErrorCount": 2,
        },
    )
    assert payload == {
        "diskUsedMB": 512,
        "diskTotalMB": 1024,
        "quotaOverLimit": [10],
        "quotaNearLimit": [20],
        "quotaOverLimitCount": 3,
        "quotaNearLimitCount": 2,
        "diskWarnCount": 1,
        "diskErrorCount": 2,
    }


def test_heartbeat_usage_payload_survives_broken_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(
        agent.shutil,
        "disk_usage",
        lambda _path: _DiskUsage(used=1, total=1000),
    )

    def broken():
        raise RuntimeError("boom")

    payload = agent._heartbeat_usage_payload(
        EnvdSettings(workspace_base=tmp_path), broken
    )
    assert payload == {"diskUsedMB": 0, "diskTotalMB": 0}


def test_node_record_update_usage_exposed_in_to_dict():
    registry = NodeRegistry()
    record = registry.register(
        node_id="node_u",
        address="http://127.0.0.1:49983",
        total_memory_mb=1024,
        total_cpu_percent=200,
        total_disk_mb=4096,
        total_processes=128,
    )
    record.update_usage(
        used_disk_mb=1234,
        disk_total_mb=4096,
        quota_over_limit=[9, 10],
        quota_near_limit=[11],
        quota_over_limit_count=5,
        quota_near_limit_count=2,
        disk_warn_count=1,
        disk_error_count=3,
    )
    data = record.to_dict()
    assert data["usedDiskMB"] == 1234
    assert data["diskTotalMB"] == 4096
    assert data["quotaOverLimit"] == [9, 10]
    assert data["quotaNearLimit"] == [11]
    assert data["quotaOverLimitCount"] == 5
    assert data["quotaNearLimitCount"] == 2
    assert data["diskWarnCount"] == 1
    assert data["diskErrorCount"] == 3


async def test_heartbeat_endpoint_stores_usage_snapshot(tmp_path):
    control = create_control_app(
        settings=ControlSettings(api_keys=("local-key",)),
        runtime_registry=RuntimeRegistry(tmp_path),
        workspace_base=tmp_path,
    )
    headers = {"X-Internal-Key": "internal-key"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        registered = await client.post(
            "/internal/nodes/register",
            headers=headers,
            json={
                "address": "http://127.0.0.1:49983",
                "totalMemoryMB": 1024,
                "totalCPUPercent": 200,
                "totalDiskMB": 4096,
                "totalProcesses": 128,
            },
        )
        node_id = registered.json()["nodeID"]
        response = await client.post(
            f"/internal/nodes/{node_id}/heartbeat",
            headers=headers,
            json={
                "diskUsedMB": 42,
                "diskTotalMB": 4096,
                "quotaOverLimit": [9],
                "quotaNearLimit": [],
                "quotaOverLimitCount": 5,
                "quotaNearLimitCount": 0,
                "diskWarnCount": 1,
                "diskErrorCount": 2,
            },
        )
        assert response.status_code == 204
        record = control.state.nodes.get(node_id)
        assert record.used_disk_mb == 42
        assert record.disk_total_mb == 4096
        assert record.quota_over_limit == [9]
        assert record.quota_near_limit == []
        assert record.quota_over_limit_count == 5
        assert record.quota_near_limit_count == 0
        assert record.disk_warn_count == 1
        assert record.disk_error_count == 2


# ------------------------------------------------------------- app lifespan


async def test_app_lifespan_starts_quota_maintenance(tmp_path, monkeypatch):
    calls: dict = {}

    def fake_reconcile(**kwargs):
        calls.update(kwargs)
        return {"cleaned": [1], "skipped": []}

    monkeypatch.setattr(app_module, "reconcile_orphan_projects", fake_reconcile)
    monkeypatch.setattr(
        quota_maintenance,
        "xfs_project_supported",
        lambda _mp, via_agent=False: (False, "not xfs"),
    )
    app = create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    async with app.router.lifespan_context(app):
        assert app.state.quota_monitor is not None
        assert app.state.reconcile_task is not None
        await app.state.reconcile_task
        assert calls == {
            "workspace_base": tmp_path,
            "mount_point": tmp_path,
            "via_agent": False,
        }
    assert app.state.quota_monitor._task is None


def test_create_app_nonroot_discloses_direct_quota_downgrade(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Important-1/2): a non-root worker without effective
    CAP_SYS_ADMIN on an XFS workspace with the direct local quota path (no
    E2B_QUOTA_AGENT_URL, the A6 agent-form switch) must get a startup warning
    with the required configuration instead of silently losing disk hard
    limits."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(xfs_quota, "_has_effective_cap_sys_admin", lambda: False)
    mount_path = str(Path(tmp_path).resolve())
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: f"/dev/nvme0n1p2 {mount_path} xfs rw,prjquota 0 0\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        # E2B_PRIV_HELPERS=off pins the *non-root in-process* shape this test
        # is about: with the Track F brokers installed, a non-root worker
        # keeps per-sandbox uids and the E3.2 disclosure no longer applies
        # (tests/unit/test_priv_helpers.py covers that shape).
        settings=EnvdSettings(
            executor="local", workspace_base=tmp_path, priv_helpers="off"
        ),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r.message for r in caplog.records if r.name == "envd_service.app"] == [
        app_module.PER_UID_NONROOT_WARNING,
        f"{xfs_quota.NONROOT_DIRECT_QUOTA_REASON} (direct xfs_quota requires "
        "root/CAP_SYS_ADMIN; per-sandbox disk hard limits are disabled while "
        "the agent form is off; set E2B_QUOTA_AGENT_URL to the quota-agent, "
        "or run the worker as root)",
    ]


def test_create_app_nonroot_with_sys_admin_cap_no_disclosure(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Important-2): a non-root worker with effective
    CAP_SYS_ADMIN (k8s runAsUser 65534 + SYS_ADMIN) keeps the direct quota
    path, so the startup downgrade warning must not fire."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_self_status",
        lambda: "Name:\tpytest\nCapEff:\t0000003fffffffff\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        # The Track F brokers live in /var/lib/e2b-priv, outside this test's
        # tmp_path workspace, so a simulated non-root worker would (correctly)
        # refuse the broker shape here. These tests pin the non-root
        # *in-process* disclosure, so they ask for it explicitly.
        settings=EnvdSettings(
            executor="local", workspace_base=tmp_path, priv_helpers="off"
        ),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_nonroot_non_xfs_host_no_disclosure(
    tmp_path, monkeypatch, caplog
):
    """E5.1 review (Minor-13): on a non-XFS host the startup warning must
    not blame missing root/CAP_SYS_ADMIN; the real filesystem reason is
    reported by detection instead."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    monkeypatch.setattr(xfs_quota, "_has_effective_cap_sys_admin", lambda: False)
    mount_path = str(Path(tmp_path).resolve())
    monkeypatch.setattr(
        xfs_quota,
        "_read_proc_mounts",
        lambda: f"/dev/disk1s5 {mount_path} apfs rw,local 0 0\n",
    )
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(
            executor="local", workspace_base=tmp_path, priv_helpers="off"
        ),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_root_direct_quota_no_disclosure(tmp_path, monkeypatch, caplog):
    """Root workers keep the direct xfs_quota path; no downgrade warning."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(executor="local", workspace_base=tmp_path),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []


def test_create_app_nonroot_via_agent_no_disclosure(tmp_path, monkeypatch, caplog):
    """Non-root + E2B_QUOTA_VIA_AGENT=true delegates to quota-agent, so the
    direct-path downgrade warning must not fire."""
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    caplog.set_level(logging.WARNING)
    create_envd_app(
        settings=EnvdSettings(
            executor="local",
            workspace_base=tmp_path,
            quota_via_agent=True,
            priv_helpers="off",
        ),
        runtime_registry=RuntimeRegistry(tmp_path),
    )
    assert [r for r in caplog.records if "磁盘配额不可用" in r.message] == []
