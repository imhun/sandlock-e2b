"""The checkpoint images' account: whose disk they spend, and what a refusal says.

These pin the decision in ``docs/checkpoint-restore-e2b-half.md`` §2 D3, because
both of the obvious alternatives are wrong in ways that are invisible until a
fleet hits them:

* not accounting for ``_runtime`` at all makes a checkpoint a way to grow a
  footprint nobody bills (the directory is a *sibling* of the tree the per-sandbox
  quota measures);
* billing it to the sandbox's ``diskMB`` turns "pause" into "you may not write any
  more": the write side of that budget is enforced with ``RLIMIT_FSIZE=0`` and
  ``ENOSPC`` on creates, so a paused sandbox would be pushed over by the very
  action that was supposed to free the node, while none of its own files changed.

So the images are billed to the platform, measured with the same accounting the
per-sandbox ledger uses, and an image that does not fit is refused -- falling back
to freezing in place rather than quietly spending a user's space.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.priv_helpers import dir_size
from envd_service.runtime.platform_disk import (
    checkpoint_admission,
    measure_platform_disk_bytes,
)
from gateway_common.paths import sandbox_checkpoint_dir, sandbox_runtime_dir


def _sandbox_tree(base: Path, sandbox_id: str) -> None:
    """The tree the per-sandbox quota measures (``<base>/<id>``)."""
    (base / sandbox_id).mkdir(parents=True)
    (base / sandbox_id / "work.txt").write_bytes(b"w" * 4096)


def _checkpoint_image(base: Path, sandbox_id: str, name: str = "gen0") -> Path:
    image = sandbox_checkpoint_dir(base, sandbox_id) / name
    image.mkdir(parents=True)
    (image / "memory.bin").write_bytes(b"m" * 8192)
    return image


def test_an_image_is_billed_to_the_platform_and_not_to_the_sandbox(tmp_path: Path) -> None:
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_a")
    sandbox_before = dir_size(base / "sbx_a")
    platform_before = measure_platform_disk_bytes(base)

    _checkpoint_image(base, "sbx_a")

    assert dir_size(base / "sbx_a") == sandbox_before, (
        "a checkpoint image lives in the runtime dir, so the sandbox's own number "
        "must not move -- pause is not allowed to spend the user's budget"
    )
    assert measure_platform_disk_bytes(base) > platform_before, (
        "and the platform's number must see it, or the image is a footprint nobody bills"
    )


def test_the_platform_account_uses_the_ledgers_own_arithmetic(tmp_path: Path) -> None:
    """Same accounting as the per-sandbox ledger, byte for byte.

    What matters here is not "directories cost N bytes" -- that is the filesystem's
    answer, and ``test_dir_ledger`` already pins it for the ledger. It is that the
    platform's number is *the same quantity* as the user's: a fleet decides budgets
    by comparing them, and two different definitions of "usage" would make that
    comparison meaningless. Measured on the cluster's NAS (N31) the shared
    arithmetic includes directory blocks; on APFS it may not, and either way the
    two numbers must agree with each other.
    """
    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_a")
    image = sandbox_checkpoint_dir(base, "sbx_a") / "gen0"
    for sub in ("process", "process/memory", "process/threads"):
        (image / sub).mkdir(parents=True, exist_ok=True)
    (image / "process" / "memory" / "0.bin").write_bytes(b"\x01" * 4096)

    assert measure_platform_disk_bytes(base) == dir_size(base / "_runtime"), (
        "the platform account must be the ledger's arithmetic over the runtime dir"
    )


def test_an_absent_runtime_dir_measures_zero(tmp_path: Path) -> None:
    """A worker that has never checkpointed must not report a bogus number."""
    assert measure_platform_disk_bytes(tmp_path / "nothing-here") == 0


def test_admission_is_unlimited_until_a_fleet_opts_in(tmp_path: Path) -> None:
    allowed, reason = checkpoint_admission(used_bytes=10**12, incoming_bytes=10**12)
    assert allowed is True, (
        "E2B_PLATFORM_DISK_MB defaults to 0 = unlimited: nothing changes until a "
        f"deployment decides; got {reason!r}"
    )
    assert reason == ""


def test_admission_allows_exactly_the_budget_and_refuses_past_it() -> None:
    limit = 100 * 1024 * 1024
    allowed, reason = checkpoint_admission(
        used_bytes=60 * 1024 * 1024, incoming_bytes=40 * 1024 * 1024, limit_bytes=limit
    )
    assert (allowed, reason) == (True, ""), "exactly at the budget still fits"

    allowed, reason = checkpoint_admission(
        used_bytes=60 * 1024 * 1024, incoming_bytes=40 * 1024 * 1024 + 1, limit_bytes=limit
    )
    assert allowed is False
    # A refusal has to be sayable: the caller keeps the sandbox running and has to
    # explain why the checkpoint did not happen.
    assert reason == (
        "checkpoint of 40 MiB would put the platform's checkpoint account at 100 MiB, "
        "over its 100 MiB budget (60 MiB already held); the sandbox is left paused in "
        "place instead of spending the user's disk"
    )

def test_the_image_path_is_platform_state_the_sandbox_tree_never_contains(tmp_path: Path) -> None:
    """Where the image is, stated as a property rather than a string.

    The sandbox's own tree is ``<base>/<id>``; the image is under
    ``<base>/_runtime/<id>``. That is the same split the record and the command log
    already live under -- platform state the sandbox cannot reach -- and it is what
    makes "the image holds the sandbox's whole process" acceptable.
    """
    base = tmp_path / "sandboxes"
    sandbox_tree = base / "sbx_a"
    image = sandbox_checkpoint_dir(base, "sbx_a")
    assert sandbox_tree not in image.parents, (
        f"the image must not sit inside the sandbox's tree ({sandbox_tree}), got {image}"
    )
    assert image.is_relative_to(base / "_runtime"), (
        "the image belongs to the platform's runtime dir"
    )


def test_teardown_takes_the_images_too_and_they_are_not_under_the_runtime_dir(
    tmp_path: Path,
) -> None:
    """The teardown's two calls, and why it needs two.

    Stated here because the checkpoint images are the largest thing the platform
    holds for a sandbox: if the teardown ever narrowed to "the record and the
    log", images would silently outlive the sandboxes they belong to, which is
    exactly the leak shape the platform/workspace split was built to avoid.

    They live *beside* ``_runtime/<id>`` rather than inside it (the sandbox's own
    slot is what writes them, so they cannot sit under a worker-owned ``0700``
    dir), which is why the teardown has to remove both -- and why this asserts
    the pair instead of the old "one rmtree is enough" shape.
    """
    import shutil

    from envd_service.runtime.checkpoint_store import remove_checkpoint_images

    base = tmp_path / "sandboxes"
    _sandbox_tree(base, "sbx_a")
    _checkpoint_image(base, "sbx_a")

    # The teardown's first call, verbatim (``agent._delete_sandbox_runtime``).
    shutil.rmtree(sandbox_runtime_dir(base, "sbx_a"), ignore_errors=True)
    assert sandbox_checkpoint_dir(base, "sbx_a").exists(), (
        "the images are not under the runtime dir -- if this ever passes without "
        "the second call below, the two hierarchies have been merged again"
    )

    # ...and its second.
    assert remove_checkpoint_images(base, "sbx_a") is True
    assert not sandbox_checkpoint_dir(base, "sbx_a").exists(), "the images must be gone"
    assert remove_checkpoint_images(base, "sbx_a") is False, "idempotent"
