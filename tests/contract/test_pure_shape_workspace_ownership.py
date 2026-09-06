"""FUP #6 regression contract: pure-sandlock workspace ownership.

The pure shape (``E2B_BASE_IMAGE`` empty) runs commands directly with the
host RunAs identity — no chroot, and therefore no supervisor mediation tier
to create files on the sandbox's behalf. A root worker therefore has to
chown the root-created workspace to the sandbox's shared host uid (legacy
uid 1000), otherwise the first shell write to the workspace root is EACCES
(the gate-B migration trio; evidence ``tmp/m4-bisect-t1-pure.log``).

This contract locks the behavior on a real sandlock worker: a sandbox shell
can write its workspace root, the write survives migration to another
worker, the target shell can keep writing, and the exported workspace on the
target is owned by the shared RunAs uid — not root and not a world-writable
permission change.

Skipped outside the Linux sandlock runner and whenever a base image is
configured (the image-rootfs/chroot shape uses supervisor mediation and is
covered by gate A).
"""

from __future__ import annotations

import io
import os
import tarfile

import httpx
import pytest

from e2b import Sandbox
from tests.security.conftest import sandlock_ready

#: Legacy shared RunAs uid for a root worker without per-sandbox uids
#: (mirrors ``envd_service.uid_pool.LEGACY_SHARED_UID`` / the executor's
#: ``_run_as_identity``; asserted here against the exported archive).
SHARED_RUNAS_UID = 1000

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "pure-shape workspace ownership contract needs Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)

_NO_BASE_IMAGE = pytest.mark.skipif(
    bool(os.environ.get("E2B_BASE_IMAGE")),
    reason=(
        "pure-sandlock contract requires an empty E2B_BASE_IMAGE "
        "(gate B shape)"
    ),
)


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


async def _route(harness, sandbox_id) -> dict:
    async with httpx.AsyncClient(base_url=harness["api_url"]) as client:
        resp = await client.get(
            f"/internal/routes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )
    assert resp.status_code == 200
    return resp.json()


async def _migrate(harness, sandbox_id) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=harness["api_url"], timeout=60
    ) as client:
        return await client.post(
            f"/sandboxes/{sandbox_id}/migrate",
            headers={"X-API-Key": "local-key"},
            json={},
        )


@_NO_BASE_IMAGE
async def test_pure_shape_shell_writes_workspace_root_and_migration_preserves_it(
    multinode_two_workers,
) -> None:
    """A sandbox shell can write its workspace root, and migrating to another
    worker preserves both the content and the writable ownership."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(**_opts(harness))
    sandbox_id = sandbox.sandbox_id
    try:
        before = await _route(harness, sandbox_id)
        source_address = before["address"]
        assert source_address in harness["worker_urls"]
        target_address = next(
            url for url in harness["worker_urls"] if url != source_address
        )

        # FUP #6 symptom: the sandbox shell must be able to write its own
        # workspace root on the creating node.
        assert (
            sandbox.commands.run("echo g2-pre > g2-marker.txt").exit_code == 0
        )

        migrated = await _migrate(harness, sandbox_id)
        assert migrated.status_code == 200
        after = await _route(harness, sandbox_id)
        assert after["address"] == target_address
        assert after["nodeID"] != before["nodeID"]

        # Content survives the archive transfer, and the target shell can
        # still write and overwrite files at the workspace root.
        assert sandbox.commands.run("cat g2-marker.txt").stdout == "g2-pre\n"
        assert (
            sandbox.commands.run("echo g2-post > g2-marker.txt").exit_code == 0
        )
        assert sandbox.commands.run("cat g2-marker.txt").stdout == "g2-post\n"

        # Host side: the exported target workspace is owned by the shared
        # RunAs uid — alignment is chown, not a blanket chmod.
        async with httpx.AsyncClient(
            base_url=target_address, timeout=60
        ) as client:
            exported = await client.get(
                f"/agent/sandboxes/{sandbox_id}/export",
                headers={"X-Internal-Key": "internal-key"},
            )
        assert exported.status_code == 200
        with tarfile.open(
            fileobj=io.BytesIO(exported.content), mode="r:gz"
        ) as tar:
            # ``tar.add(workspace, arcname=".")`` names members "./..." on
            # some tarfile versions; resolve by basename instead of assuming
            # a bare root-relative name.
            by_stripped = {m.name.lstrip("./"): m for m in tar.getmembers()}
            root = by_stripped.get("") or by_stripped.get(".")
            marker = by_stripped["g2-marker.txt"]
        assert root is not None, "export archive has no workspace root member"
        assert root.uid == SHARED_RUNAS_UID
        assert marker.uid == SHARED_RUNAS_UID
    finally:
        sandbox.kill()
