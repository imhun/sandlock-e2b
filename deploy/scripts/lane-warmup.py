"""Pay the lane's one-time *page-cache* cost before the suite runs.

What this is *not*: the fix for the "first launch is slow" bug. That turned
out to be a hand-rolled ``clone3`` releasing the GIL around the syscall, so a
child could park forever before exec (see ``envd_service/identity_grant.py``,
fixed 2026-10-11). The earlier reading here -- "184 s cold, 0.079 s after a
plain read of the tree" -- was a coincidence of that bug: 184 s was the pool's
readiness window with a wedged child, and the containers that looked "fixed"
were ones whose child happened to win the race.

What remains true, and is why this step stays: the image rootfs is *resolved*
once per image per container (2.7 s with the local mirror; the resolver caches
in ``E2B_TEST_ROOTFS_DIR``), and reading the 152 MB tree plainly (0.6 s, 240
MB/s) removes any cold-cache page-in from the first launch's path.

That matters twice over: the stall is *per container*, so a lane that starts a
container per phase, per iteration pays it every time; and it lands inside the
first test that needs a sandbox -- a security file's ``require_sandlock``
fixture -- where the pool's readiness deadline turns it into a *whole file*
failing (11 files in one full run, all with a 30 s slot-launch timeout).

This script is best-effort by design: the caller reports a failure and carries
on (a lane that cannot warm up is slower, not broken).
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "/workspace")


def _mark(label: str, since: float) -> float:
    now = time.perf_counter()
    print(f"warmup {label}: {now - since:.2f}s", flush=True)
    return now


def _read_tree(root) -> tuple[int, int]:
    """Read every regular file once, in 1 MB chunks (the warm-up itself)."""
    total = 0
    files = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                files += 1
            except OSError:
                continue
    return total, files


def main() -> int:
    from tests.security.conftest import resolve_test_rootfs

    # Every image the suite resolves and launches from: its own test rootfs and
    # the lane's base image (the one the SDK contracts create sandboxes from).
    images = ["python:3.11-slim"]
    base = (os.environ.get("E2B_BASE_IMAGE") or "").strip()
    if base and base != images[0]:
        images.append(base)

    for image in images:
        t = time.perf_counter()
        try:
            rootfs = resolve_test_rootfs(image)
        except Exception as exc:  # noqa: BLE001 - best effort, the lane goes on
            print(f"warmup {image}: could not resolve ({exc})", flush=True)
            continue
        resolved = _mark(f"resolve {image}", t)
        t = time.perf_counter()
        total, files = _read_tree(rootfs)
        _mark(
            f"read {image}: {files} files, {total / 1e6:.1f} MB",
            t,
        )
        del resolved
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
