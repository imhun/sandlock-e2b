"""Task 1 of the 2026-10-06 rename plan: the new names, the freeze, the words.

Three things are asserted here, and all three are the point of the rename rather
than decorations on it:

* the **frozen** return value of the instance-name rule. The function is
  renamed, but its output is a disk contract the control plane and the worker
  both derive the slot documents' path from (ruling D20) -- so the bytes that
  come out of it may not move with the identifier. The three literals are
  lifted byte-for-byte from the pre-rename implementation (short id passes
  through, an 84-byte id is replaced by ``sbx_<sha256[:16]>``, an id exactly at
  the 64-byte ceiling passes through).
* the backend types are reachable under their new names, so the module move is
  not just a file rename that leaves the symbols behind.
* **the live tree stopped spelling the letters.** ``route A`` / ``route B`` were
  design-review names for two backends ("the path mediator runs in the worker
  process" vs "it runs as the sandbox's own host uid"); the plan replaced them
  with what they are -- ``own_identity``, ``slot``, ``identity_grant`` -- and a
  rename is only finished when the prose says so too. The letters in operator
  messages were part of that: the worker's readiness line, the refusal texts the
  tests match on, and the acceptance scripts that grep the worker's log all moved
  together (see the same day's commits), because leaving half of a rename behind
  is how a name comes back.

Two lists make the last check honest rather than a glob that happened to miss
files:

* :data:`_KEEPS_THE_OLD_NAME` is the *one* live sentence that must keep the old
  name -- a reader who knows this mechanism by its review name has to be able to
  find it -- and it is asserted to still be there, so the pointer cannot rot into
  an unused excuse;
* :data:`_HISTORY` is where the letters belong: the archived evidence and the two
  dated ledgers record what each version said on the day it shipped, so rewriting
  them would be editing the record. They are asserted to still contain the
  letters, so the exemption stays a decision on the record.

The frozen values are spelled differently **on purpose**, and that is what makes
the last check cheap: every value this rename may not touch carries a lowercase
``b`` or an underscore -- ``.route-b``, ``/tmp/sandlock-route-b``, the
``rb-<id>`` leaf, ``E2B_ROUTE_B*`` / ``E2B_SLOT_IDENTITY*`` (the aliases Task 6
still reads), ``route_b.py``, and the rename plan's own filename -- so none of
them can trip a pattern that matches only the words.

``third_party/sandlock`` is deliberately out of scope: it is the fork's own
repository, and its ``e2b-integration.md`` / ``supervise-identity-handoff.md``
are that project's record of the same mechanism.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The letter names, in every spelling live prose used: "route B", "route-B",
#: sentence-initial "Route B", the terse "routeB" of a probe's case label, and
#: the matching "route A" side (the in-process mediator). Deliberately *not*
#: included: `route_b`, `route-b`, `ROUTE_B` -- those are the frozen identifiers,
#: filenames, env keys and disk paths, and the freeze is the reason this pattern
#: can be this blunt.
_LETTERS = re.compile(
    r"route B|route-B|Route B|Route-B|routeB|RouteB|route A|route-A|Route A|RouteA"
)

_REPO = Path(__file__).resolve().parent.parent.parent

#: Live prose: the product code, the deploy assets, the tests, plus the top-level
#: docs (`docs/*.md`, one level, so the archived subdirectories are not pulled in
#: by accident) and the two files a newcomer reads first.
_LIVE_ROOTS = (
    "envd_service",
    "control_plane",
    "c3_agent",
    "gateway_common",
    "quota_agent",
    "autoscaler",
    "deploy",
    "tests",
)
_LIVE_FILES = ("README.md", "spec.md")
_SCANNED_SUFFIXES = frozenset({".py", ".sh", ".md", ".yaml", ".yml", ".c", ".h", ".example"})

#: Where the letters stay. `docs/deploy-clusters.md` and `docs/HANDOFF.md` are
#: dated ledgers -- every section says which version it is about -- and
#: `docs/reports/**`, `docs/superpowers/**`, `docs/security-audit/**` are the
#: archive (the rename plan's 「历史文档不改」).
_HISTORY = (
    "docs/deploy-clusters.md",
    "docs/HANDOFF.md",
)
_HISTORY_TREES = ("docs/reports", "docs/superpowers", "docs/security-audit")

#: The one live sentence allowed to keep the old name, per file.
_KEEPS_THE_OLD_NAME = {
    "docs/isolation-boundaries.md": "旧称 route B（2026-10 按本质改名）",
}

#: This file has to *say* the letters to forbid them -- the pattern, the one
#: allowance and the failure text all spell them -- so it is the one code file
#: outside the scan. (The `tmp/` pin next door has the same shape: it names the
#: paths it forbids and scans `docs/**`, which cannot contain itself.)
_SELF = "tests/unit/test_own_identity_naming.py"


def _live_text_files() -> tuple[Path, ...]:
    files: set[Path] = set()
    for rel in _LIVE_ROOTS:
        files.update(
            path
            for path in (_REPO / rel).rglob("*")
            if path.is_file() and path.suffix in _SCANNED_SUFFIXES
        )
    files.update(
        path
        for path in (_REPO / "docs").glob("*.md")
        if path.relative_to(_REPO).as_posix() not in _HISTORY
    )
    files.update(_REPO / name for name in _LIVE_FILES)
    files.discard(_REPO / _SELF)
    return tuple(sorted(files))

def test_the_own_identity_instance_name_is_frozen() -> None:
    from gateway_common.paths import (
        OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES,
        own_identity_instance_name,
    )

    assert OWN_IDENTITY_INSTANCE_NAME_MAX_BYTES == 64
    assert own_identity_instance_name("sbx_0123456789abcdef") == "sbx_0123456789abcdef"
    # Over-long id -> hash: this branch is part of the rule, not an
    # implementation detail of the worker. A control plane that did not know it
    # would derive the wrong directory for exactly those sandboxes (D20).
    assert own_identity_instance_name("sbx_" + "a" * 80) == "sbx_b926db5e21e80dd2"
    # Exactly at the ceiling passes through unchanged.
    at_limit = "sbx_" + "a" * 60
    assert own_identity_instance_name(at_limit) == at_limit


def test_the_backend_types_are_reachable_under_their_new_names() -> None:
    from envd_service.own_identity import (
        OwnIdentityConfig,
        OwnIdentityExecProcess,
        OwnIdentityInstance,
    )

    assert OwnIdentityConfig(mode="off").mode == "off"
    # The two client classes exist under the new names (their behaviour is the
    # rest of the suite's business, not this guard's).
    assert OwnIdentityExecProcess.__name__ == "OwnIdentityExecProcess"
    assert OwnIdentityInstance.__name__ == "OwnIdentityInstance"


def test_the_live_tree_stopped_spelling_the_letters() -> None:
    """No live file says `route A` / `route B` -- only the frozen spellings."""
    offences: list[str] = []
    for path in _live_text_files():
        rel = path.relative_to(_REPO).as_posix()
        text = path.read_text(encoding="utf-8")
        keep = _KEEPS_THE_OLD_NAME.get(rel)
        if keep is not None:
            # The pointer to the old name is the one exemption, and it has to be
            # alive: an allowance nobody cites is how the weak version comes back.
            assert keep in text, (
                f"{rel} no longer says {keep!r}; if the pointer moved, move it "
                f"here rather than deleting it silently"
            )
            text = text.replace(keep, "")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LETTERS.search(line):
                offences.append(f"{rel}:{lineno}: {line.strip()}")

    assert offences == [], (
        "the letters `route A/B` are review names, not the backend's name: say "
        "`own identity` / `own-identity slot` (values keep their frozen spelling: "
        "`.route-b`, `/tmp/sandlock-route-b`, `rb-<id>`, `E2B_ROUTE_B*`)\n"
        + "\n".join(offences)
    )


def test_the_archive_still_spells_them() -> None:
    """The exemption above is a decision on the record, not a glob accident.

    The dated ledgers and the archive keep the name they shipped with; a rewrite
    there would be editing history rather than renaming code. This is checked so
    that exempting `_HISTORY` cannot quietly turn into "the check found nothing
    because the files are gone".
    """
    for rel in _HISTORY:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert _LETTERS.search(text), f"{rel} lost the letters it shipped with"

    archived = [
        path
        for tree in _HISTORY_TREES
        for path in (_REPO / tree).rglob("*.md")
        if _LETTERS.search(path.read_text(encoding="utf-8"))
    ]
    assert archived, (
        "no archived doc under "
        + ", ".join(_HISTORY_TREES)
        + " mentions the letters any more -- the rename has started rewriting the record"
    )
