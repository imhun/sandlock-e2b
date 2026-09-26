"""Template-image isolation (Linux + Docker + Landlock ABI >= 6 only).

The chroot (image-rootfs) shape is the only shape where E2B asks the fork for
path mediation -- ``fs_denied`` + chroot -- and mediation now has exactly one
identity: the sandbox's own host uid, which a route-B supervise slot provides.
E2B stopped asking for the fork's supervisor downgrade tier on 2026-09-10 and
fork B3 deleted that field outright (2026-09-11), so a privileged worker
mediating in-process no longer gets a silent, supervisor-owned sandbox (T5):
the fork refuses the create.

That makes *how* these tests build the sandbox part of what is under test --
they drive the production path (pooled per-sandbox uid + ``E2B_ROUTE_B=auto`` +
slot) rather than a hand-built in-process instance -- and the refusal of the
old shape is pinned here too, so the downgrade tier cannot quietly return.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from envd_service.executors.sandlock import SandlockExecutor
from tests.security.conftest import (
    SANDBOX_UID,
    require_mediation_capable,
    resolve_test_rootfs,
    route_b_sandbox,
    run_sh,
)

IMAGE = "python:3.11-slim"


@pytest.mark.usefixtures("require_sandlock")
async def test_image_rootfs_execution():
    """Commands execute against the image rootfs -- the one
    ``resolve_image_rootfs`` produced, not the host's own."""
    image = os.environ.get("E2B_BASE_IMAGE") or os.environ.get("E2B_TEMPLATE_IMAGES")
    if not image:
        pytest.skip("no base image configured for template isolation test")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI required for image rootfs resolution")

    rootfs = resolve_test_rootfs(image)
    # A file that exists only in *this* rootfs: reading it back proves the
    # chroot resolved to the image. The runner is itself a Debian-family
    # container, so an os-release check alone could not tell the two apart.
    marker = rootfs / "template-marker.txt"
    marker.write_text("IN_IMAGE_ROOTFS")
    os.chmod(marker, 0o644)

    executor, workspace = route_b_sandbox(image, rootfs)
    try:
        require_mediation_capable(executor)
        code, out, err = await run_sh(
            executor, str(workspace), "cat /template-marker.txt"
        )
        assert (code, out, err) == (0, b"IN_IMAGE_ROOTFS", b"")

        code, out, err = await run_sh(executor, str(workspace), "cat /etc/os-release")
        assert code == 0, f"exit={code} stderr={err!r}"
        ids = [
            line
            for line in out.decode().splitlines()
            if line.startswith("ID=") or line.startswith("ID_LIKE=")
        ]
        assert ids and ids[0] in ("ID=debian", "ID=ubuntu"), ids
    finally:
        executor.close()


@pytest.mark.usefixtures("require_sandlock")
async def test_image_rootfs_cannot_reach_host_filesystem():
    """The chroot restricts the path space: host paths are not visible, and
    the Landlock "/" rule only covers the image rootfs, not the host root."""
    rootfs = resolve_test_rootfs()
    executor, workspace = route_b_sandbox(IMAGE, rootfs)
    # A directory this test really created on the host: probing a path that
    # never existed would say "hidden" even with no chroot at all.
    host_marker = Path(tempfile.mkdtemp(prefix="e2b-host-marker-"))
    probe = (
        f"if [ -e {host_marker} ]; then echo HOST_VISIBLE; else echo HOST_HIDDEN; fi"
    )
    try:
        require_mediation_capable(executor)
        code, out, err = await run_sh(executor, str(workspace), probe)
        # The host-only random path is not visible inside the chroot: the host
        # filesystem is unreachable even though Landlock "/" covers the image
        # rootfs (/workspace is the sandbox's own mount, so it is not a probe).
        assert (code, out.strip(), err) == (0, b"HOST_HIDDEN", b"")
    finally:
        executor.close()
        shutil.rmtree(host_marker, ignore_errors=True)


@pytest.mark.usefixtures("require_sandlock")
@pytest.mark.skipif(
    os.geteuid() != 0, reason="the refusal is about an euid-0 in-process mediator"
)
async def test_in_process_chroot_is_refused_without_a_slot(caplog):
    """The dropped downgrade tier, pinned.

    With ``E2B_ROUTE_B=off`` a root worker would mediate in-process while the
    sandbox runs as uid 1000 -- exactly the T5 shape. Before 2026-09-10 E2B
    asked the fork to accept it through an explicit downgrade tier (loud, but
    it still leaves supervisor-owned files); now nothing is asked, and fork B3
    deleted the tier entirely, so the create is refused fail-closed.
    Re-introducing such a tier -- or quietly dropping ``fs_denied`` to dodge
    the refusal -- fails here.

    The refusal is pinned from two sides because the fork's FFI cannot carry a
    reason (``sandlock_instance_launch`` returns a null handle, and the SDK
    turns that into a generic ``RuntimeError`` -- SL-12): the create has to
    fail, and E2B's own construction-time disclosure has to say why.
    """
    rootfs = resolve_test_rootfs()
    # The disclosure is warn-once per process and a full-suite run may have
    # already disclosed the shape for another sandbox; reset it to pin this one.
    SandlockExecutor._mediation_shape_disclosed = False
    with caplog.at_level(logging.ERROR, logger="envd_service.executors.sandlock"):
        executor, workspace = route_b_sandbox(IMAGE, rootfs, with_route_b=False)
    try:
        assert executor._route_b_active is False
        assert executor._route_b_decline == "E2B_ROUTE_B=off"
        disclosed = [
            record.getMessage()
            for record in caplog.records
            if "not on a supervise slot" in record.getMessage()
        ]
        assert len(disclosed) == 1, disclosed
        assert (
            "runs in-process, not on a supervise slot (E2B_ROUTE_B=off)"
            in disclosed[0]
        ), disclosed[0]
        with pytest.raises(
            RuntimeError,
            match="sandlock_instance_launch failed|in-process path mediation refused",
        ):
            await run_sh(executor, str(workspace), "true")
    finally:
        executor.close()

    # Control: the same root worker and the same sandbox host uid, but with a
    # slot -- i.e. the mediator *is* that uid. It starts, which is what makes
    # the refusal above evidence about *who* mediates rather than about a
    # runner that cannot create any sandbox at all.
    #
    # This control used to be "the same shape with the mediation removed" (the
    # pure shape, route B off). N15 made that shape mediated too -- the host
    # root as the mediator's root -- so it is refused for exactly the same
    # reason, and the control had to move to the axis that still differs.
    plain, plain_ws = route_b_sandbox(None, None)
    try:
        require_mediation_capable(plain)
        code, out, err = await run_sh(
            plain, str(plain_ws), "id -u; printf x > probe.txt"
        )
        assert (code, out, err) == (0, b"0\n", b"")
        assert (plain_ws / "probe.txt").stat().st_uid == SANDBOX_UID
    finally:
        plain.close()


@pytest.mark.usefixtures("require_sandlock")
@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="the unprivileged worker shape both production manifests ship",
)
async def test_unprivileged_worker_still_mediates_the_chroot():
    """Uid 65534 with no CAP_SETUID: no slot can be leased *and* no pooled uid
    exists, so the sandbox runs as the worker's own identity (E5.1).

    That is the shape `docker-compose.prod.yml` and `deploy/k8s/worker.yaml`
    actually run today, and deleting the supervisor tier must not touch it: the
    fork refuses in-process mediation only when the mediator could remap to a
    *different* non-zero uid. If this starts failing with
    ``sandlock_instance_launch failed``, the tier removal broke the deployment
    rather than a test -- and the disclosure ERROR should not appear either,
    because there is nothing refused to explain.
    """
    rootfs = resolve_test_rootfs()
    marker = rootfs / "template-marker.txt"
    marker.write_text("IN_IMAGE_ROOTFS")
    os.chmod(marker, 0o644)

    executor, workspace = route_b_sandbox(IMAGE, rootfs, host_uid=None)
    try:
        assert executor._route_b_active is False
        assert executor._in_process_mediation_is_refused() is False
        code, out, err = await run_sh(
            executor, str(workspace), "id -u; cat /template-marker.txt"
        )
        assert (code, out, err) == (0, f"{os.geteuid()}\nIN_IMAGE_ROOTFS".encode(), b"")
    finally:
        executor.close()
