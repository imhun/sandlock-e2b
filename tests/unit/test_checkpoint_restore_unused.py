"""The worker must not reach for the fork's checkpoint/restore on its own.

The engine side is now ready (fork `a6f6b04`, 2026-09-24): the restore stub is
delivered by descriptor (`execveat(AT_EMPTY_PATH)`) and the ruleset grants that
one host file `EXECUTE|READ_FILE`, so restore resumes inside an emulated chroot
*and* inside a real root -- `test_restore_resumes_inside_a_chroot_root` runs both
and asserts the counter advances with a clean fd table. That replaces the earlier
state, where a chroot root could not host the stub at all (§9.7.9).

What is still missing is E2B's half of the feature, and it is the reason this
scan stays: image storage and ownership (the image carries the policy and the
process memory), quota and cleanup, the pause/resume lifecycle, and a decision
about `restore_skipped` -- sockets, pipes and memfds do not come back, so a
sandbox's connections do not either. Nothing in the worker calls the API today;
if that changes, this test fails and points at the checklist in
`docs/chroot-workspace-exec.md` §9.7.9 / §11.6.

**The architecture precondition is met** -- it was not when this scan was written.
The engine now covers x86_64, aarch64 and riscv64: `restore_interactive` accepts
all three, `restore-stub.c` has a `__aarch64__` branch, and `build.rs` made a
missing stub *fatal* on those targets instead of a warning. So the aarch64 fleet
the product actually runs on can host the feature.

What still blocks it is E2B's half, and the design now exists:
`docs/checkpoint-restore-e2b-half.md`. It also records the one piece that turns
out not to be E2B-side -- under route B the `Sandbox` lives inside the slot
process, so a `checkpoint`/`restore` *verb* has to exist before any worker-side
code can call anything (the `update_network` verb went the same way).

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
        "envd started using the fork's checkpoint/restore: the engine works under "
        "a chroot/real root now, but the E2B half is not designed yet (image "
        "storage/ownership/quota, pause/resume lifecycle, and what a sandbox does "
        "when its sockets come back as `restore_skipped`) -- see "
        "docs/checkpoint-restore-e2b-half.md (design) and "
        "docs/chroot-workspace-exec.md §9.7.9 / §11.6 (engine): "
        + ", ".join(offenders)
    )
