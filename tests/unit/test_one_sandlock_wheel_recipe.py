"""The sandlock cross-compile recipe exists in exactly one place: the fork.

History, because the shape keeps trying to come back. E2B carried its own copy
of the fork's wheel builder under ``third_party/sandlock-wheel-builder/``
(``Dockerfile`` + ``cargo-config.toml`` + ``zigcc``). When the pipeline moved
into the fork on 2026-09-09 -- a release wheel has to carry
``sandlock/bin/sandlock-supervise`` and the ``restore-stub``, and the E2B copy
produced neither while still exiting 0 (``deploy/scripts/build-sandlock-wheels.sh``
records that measurement) -- the copy was relabelled SUPERSEDED and kept "as
history".

What a near-copy actually does: ``cargo-config.toml`` and ``zigcc`` were
**byte-identical** to the fork's, so the only thing the leftover could still do
was send a reader to the file that must not run. It was deleted 2026-09-30.

Two ways the duplication could return, both pinned here:

* a second ``cargo-config.toml`` / ``zigcc`` / builder Dockerfile is added to
  the parent repo (the parent's ``git ls-files`` sees exactly its own files, so
  the fork's copies -- a gitlink from the parent's point of view -- are not
  candidates);
* the E2B wrapper grows its own buildx recipe again instead of delegating to
  the fork's ``python/build-wheels.sh``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
WRAPPER = REPO / "deploy" / "scripts" / "build-sandlock-wheels.sh"

#: Deleted 2026-09-30; kept in the message so the next reader knows what the
#: absence means (and where the live recipe went).
RETIRED_DIR = "third_party/sandlock-wheel-builder"


def _parent_repo_files(*, existing_only: bool = False) -> list[str]:
    """The parent repo's tracked files (a submodule is a gitlink, so the fork's
    own copies never appear here -- that is what makes this a *duplicate* test).

    ``existing_only`` skips paths that are tracked but already gone from the
    worktree: a deletion is a state the pin has to accept before it is staged,
    otherwise the pin fails for the one person doing the right thing.
    """
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    )
    paths = [line for line in listed.stdout.splitlines() if line]
    if existing_only:
        return [p for p in paths if (REPO / p).exists()]
    return paths


def test_the_retired_e2b_side_builder_directory_is_gone() -> None:
    """Absent on disk *and* untracked: a leftover file would be the same trap."""
    assert not (REPO / RETIRED_DIR).exists(), (
        f"{RETIRED_DIR} is back. The live recipe is the fork's "
        "python/wheel-builder/ (see docs/build-test-deploy-pitfalls.md A7)."
    )
    tracked = [
        p for p in _parent_repo_files(existing_only=True) if p.startswith(RETIRED_DIR)
    ]
    assert tracked == [], f"{RETIRED_DIR} is tracked again: {tracked}"


def test_the_parent_repo_holds_no_copy_of_the_cross_compile_assets() -> None:
    """One ``zigcc``, one ``cargo-config.toml`` -- and they live in the fork.

    The fork's own copies are invisible here on purpose: a submodule is a
    gitlink to the parent, so anything ``git ls-files`` reports is a *second*
    copy by construction.
    """
    offenders = sorted(
        path
        for path in _parent_repo_files(existing_only=True)
        if path.endswith("/zigcc") or path.endswith("/cargo-config.toml")
    )
    assert offenders == [], (
        "the parent repo must not carry a copy of the fork's cross-compile "
        f"assets (they live under third_party/sandlock/python/wheel-builder/): {offenders}"
    )


def test_the_e2b_wrapper_delegates_and_names_no_builder_of_its_own() -> None:
    """The E2B entry point stages and delegates; the recipe stays in the fork."""
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'sh "$FORK/python/build-wheels.sh"' in text, (
        "build-sandlock-wheels.sh must run the fork's pipeline "
        "(third_party/sandlock/python/build-wheels.sh)"
    )
    assert "docker buildx build" not in text, (
        "the wrapper grew its own buildx recipe again -- that is the second "
        "code path this pin exists to prevent"
    )
    assert "wheel-builder/Dockerfile" not in text, (
        "the wrapper names a builder Dockerfile; the fork's build-wheels.sh "
        "owns that choice"
    )
