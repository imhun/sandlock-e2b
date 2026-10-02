"""One tar extractor, shared by every image that reads a sandbox archive.

The same operation -- "take tar bytes somebody else produced and land them in a
directory" -- is needed by two services that ship as **different images**:

* the control plane, importing a migration archive (``_import_sandbox_archive``);
* the per-node agents: ``c3_agent``'s materialization unpacks a snapshot's
  ``fs.tar`` (Task 2), and ``envd_service``'s degraded create path unpacks the
  same payload when the control plane did not materialize the tree itself.

Until Task 2 the first two call sites each carried their own copy of the guard
(``control_plane/api/sandboxes.py::_extract_sandbox_archive`` and
``envd_service/agent.py::_extract_sandbox_archive``). The agent is a different
image, so "import the control plane's" was never available -- which is exactly
how a path-escape guard ends up written twice and fixed once. It lives here
instead: both images already depend on ``gateway_common``, and each caller
imports *this* function object.

Two rules, and they are the whole point:

* **member filtering** -- a symlink member whose target is absolute is skipped
  (volume mounts are archived as links to paths that only exist on the source
  node; the target is re-created by provisioning), and a member whose *name*
  walks out of the destination is refused;
* **destination containment** -- every member is resolved against the
  destination before it is written, so a link the previous incarnation left in
  the destination is a named refusal rather than a way out of the tree.

Reasons are named (:class:`ArchiveRefusal`) because the callers branch on
them: "the payload is hostile/broken" (502 on a create) is not "the plan is
wrong" (400), and neither is "the destination holds a link" -- which is the
§4.3.1 requirement that the *destination* side has its own name.

The unpack **streams**: members are walked one at a time and never listed into
memory up front (``getmembers()`` builds an index of the whole archive, and a
real snapshot is far larger than the ones this was written against -- the
agent's ``maint`` container was OOM-killed once during Task 1's measurement,
``docs/create-local-first-design.md`` §3.0).
"""

from __future__ import annotations

import os
import tarfile
from pathlib import PurePosixPath
from pathlib import Path

#: A member's *name* walks out of the destination (``..`` or an absolute path).
MEMBER_ESCAPES = "archive-member-escapes"
#: A clean member name that still lands outside: the destination holds a link.
DESTINATION_IS_A_SYMLINK = "destination-is-a-symlink"
#: The extraction started and did not finish (a truncation, an I/O error).
ARCHIVE_IS_CORRUPT = "archive-is-corrupt"
#: The caller's destination is not a directory to unpack into.
PARTIAL_UNPACK = "partial-unpack"

#: ``tarfile.FilterError`` is 3.12+; older interpreters only have ``TarError``.
_FILTER_ERRORS: tuple[type[BaseException], ...] = tuple(
    error
    for error in (getattr(tarfile, "FilterError", None),)
    if error is not None
)


class ArchiveRefusal(Exception):
    """A named, fail-closed refusal from one archive extraction."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _member_target(dest_resolved: Path, dest: Path, name: str) -> Path:
    """Where one member lands, or a named refusal.

    The two escapes are named apart on purpose: a member whose name walks out
    is a broken archive, while a clean name that still resolves outside means
    the destination itself holds a link on the way -- the failure the caller
    has to be able to name (``materialize.py`` refuses a tree whose previous
    incarnation left a link there).
    """
    parts = PurePosixPath(name).parts
    if not name or name.startswith("/") or any(part == ".." for part in parts):
        raise ArchiveRefusal(
            MEMBER_ESCAPES, f"archive member {name!r} escapes the destination"
        )
    target = (dest / name).resolve()
    if not target.is_relative_to(dest_resolved):
        raise ArchiveRefusal(
            DESTINATION_IS_A_SYMLINK,
            f"the destination holds a symbolic link on the way to {name!r}: "
            "refusing to write through it",
        )
    return target


def extract_sandbox_archive(archive_path: Path, dest: Path) -> int:
    """Unpack ``archive_path`` into the existing directory ``dest``.

    Returns the number of members written. Raises :class:`ArchiveRefusal` (and
    never a bare ``tarfile``/``OSError``) so both callers translate one shape.
    """
    destination = Path(dest)
    resolved_dest = destination.resolve()
    if not resolved_dest.is_dir():
        raise ArchiveRefusal(
            PARTIAL_UNPACK, f"the destination {destination} is not a directory"
        )
    written = 0
    try:
        with tarfile.open(Path(archive_path), "r:*") as tar:
            for member in tar:
                if member.issym() and os.path.isabs(member.linkname):
                    # A volume mount from the source node: the path does not
                    # exist here and provisioning re-creates it.
                    continue
                _member_target(resolved_dest, destination, member.name)
                try:
                    tar.extract(member, destination, filter="data")
                except TypeError:  # pragma: no cover - Python < 3.12
                    tar.extract(member, destination)
                written += 1
    except (tarfile.TarError, EOFError) as exc:
        reason = (
            MEMBER_ESCAPES if isinstance(exc, _FILTER_ERRORS) else ARCHIVE_IS_CORRUPT
        )
        raise ArchiveRefusal(reason, f"{archive_path}: {exc}") from exc
    except OSError as exc:
        raise ArchiveRefusal(PARTIAL_UNPACK, f"{archive_path}: {exc}") from exc
    return written
