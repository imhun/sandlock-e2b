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
import os
import time
from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)

HOST_ONLY_DIR = "/obs2-host-only"

#: Where the probe and the host hand each other the baton, as a *child* of the
#: workspace: inotify is not recursive, so a create inside it cannot turn up as
#: an event on a watch the test registers on the workspace itself. Timing the
#: host side with a sleep instead races the sandbox's own start-up -- on an
#: emulated lane the watch lands seconds into the host's timeline, and the file
#: the watch is meant to report is already there by then (measured 2026-09-24
#: on the aarch64 lane: the host's write at t=2.0s was already visible to the
#: sandbox at its own t=18ms, i.e. before the watch existed, so no event was
#: ever queued and the probe read back `events == []`).
HANDSHAKE = "handshake"
READY = "READY"
DONE = "DONE"

#: Adds a watch, announces it through the handshake, and reports both the
#: syscall outcome and every event the watch delivered afterwards.
#: ``__TARGET__`` is the path the sandbox asks to watch, ``__HANDSHAKE__`` the
#: directory the two sides hand off in.
PROBE = r'''
import ctypes, json, os, struct, time
libc = ctypes.CDLL("libc.so.6", use_errno=True)
out = {}
ifd = libc.inotify_init1(os.O_NONBLOCK)
ctypes.set_errno(0)
wd = libc.inotify_add_watch(ifd, b"__TARGET__", 0x00000002 | 0x00000100)
out["wd"] = wd
out["errno"] = ctypes.get_errno()

# The watch is up -- say so, so the host only then touches the file this test
# is about. The sandbox proves the registration itself; the host cannot.
open("/workspace/__HANDSHAKE__/__READY__", "w").write("x")

buf = ctypes.create_string_buffer(8192)
events = []
deadline = time.monotonic() + 120.0
while not os.path.exists("/workspace/__HANDSHAKE__/__DONE__"):
    if time.monotonic() > deadline:
        out["timeout"] = True
        break
    time.sleep(0.05)
# The host touched its file before DONE, so the events are already queued; the
# nudge covers the scheduler, not the ordering.
time.sleep(0.5)
while True:
    n = libc.read(ifd, buf, 8192)
    if n <= 0:
        break
    off = 0
    while off < n:
        _wd, mask, _cookie, ln = struct.unpack_from("iIII", buf.raw, off)
        events.append([mask, buf.raw[off + 16:off + 16 + ln].split(b"\x00")[0].decode(errors="replace")])
        off += 16 + ln
out["events"] = events
print(json.dumps(out))
'''


def _drive(executor, workspace: Path, target: str, host_activity: Path | None):
    """Run the probe, driving the host's activity off the sandbox's handshake.

    The probe registers the watch and then writes ``handshake/READY``; the host
    waits for *that* file before touching the file the watch is supposed to
    report, and writes ``handshake/DONE`` to end the probe. See ``HANDSHAKE``.
    """
    handshake = workspace / HANDSHAKE
    handshake.mkdir(exist_ok=True)
    # The probe creates the first handshake file, so the directory needs the
    # workspace's own owner and mode, not the test runner's.
    workspace_stat = workspace.stat()
    if os.geteuid() == 0:
        os.chown(handshake, workspace_stat.st_uid, workspace_stat.st_gid)
    os.chmod(handshake, workspace_stat.st_mode & 0o7777)
    ready, done = handshake / READY, handshake / DONE
    ready.unlink(missing_ok=True)
    done.unlink(missing_ok=True)
    (workspace / "probe.py").write_text(
        PROBE.replace("__TARGET__", target)
        .replace("__HANDSHAKE__", HANDSHAKE)
        .replace("__READY__", READY)
        .replace("__DONE__", DONE)
    )

    async def _run():
        task = asyncio.create_task(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/probe.py")
        )
        # Race the probe against its own handshake: if it dies before
        # announcing the watch, sitting out the full `_await_path` timeout
        # would hide the one thing that explains the failure.
        announced = asyncio.create_task(
            _await_path(ready, what="the sandbox to register its watch")
        )
        finished, _ = await asyncio.wait(
            {task, announced}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in finished:
            code, out, err = task.result()
            raise AssertionError(
                f"the probe exited (code {code}) before announcing its watch: "
                f"stderr={err!r} stdout={out!r}"
            )
        announced.result()
        if host_activity is not None:
            host_activity.write_text("x")
        done.write_text("x")
        return await asyncio.wait_for(task, timeout=300)

    code, out, err = asyncio.run(_run())
    assert code == 0, err
    result = json.loads(out.decode())
    assert "timeout" not in result, f"the probe never saw the handshake: {result}"
    return result


async def _await_path(path: Path, *, what: str, timeout: float = 120.0) -> None:
    """Wait for ``path``, failing loudly instead of hanging the lane."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {what}: {path}"
        await asyncio.sleep(0.05)


def test_mediated_shape_resolves_the_watch_inside_the_virtual_root():
    """Host paths are refused; a path inside the sandbox still watches."""
    rootfs = resolve_test_rootfs("python:3.11-slim")
    executor, workspace = own_identity_sandbox("python:3.11-slim", rootfs)
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


def test_the_pure_shape_is_mediated_too_and_the_watch_stays_inside():
    """N15's acceptance: the pure (no-rootfs) shape answers this the same way.

    This case used to be `xfail(strict=True)` -- "known residual: the pure
    (no-chroot) shape has no path mediation at all, so the watch still resolves
    against the host root" -- and it is the measurement the route was chosen
    from (`docs/pure-shape-decision.md` §5): with the *host root* as the
    mediator's root, virtual path == host path (identity translation), the
    existing handlers apply unchanged and the assertions below hold without a
    second gate written for this shape.

    Both halves matter: the host directory must be refused (that is the leak),
    and the sandbox's own workspace must still be watchable (that is why the
    answer is mediation rather than blocklisting `inotify_add_watch`).
    """
    host_only = Path(HOST_ONLY_DIR)
    host_only.mkdir(parents=True, exist_ok=True)
    executor, workspace = own_identity_sandbox(None, None)
    try:
        require_mediation_capable(executor)
        leaked = _drive(executor, workspace, HOST_ONLY_DIR, host_only / "HOST_FILE")
        assert leaked["wd"] < 0, f"host directory was watchable: {leaked}"
        assert leaked["events"] == [], f"host activity leaked into the sandbox: {leaked}"

        inside = _drive(executor, workspace, "/workspace", workspace / "INSIDE_FILE")
        assert inside["wd"] >= 0, f"watching the workspace was refused: {inside}"
        assert inside["events"] == [[256, "INSIDE_FILE"], [2, "INSIDE_FILE"]], inside
    finally:
        executor.close()
        (host_only / "HOST_FILE").unlink(missing_ok=True)
