"""The worker must not reach for the fork's checkpoint/restore -- yet.

Measured 2026-09-23 (fork commit `43cc62a`): `Sandbox::restore_interactive`
execs its restore stub by its *host* path, and any chroot root (emulated or
real) resolves the workload's paths inside the rootfs, so the stub cannot run
there. The fork now refuses such a call immediately and says so; its own
integration test `test_restore_resumes_inside_a_real_root` pins both root shapes
failing that way, and the fork's OCI test already recorded "restore of a
chrooted checkpoint" as a separate limitation.

E2B's production shape *is* a chroot root (`E2B_BASE_IMAGE=python-mcp:3.14`
⇒ image-rootfs ⇒ `chroot`/`real_root`), so a "resume this sandbox" feature built
on this API would fail on the first real node. Nothing in the worker calls it
today; this test is the reminder that the gap has to be closed first (carry the
stub into the root with a policy mount, or restore without a root) -- and that
`docs/chroot-workspace-exec.md` §9.7.9 is the place to record it.

Text scan on purpose: it is the call sites, not the behaviour, that must stay
absent, and a grep-shaped assertion is what makes "we re-checked" durable.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The fork's checkpoint/restore API surface (Rust symbols and their Python
#: bindings in `sandlock._sdk`). Deliberately not the bare word "restore": the
#: lifecycle code legitimately says "restored sandboxes" about the uid pool.
FORBIDDEN = ("checkpoint", "restore_interactive", "restore_skipped")


def test_no_worker_source_calls_the_fork_checkpoint_restore_api() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "envd_service").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO)}: {needle}")
    assert offenders == [], (
        "envd started using the fork's checkpoint/restore; re-verify it against "
        "the real root first (the restore stub is a host path and any chroot root "
        "refuses it -- docs/chroot-workspace-exec.md §9.7.9): "
        + ", ".join(offenders)
    )
