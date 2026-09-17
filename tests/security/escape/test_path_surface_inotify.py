"""OBS-2: `inotify_add_watch` must resolve inside the sandbox's virtual root.

The chroot (image-rootfs) shape has no kernel-level root -- the confined
child's kernel root is the host root and the supervisor manufactures the rootfs
view by intercepting path syscalls -- so the fork's confinement rests on the
interception list being complete (`third_party/sandlock/crates/sandlock-core/
src/sys/path_surface.rs`). `inotify_add_watch` takes a path and is covered by
no Landlock access right, so while it was neither mediated nor refused it
resolved against the *host* root: measured, a watch registered on a host
directory from inside the sandbox and then delivered `IN_CREATE`/`IN_MODIFY`
carrying the host file's name. Against a worker's workspace base that is other
tenants' sandbox ids and their file activity.

The fork now mediates it (resolves the path inside the virtual root and
registers the watch on the rootfs-resolved object, on behalf of the child).
These tests are the E2B-side acceptance: the mediated shape must refuse host
paths *and keep working* for paths inside the sandbox, and the pure shape --
which has no mediator at all -- is pinned as a known residual.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
    run_sh,
)

HOST_ONLY_DIR = "/obs2-host-only"

#: Adds a watch, waits, and reports both the syscall outcome and any events.
#: ``__TARGET__`` is the path the sandbox asks to watch.
PROBE = r'''
import ctypes, json, os, struct, time
libc = ctypes.CDLL("libc.so.6", use_errno=True)
out = {}
ifd = libc.inotify_init1(os.O_NONBLOCK)
ctypes.set_errno(0)
wd = libc.inotify_add_watch(ifd, b"__TARGET__", 0x00000002 | 0x00000100)
err = ctypes.get_errno()
out["wd"] = wd
out["errno"] = err
time.sleep(3.5)
buf = ctypes.create_string_buffer(8192)
n = libc.read(ifd, buf, 8192)
events = []
off = 0
while n > 0 and off < n:
    _wd, mask, _cookie, ln = struct.unpack_from("iIII", buf.raw, off)
    events.append([mask, buf.raw[off + 16:off + 16 + ln].split(b"\x00")[0].decode(errors="replace")])
    off += 16 + ln
out["events"] = events
print(json.dumps(out))
'''


def _drive(executor, workspace: Path, target: str, host_activity: Path | None):
    """Run the probe and, meanwhile, optionally touch a file on the host side."""
    (workspace / "probe.py").write_text(PROBE.replace("__TARGET__", target))

    async def _run():
        task = asyncio.create_task(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/probe.py")
        )
        await asyncio.sleep(2.0)
        if host_activity is not None:
            host_activity.write_text("x")
        return await task

    code, out, err = asyncio.run(_run())
    assert code == 0, err
    return json.loads(out.decode())


def test_mediated_shape_resolves_the_watch_inside_the_virtual_root():
    """Host paths are refused; a path inside the sandbox still watches."""
    rootfs = resolve_test_rootfs("python:3.11-slim")
    executor, workspace = route_b_sandbox("python:3.11-slim", rootfs)
    host_only = Path(HOST_ONLY_DIR)
    try:
        require_mediation_capable(executor)

        # (1) A directory that exists only on the host: no watch may be added.
        host_only.mkdir(parents=True, exist_ok=True)
        leaked = _drive(executor, workspace, HOST_ONLY_DIR, host_only / "HOST_FILE")
        assert leaked["wd"] < 0, f"host directory was watchable: {leaked}"
        assert leaked["events"] == [], f"host activity leaked: {leaked}"

        # (2) The sandbox's own workspace must still work -- that is the point
        # of mediating rather than blocklisting (watch-mode tooling).
        inside = _drive(executor, workspace, "/workspace", workspace / "INSIDE_FILE")
        assert inside["wd"] >= 0, f"watching the workspace was refused: {inside}"
        # Creating and then writing the file is one IN_CREATE + one IN_MODIFY,
        # in that order -- the watch really is attached to the sandbox's own
        # workspace through the mount mapping.
        assert inside["events"] == [[256, "INSIDE_FILE"], [2, "INSIDE_FILE"]], inside
    finally:
        executor.close()
        (host_only / "HOST_FILE").unlink(missing_ok=True)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known residual: the pure (no-chroot) shape has no path mediation at all "
        "-- Landlock is its only barrier and it has no access right for inotify -- "
        "so the watch still resolves against the host root there"
    ),
)
def test_pure_shape_inotify_still_reaches_the_host_root():
    """Pin the residual: the pure shape is unmediated, so this must fail."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    from tests.security.conftest import sandbox_tmpdir

    host_only = Path(HOST_ONLY_DIR)
    host_only.mkdir(parents=True, exist_ok=True)
    workspace = Path(sandbox_tmpdir(suffix="-obs2-pure"))
    executor = SandlockExecutor(
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
    try:
        leaked = _drive(executor, workspace, HOST_ONLY_DIR, host_only / "HOST_FILE")
    finally:
        executor.close()
        (host_only / "HOST_FILE").unlink(missing_ok=True)
    assert leaked["events"] == [], f"host activity leaked into the sandbox: {leaked}"
