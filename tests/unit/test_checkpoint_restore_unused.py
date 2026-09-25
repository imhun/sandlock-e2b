"""The worker must not *call* the fork's checkpoint/restore API.

The engine side is now ready (fork `a6f6b04`, 2026-09-24): the restore stub is
delivered by descriptor (`execveat(AT_EMPTY_PATH)`) and the ruleset grants that
one host file `EXECUTE|READ_FILE`, so restore resumes inside an emulated chroot
*and* inside a real root -- `test_restore_resumes_inside_a_chroot_root` runs both
and asserts the counter advances with a clean fd table. That replaces the earlier
state, where a chroot root could not host the stub at all (§9.7.9).

E2B's half of the feature is now designed and being built
(`docs/checkpoint-restore-e2b-half.md`), which is why this scan changed what it
matches: it used to flag the **word** "checkpoint" anywhere under `envd_service`,
because nothing there had any business mentioning it. The E2B side now has its own
vocabulary for these images -- storage, the platform account, the pause/resume
lifecycle -- so the word is prose and the scan is about **call sites**.

What must still never appear is a direct call into those bindings, and the reason
is structural rather than stylistic: under route B the `Sandbox` belongs to the
`sandlock-supervise` slot, so the worker drives a checkpoint through the slot's
verb. An in-process call from envd would have no live sandbox to act on.

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

#: Call sites, not vocabulary. `.checkpoint(` is the binding's shape (a call on a
#: sandbox object); the other two are the engine-side names a caller would have to
#: touch to do any of this in-process. The bare words "checkpoint" and "restore"
#: are deliberately absent: the E2B half legitimately talks about images, and the
#: uid-pool lifecycle legitimately says "restored sandboxes".
FORBIDDEN = (".checkpoint(", "restore_interactive", "restore_skipped")


def test_no_worker_source_calls_the_fork_checkpoint_restore_api() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "envd_service").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO)}: {needle}")
    assert offenders == [], (
        "envd reached into the fork's checkpoint/restore bindings directly. Under "
        "route B the `Sandbox` lives in the slot, so a checkpoint or a restore has "
        "to go through the slot's verbs (`checkpoint` / `restore`) -- an in-process "
        "call has no live sandbox to act on. If this fired for a real reason, the "
        "shape in docs/checkpoint-restore-e2b-half.md §(g) was bypassed: "
        + ", ".join(offenders)
    )
