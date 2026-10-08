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
is structural rather than stylistic: under own identity the `Sandbox` belongs to the
`sandlock-supervise` slot, so the worker drives a checkpoint through the slot's
verb. An in-process call from envd would have no live sandbox to act on.

**The architecture precondition is met** -- it was not when this scan was written.
The engine now covers x86_64, aarch64 and riscv64: `restore_interactive` accepts
all three, `restore-stub.c` has a `__aarch64__` branch, and `build.rs` made a
missing stub *fatal* on those targets instead of a warning. So the aarch64 fleet
the product actually runs on can host the feature.

E2B's half is built now (S2/S3/S4 in `docs/checkpoint-restore-e2b-half.md`):
the images live in the platform's own runtime dir and account, `pause` writes
one and `resume` thaws-or-resumes, and `restore_skipped` is reported rather than
swallowed. This scan is what keeps the *shape* of that half from drifting back:
every path goes through the slot's verbs, and the worker never calls the fork's
bindings in process.

Text scan on purpose: it is the call sites, not the behaviour, that must stay
absent, and a grep-shaped assertion is what makes "we re-checked" durable.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Call sites, not vocabulary -- and each needle is the *binding* shape, i.e. an
#: attribute access on a sandbox/instance object.
#:
#: ``.checkpoint(`` and ``.restore_skipped(`` are methods on the wheel's
#: ``Sandbox``/``SandboxInstance`` (``sandlock-core/src/instance.rs``), and
#: ``restore_interactive`` is the engine's other restore entry point. The bare
#: words, spelled without the dot, are deliberately **not** matched: the E2B half
#: has its own vocabulary now -- ``checkpoint``/``restore`` are the *slot verbs*
#: it is supposed to call, ``restore_skipped`` is the wire key of the restore
#: reply it is supposed to read (``own_identity.OwnIdentityInstance``), and the uid-pool
#: lifecycle legitimately says "restored sandboxes". Matching those would make
#: the guard fire on exactly the shape it exists to require.
FORBIDDEN = (".checkpoint(", "restore_interactive", ".restore_skipped(")


def _worker_sources(repo: Path) -> list[Path]:
    """Every ``envd_service/**/*.py`` under *repo* -- the scan's whole input."""
    return sorted((repo / "envd_service").rglob("*.py"))


def _offenders(repo: Path) -> list[str]:
    """``relative/path.py: needle`` for each forbidden call site, in file order."""
    offenders: list[str] = []
    for path in _worker_sources(repo):
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(repo)}: {needle}")
    return offenders


def test_the_scan_actually_reads_the_worker_tree() -> None:
    """The guard has to *look at something* before it can be green about it.

    ``offenders == []`` is also what a scan of nothing returns, so a renamed
    ``envd_service/`` (or a package that stopped shipping ``.py`` files) would
    leave this suite green while the call sites it exists to forbid went
    unread. The count is asserted rather than a file list because the tree
    legitimately grows.
    """
    assert _worker_sources(REPO) != [], (
        "the scan found no `envd_service/**/*.py`; the guard is no longer "
        "reading the worker tree, so its emptiness proves nothing"
    )


def test_the_scan_flags_a_call_site_in_a_synthetic_tree(tmp_path: Path) -> None:
    """The other half: a call site is an *offender*, not merely "not found".

    Monkeypatching the real ``REPO`` to show this would race the other
    workstreams sharing this worktree, so the tree is built here and matched by
    the same :func:`_offenders` the real scan uses.
    """
    package = tmp_path / "envd_service"
    package.mkdir()
    (package / "bad.py").write_text(
        "def f(sandbox):\n    return sandbox.checkpoint(dir)\n", encoding="utf-8"
    )
    assert _offenders(tmp_path) == ["envd_service/bad.py: .checkpoint("]


def test_no_worker_source_calls_the_fork_checkpoint_restore_api() -> None:
    offenders = _offenders(REPO)
    assert offenders == [], (
        "envd reached into the fork's checkpoint/restore bindings directly. Under "
        "own identity the `Sandbox` lives in the slot, so a checkpoint or a restore has "
        "to go through the slot's verbs (`checkpoint` / `restore`) -- an in-process "
        "call has no live sandbox to act on. If this fired for a real reason, the "
        "shape in docs/checkpoint-restore-e2b-half.md §(g) was bypassed: "
        + ", ".join(offenders)
    )
