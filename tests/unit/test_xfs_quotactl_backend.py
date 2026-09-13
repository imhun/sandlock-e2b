"""Device-free XFS quota backend (B1).

The quota-agent runs where ``xfs_quota`` cannot: a container that only
bind-mounts the filesystem has no ``/dev/nvme0n1p2``, so every ``xfs_quota -x``
subcommand fails with ENXIO (measured 2026-09-12). These tests pin the fd-based
replacement, including the two mistakes the probe caught on the target:

* ``fs_disk_quota`` counts **512-byte basic blocks** (writing 4 MiB as 4,194,304
  blocks silently meant 2 GiB and the limit never bit);
* ``Q_XGETNEXTQUOTA`` reports ghost dquots (all-zero limits/usage) that must not
  show up as projects.
"""

from __future__ import annotations

import ctypes
import errno
import struct
import threading
from pathlib import Path

import pytest

from envd_service import xfs_quotactl as q


def _fill_disk_quota(buf, projid: int, hard: int, soft: int, used: int) -> None:
    struct.pack_into("<b", buf, 0, q._FS_DQUOT_VERSION)
    struct.pack_into("<I", buf, 4, projid)
    struct.pack_into("<Q", buf, 8, hard)
    struct.pack_into("<Q", buf, 16, soft)
    struct.pack_into("<Q", buf, 40, used)


#: How many bytes this kernel copies for each command -- the whole structure,
#: not the prefix the caller happens to allocate. Measured on Linux 7.0.x
#: twice, independently: a guard page right behind the buffer (the smallest
#: allocation the syscall completes with) and a 0xAA-prefilled buffer (the
#: last byte the kernel touches). Logs: tmp/quotaleakA-fix2-01-buffer-sizes-
#: redtree.log (unfixed tree) and tmp/quotaleakA-fix2-05-buffer-sizes-green.log.
#:
#: A caller buffer smaller than this is an out-of-bounds access: on the heap it
#: silently corrupts whatever follows, behind a guard page it fails EFAULT.
KERNEL_COPY_BYTES = {
    q._Q_XGETQSTATV: 160,     # fs_quota_statv
    q._Q_XGETQUOTA: 112,      # fs_disk_quota
    q._Q_XGETNEXTQUOTA: 112,  # fs_disk_quota
    q._Q_XSETQLIM: 112,       # fs_disk_quota (copied *in*)
}
#: ``FS_IOC_FSGETXATTR`` copies ``struct fsxattr`` out (same measurement).
KERNEL_COPY_BYTES_IOCTL = 28


class FakeKernel:
    """Scripted stand-in for ``quotactl_fd`` (the only kernel seam)."""

    def __init__(self, *, entries=None, quota=None, flags=0x0030):
        self.entries = list(entries or [])          # (projid, hard, soft, used) 512B blocks
        self.quota = dict(quota or {})              # projid -> (hard, soft, used)
        self.flags = flags
        self.setqlim: list[tuple[int, int, int]] = []
        self.calls: list[int] = []
        self.buffer_bytes: dict[int, int] = {}

    def __call__(self, fd, cmd, qid, buf):
        self.calls.append(cmd)
        self.buffer_bytes[cmd] = len(buf)
        # The kernel copies the whole struct in/out with copy_from_user/
        # copy_to_user. Emulate that, and the guard page's verdict on a buffer
        # too small for it, so a stale caller-side constant cannot hide behind
        # a roomy heap (C1: 104 bytes handed to a kernel that copies 160).
        if len(buf) < KERNEL_COPY_BYTES[cmd]:
            return -1, errno.EFAULT
        if cmd == q._Q_XGETQSTATV:
            struct.pack_into("<b", buf, 0, q._FS_QSTATV_VERSION1)
            struct.pack_into("<H", buf, 2, self.flags)
            return 0, 0
        if cmd == q._Q_XSETQLIM:
            fieldmask = struct.unpack_from("<h", buf.raw, 2)[0]
            projid = struct.unpack_from("<I", buf.raw, 4)[0]
            hard = struct.unpack_from("<Q", buf.raw, 8)[0]
            soft = struct.unpack_from("<Q", buf.raw, 16)[0]
            self.setqlim.append((projid, hard, soft))
            self.quota[projid] = (hard, soft, self.quota.get(projid, (0, 0, 0))[2])
            assert fieldmask & q._FS_DQ_BHARD, "BHARD must be in the field mask"
            return 0, 0
        if cmd == q._Q_XGETQUOTA:
            if qid not in self.quota:
                return -1, errno.ENOENT
            hard, soft, used = self.quota[qid]
            _fill_disk_quota(buf, qid, hard, soft, used)
            return 0, 0
        if cmd == q._Q_XGETNEXTQUOTA:
            for projid, hard, soft, used in sorted(self.entries):
                if projid > qid:
                    _fill_disk_quota(buf, projid, hard, soft, used)
                    return 0, 0
            return -1, errno.ENOENT
        raise AssertionError(f"unexpected cmd {cmd:#x}")


@pytest.fixture()
def fake(monkeypatch):
    kernel = FakeKernel()
    monkeypatch.setattr(q, "_quotactl_fd", kernel)
    monkeypatch.setattr(q._MOUNT_FDS, "get", lambda mount: 42)
    monkeypatch.setattr(q, "_libc", lambda: type("L", (), {"ioctl": staticmethod(lambda *a: 0),
                                                           "syscall": staticmethod(lambda *a: 0)})())
    return kernel


@pytest.fixture(autouse=True)
def _clear_caches():
    q._FACTS_CACHE.clear()
    yield
    q._FACTS_CACHE.clear()


# --- 1. units -----------------------------------------------------------------


def test_mb_to_basic_blocks_is_the_512_byte_unit():
    assert q.mb_to_basic_blocks(1) == 2048
    assert q.mb_to_basic_blocks(4) == 8192
    assert q.mb_to_basic_blocks(0) == 0
    assert q.basic_blocks_to_kib(8192) == 4096      # 4 MiB in 1 KiB blocks


def test_a_4mib_limit_means_4194304_bytes(fake):
    """The probe's 2 GiB mistake: 4 MiB must reach the kernel as 8192 blocks."""
    q.set_limit("/mnt/vol", 10001, 4)

    assert fake.setqlim == [(10001, 8192, 8192)]
    hard_bytes = q.mb_to_basic_blocks(4) * q._BASIC_BLOCK_BYTES
    assert hard_bytes == 4 * 1024 * 1024 == 4194304


# --- 2. ghost filtering -------------------------------------------------------


def test_project_table_filters_ghost_dquots(fake):
    fake.entries = [
        (10001, 8192, 8192, 4096),   # live: 4 MiB limit, 2 MiB used
        (10002, 0, 0, 0),            # ghost left behind by a release
        (10003, 0, 0, 2048),         # no limits, stale usage: also a ghost
    ]

    table = q.project_table("/mnt/vol")

    # Mirrors the operator view (`xfs_quota report -p`), which only lists
    # projects that actually carry a limit.
    assert set(table) == {10001}
    assert table[10001] == (2048, 4096, 4096)  # used, soft, hard in 1 KiB blocks


def test_usage_reports_none_for_an_unknown_project(fake):
    assert q.usage("/mnt/vol", 99999) is None


# --- 2b. the kernel's structures fit the caller's buffers (C1) ---------------


def test_statv_state_gives_the_kernel_the_whole_struct(fake):
    """``Q_XGETQSTATV`` copies 160 bytes; the buffer must cover all of them."""
    assert q.state("/mnt/vol") == {"accounting": True, "enforcement": True}
    assert fake.buffer_bytes[q._Q_XGETQSTATV] >= KERNEL_COPY_BYTES[q._Q_XGETQSTATV]


def test_every_kernel_buffer_covers_the_struct_the_kernel_copies(fake):
    fake.entries = [(10001, 8192, 8192, 4096)]
    fake.quota[10001] = (8192, 8192, 4096)

    q.state("/mnt/vol")
    q.set_limit("/mnt/vol", 10001, 4)
    q.usage("/mnt/vol", 10001)
    q.project_table("/mnt/vol")

    assert fake.buffer_bytes == {
        q._Q_XGETQSTATV: q._STATV_BUFFER_BYTES,
        q._Q_XSETQLIM: q._FS_DISK_QUOTA_SIZE,
        q._Q_XGETQUOTA: q._FS_DISK_QUOTA_SIZE,
        q._Q_XGETNEXTQUOTA: q._FS_DISK_QUOTA_SIZE,
    }
    assert {
        cmd: size >= KERNEL_COPY_BYTES[cmd]
        for cmd, size in fake.buffer_bytes.items()
    } == {
        q._Q_XGETQSTATV: True,
        q._Q_XSETQLIM: True,
        q._Q_XGETQUOTA: True,
        q._Q_XGETNEXTQUOTA: True,
    }


def test_fsxattr_buffer_covers_the_struct_the_kernel_copies(monkeypatch, tmp_path):
    seen: dict[str, int] = {}

    class FakeLibc:
        def ioctl(self, fd, request, buf):
            seen["request"] = request.value
            seen["size"] = len(buf)
            if len(buf) < KERNEL_COPY_BYTES_IOCTL:
                ctypes.set_errno(errno.EFAULT)
                return -1
            ctypes.memmove(buf, struct.pack("<7I", 0x80000000, 0, 0, 8301, 0, 0, 0), 28)
            return 0

    monkeypatch.setattr(q, "_libc", lambda: FakeLibc())

    assert q.projid_of(tmp_path) == 8301
    assert seen == {"request": q._FS_IOC_FSGETXATTR, "size": KERNEL_COPY_BYTES_IOCTL}


def test_declared_layouts_match_the_kernel_structs_and_the_parsed_offsets():
    """The layouts are the single source of the sizes and field offsets."""
    assert q._FS_QUOTA_STATV_LAYOUT.size == KERNEL_COPY_BYTES[q._Q_XGETQSTATV] == 160
    assert q._FS_QUOTA_STATV_KERNEL_BYTES == 160
    assert q._FS_DISK_QUOTA_LAYOUT.size == KERNEL_COPY_BYTES[q._Q_XGETQUOTA] == 112
    assert q._FSXATTR_LAYOUT.size == KERNEL_COPY_BYTES_IOCTL == 28

    statv = q._FS_QUOTA_STATV_LAYOUT.pack(
        q._FS_QSTATV_VERSION1, 0, 0x0030, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    )
    assert struct.unpack_from("<b", statv, 0)[0] == q._FS_QSTATV_VERSION1
    assert struct.unpack_from("<H", statv, 2)[0] == 0x0030

    disk = q._FS_DISK_QUOTA_LAYOUT.pack(
        q._FS_DQUOT_VERSION, 0, 0x0003, 8301,
        8192, 8192, 0, 0, 4096, 0,
        0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, b"\x00" * 8,
    )
    assert struct.unpack_from("<b", disk, 0)[0] == q._FS_DQUOT_VERSION
    assert struct.unpack_from("<h", disk, 2)[0] == 0x0003
    assert struct.unpack_from("<I", disk, 4)[0] == 8301
    assert struct.unpack_from("<Q", disk, 8)[0] == 8192
    assert struct.unpack_from("<Q", disk, 16)[0] == 8192
    assert struct.unpack_from("<Q", disk, 40)[0] == 4096


# --- 2c. the project-id read answers its own question (F3) --------------------


def test_can_read_projid_is_answered_by_the_ioctl_not_by_quota_administration(
    tmp_path,
):
    """F3: ``can_read_projid`` issues its own syscall, quotas or no quotas.

    Both calls are real syscalls on the filesystem that owns ``tmp_path`` --
    nothing is faked -- because the point is that they are *different*
    syscalls: ``available()`` asks ``Q_XGETQSTATV`` whether this mount can
    **administer** project quotas, while the read only needs
    ``FS_IOC_FSGETXATTR`` on the directory. Measured on the production shape
    (a loop XFS mount without ``prjquota``, uid 65534) the two answers come
    out opposite: ``state()`` fails ENOSYS, ``available() is False``, and a
    directory tagged with project id 4242 still reads back
    ``can_read_projid() is True`` / ``projid_of() == 4242`` -- the container
    probe in tmp/w2-06-linux-probe.log is that measurement.
    """
    directory = tmp_path / "sbx_tree"
    directory.mkdir()

    reads_project_ids = q.can_read_projid(directory)
    try:
        administers = q.available(directory)
    except OSError:
        # No ``libc.so.6`` to load (a macOS dev box): the fd backend cannot be
        # used at all. The read probe must report that instead of raising, and
        # the administration probe cannot answer either.
        assert reads_project_ids is False
        with pytest.raises(OSError):
            q.state(directory)
        return

    # The ioctl needs nothing but a readable directory: an untagged directory
    # answers "project id 0" (``directory_project_id`` folds that to None),
    # not an error.
    assert reads_project_ids is True
    assert q.projid_of(directory) == 0
    # ... while quota administration on the mounts these tests run on is not
    # available (no prjquota, or not privileged): the two answers diverge,
    # which is exactly what gating the read on ``available()`` used to hide.
    assert administers is False


# --- 3. errno classification --------------------------------------------------


def test_probe_reports_unsupported_on_einval(monkeypatch, tmp_path):
    def reject(path, projid):
        raise q.QuotactlError("FS_IOC_FSSETXATTR failed: EINVAL")

    monkeypatch.setattr(q, "assign_projid", reject)
    supported, reason = q.projid32bit(tmp_path)
    assert supported is False
    assert "EINVAL" in reason


def test_probe_reports_unknown_on_other_errors(monkeypatch, tmp_path):
    def denied(path, projid):
        raise q.QuotactlError("FS_IOC_FSSETXATTR failed: EACCES")

    monkeypatch.setattr(q, "assign_projid", denied)
    supported, reason = q.projid32bit(tmp_path)
    assert supported is None                      # never claim "unsupported"
    assert "EACCES" in reason


def test_probe_is_cached_and_reverted(monkeypatch, tmp_path):
    calls = {"assign": 0, "clear": 0}

    def assign(path, projid):
        calls["assign"] += 1
        assert projid > 0xFFFF                    # the 32-bit id
        assert path.name.startswith("_quota_probe_")
        return projid

    def clear(path):
        calls["clear"] += 1

    monkeypatch.setattr(q, "assign_projid", assign)
    monkeypatch.setattr(q, "clear_projid", clear)

    assert q.projid32bit(tmp_path)[0] is True
    assert q.projid32bit(tmp_path)[0] is True
    assert calls == {"assign": 1, "clear": 1}     # cached, and reverted once
    assert not list(tmp_path.glob("_quota_probe_*"))


# --- 4. concurrency -----------------------------------------------------------


def test_probe_is_serialized_across_threads(monkeypatch, tmp_path):
    calls = {"assign": 0, "clear": 0}
    started = threading.Event()
    release = threading.Event()

    def assign(path, projid):
        calls["assign"] += 1
        started.set()
        release.wait(5)                           # hold the lock across threads
        return projid

    def clear(path):
        calls["clear"] += 1

    monkeypatch.setattr(q, "assign_projid", assign)
    monkeypatch.setattr(q, "clear_projid", clear)

    results: list[bool | None] = []
    threads = [threading.Thread(target=lambda: results.append(q.projid32bit(tmp_path)[0]))
               for _ in range(8)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(10)

    assert results == [True] * 8
    assert calls["assign"] == 1                   # one probe, no cross-talk
    assert calls["clear"] == 1


# --- 5. the deployment regression: facts without a device ---------------------


def test_local_facts_succeeds_without_the_device(monkeypatch, tmp_path):
    from envd_service import xfs_quota

    monkeypatch.setenv("E2B_XFS_QUOTA_BACKEND", "auto")
    xfs_quota._BACKEND_CACHE.clear()
    # No device: xfs_info fails, and xfs_quota -x cannot reach the mount.
    monkeypatch.setattr(xfs_quota, "_read_proc_mounts", lambda: "fake")
    monkeypatch.setattr(xfs_quota, "_find_mount", lambda text, mount: ("xfs", "rw,prjquota"))
    monkeypatch.setattr(xfs_quota, "_run_xfs_info", lambda mount: None)
    monkeypatch.setattr(q, "available", lambda mount: True)
    monkeypatch.setattr(
        q, "projid32bit", lambda mount: (True, "probe: project id 0x12345 stored verbatim")
    )

    facts = xfs_quota._local_facts(tmp_path)

    assert facts["projid32bit"] is True
    assert facts["xfs_quota"] is True
    assert facts["backend"] == "quotactl"
    assert xfs_quota._evaluate_facts(facts) == (True, "")


def test_local_facts_keeps_the_subprocess_fast_path(monkeypatch, tmp_path):
    from envd_service import xfs_quota

    monkeypatch.setenv("E2B_XFS_QUOTA_BACKEND", "auto")
    xfs_quota._BACKEND_CACHE.clear()
    monkeypatch.setattr(xfs_quota, "_read_proc_mounts", lambda: "fake")
    monkeypatch.setattr(xfs_quota, "_find_mount", lambda text, mount: ("xfs", "rw,prjquota"))
    monkeypatch.setattr(q, "available", lambda mount: False)
    monkeypatch.setattr(
        xfs_quota, "_run_xfs_info", lambda mount: "meta-data=... projid32bit=1 ..."
    )

    facts = xfs_quota._local_facts(tmp_path)

    assert facts["projid32bit"] is True
    assert "backend" not in facts                  # unchanged host/root shape
