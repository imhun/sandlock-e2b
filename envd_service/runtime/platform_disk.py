"""The platform's own disk account: what the *deployment* stores per sandbox.

Why this exists as a **separate** account (N35/D3, see
``docs/checkpoint-restore-e2b-half.md`` §1(b)):

* A checkpoint image is the sandbox's **whole process**, and it lives in
  ``<base>/_runtime/<id>/`` -- a sibling of the tree the per-sandbox quota
  measures (``<base>/<id>``). Without an account of its own it would be invisible
  to the quota, i.e. a way to grow a footprint nobody bills.
* Billing it to the sandbox's ``diskMB`` instead is a trap, and it is why the
  account is separate rather than folded in: ``pause`` is the action that *frees*
  resources, so pushing a paused sandbox over budget takes its writes away -- the
  write side is enforced with ``RLIMIT_FSIZE=0`` (every write ``EFBIG``) and
  ``O_CREAT``/``mkdir``/``symlink``/``link`` answering ``ENOSPC`` (see
  ``control_plane/registry/manager.py::enforce_disk_budget``) -- while not one of
  the sandbox's own files changed.

So the deployment measures these bytes for itself and **refuses** an image that
does not fit, falling back to the behaviour that already exists (freeze in place)
instead of quietly spending a user's space.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from gateway_common.env import env_int
from gateway_common.paths import RUNTIME_DIR_NAME, resolve_state_base

logger = logging.getLogger(__name__)

#: MiB -> bytes, the unit the knob and every message below are written in.
_MIB = 1024 * 1024

#: One sentence for every "the account cannot be measured" decision, so the
#: admission, the pre-capture check and the heartbeat cannot describe the same
#: situation in three different ways. ``{decision}`` names what the caller does
#: about it.
UNKNOWN_ACCOUNT_REASON = (
    "the platform's checkpoint account could not be measured on this worker "
    "(a tree under <state>/_runtime is outside the worker's own reach and this "
    "shape has nobody to ask for it); an unmeasured account is never read as "
    "empty -- {decision}"
)


def platform_budget_bytes() -> int:
    """The platform's checkpoint account, in bytes.

    ``E2B_PLATFORM_DISK_MB`` (default 0) is the knob, and **0 means unlimited** --
    the deployment opts in. That default is deliberate: nothing about this account
    changes today's behaviour until a fleet decides how much it is willing to hold
    on behalf of paused sandboxes.
    """
    mb = env_int("E2B_PLATFORM_DISK_MB", 0)
    return max(0, mb) * _MIB


def measure_platform_disk_bytes(
    workspace_base: str | Path,
    state_base: str | Path | None = None,
    *,
    child_bytes: "Callable[[str], int | None] | None" = None,
) -> int | None:
    """Allocated bytes under ``<state base>/_runtime``; 0 when it is absent.

    Measured with the same accounting the per-sandbox ledger uses
    (``priv_helpers.dir_size``: files plus every directory's own allocated size),
    so "the platform's number" and "the user's number" are comparable quantities
    rather than two different definitions of usage.

    ``None`` means **the bytes could not be measured** and is *returned as such*
    -- never folded into 0 (C3 Task 4 third review, I-3). A worker in the agent
    shape has nobody to ask for a ``0700`` checkpoint store it does not own, and
    "the platform stores nothing" is exactly what 0 means to both the ledger
    alert and the checkpoint admission: reading an unmeasurable account as empty
    is the fail-open this account exists to prevent. ``0`` is still the answer
    for an *absent* runtime dir -- there is genuinely nothing there.

    ``state_base`` defaults to the workspace base, i.e. the directory this
    measured before N27; with ``E2B_STATE_BASE`` set the account follows the
    images to the base they actually live under, or a fleet would read 0 while
    the images pile up on the other base (see :func:`resolve_state_base`).

    One tree at a time, through :func:`_runtime_bytes_one_tree_at_a_time`: this
    root is the *node's*, not any one sandbox's.
    """
    runtime_dir = resolve_state_base(workspace_base, state_base) / RUNTIME_DIR_NAME
    if not runtime_dir.is_dir():
        return 0
    size = _runtime_bytes_one_tree_at_a_time(runtime_dir, child_bytes=child_bytes)
    return None if size is None else int(size)


def _runtime_bytes_one_tree_at_a_time(
    runtime_dir: Path, *, child_bytes: "Callable[[str], int | None] | None" = None
) -> int | None:
    """``dir_size(<runtime>)``, split into one walk per child of ``<runtime>``.

    Why split: ``<state base>/_runtime`` is the **node's** namespace -- every
    sandbox's platform dir (``_runtime/<id>``) and the whole ``.checkpoints``
    gate live under it -- so measuring it with one ``priv_helpers.dir_size`` is
    a *multi-tree* walk as soon as a child is out of the worker's own reach (a
    checkpoint image written by a sandbox's slot, say) and the walk falls
    through to ``e2b-maint walk``. The broker's answer is one line whose ceiling
    is sized for a **single** tree (``E2B_DISK_MAX_ENTRIES`` is a per-tree cap),
    so a multi-tree answer is exactly the shape that gets refused -- and the
    accounting paths only warn, so the number would go silently stale.

    The arithmetic is unchanged: ``directory_cost`` of ``<runtime>`` itself plus
    one ``dir_size`` per child is precisely what one walk of the whole tree
    visits (``os.walk`` costs every directory it enters and every file beneath
    it). "Unchanged" is within one *route family*: a child that the worker can
    read in process and one it hands to ``e2b-maint walk`` already disagreed on
    symlinks before this change (the broker walks ``FTS_PHYSICAL``), and
    splitting only decides which children take which route. ``None`` still means
    "could not be measured".
    """
    from envd_service import priv_helpers
    from envd_service.runtime.brief_stat import directory_cost, entry_size

    total = 0
    try:
        total += directory_cost(runtime_dir)
    except OSError:
        pass
    try:
        with os.scandir(runtime_dir) as listing:
            children = list(listing)
    except OSError:
        # Nothing to split on: an unsplit broker walk is the multi-tree answer
        # this exists to prevent, so report "unknown" rather than ask for it.
        # Say so once per call: ``None`` is what the callers act on (the
        # admission refuses an unknown account, the heartbeat omits the number),
        # and this line is what an operator greps for when it starts happening.
        logger.warning(
            "cannot list %s to measure the platform disk one tree at a time; "
            "reporting the account as unknown",
            runtime_dir,
        )
        return None
    for entry in children:
        try:
            is_dir = entry.is_dir()  # follows symlinks, like ``os.walk``'s dirs
            is_symlink = entry.is_symlink()
        except OSError:
            return None
        if is_dir:
            if is_symlink:
                continue  # ``os.walk`` lists it, never descends -- so it costs 0
            size = priv_helpers.dir_size(entry.path)
            if size is None and child_bytes is not None:
                # The child is out of the worker's own reach -- a ``0700``
                # checkpoint store owned by a sandbox's uid. The **agent** is
                # the reader that replaced ``e2b-maint`` here (that binary is
                # retired), and its walk is keyed by the sandbox id, which is
                # exactly this directory's name. ``None`` from the fallback
                # keeps the account unknown, which is the same fail-closed
                # answer as before.
                try:
                    size = child_bytes(entry.name)
                except Exception:  # noqa: BLE001 - unknown stays unknown
                    size = None
        else:
            try:
                size = entry_size(entry.path)
            except OSError:
                continue
        if size is None:
            # The silent half of I-3: a child the worker cannot read (a
            # ``0700`` checkpoint store owned by a sandbox uid, with no broker
            # in the agent shape) used to fall through to a 0 for the *whole*
            # account. Name the child and let the caller treat the account as
            # unknown.
            logger.warning(
                "cannot measure %s (a child of %s): reporting the platform "
                "disk account as unknown",
                entry.path,
                runtime_dir,
            )
            return None
        total += size
    return total


def checkpoint_admission(
    *,
    used_bytes: int | None,
    incoming_bytes: int,
    limit_bytes: int | None = None,
) -> tuple[bool, str]:
    """Whether an image of ``incoming_bytes`` fits the platform's account.

    Returns ``(allowed, reason)``; ``reason`` is empty when allowed and a sentence
    naming the numbers when not, because the caller has to say *why* a checkpoint
    was refused while the sandbox kept running.

    ``used_bytes=None`` ("the account could not be measured") **never** grants:
    an unmeasured account is not an empty one (I-3), and the refusal falls back
    to the behaviour that already exists -- the sandbox is frozen in place
    instead of the platform spending space it cannot account for.
    """
    if limit_bytes is None:
        limit_bytes = platform_budget_bytes()
    if limit_bytes <= 0:
        return True, ""
    if used_bytes is None:
        return False, UNKNOWN_ACCOUNT_REASON.format(
            decision="no image can be taken until the account can be measured"
        )
    used = max(0, int(used_bytes))
    incoming = max(0, int(incoming_bytes))
    if used + incoming <= limit_bytes:
        return True, ""
    return False, (
        "checkpoint of {incoming} MiB would put the platform's checkpoint account at "
        "{total} MiB, over its {limit} MiB budget ({used} MiB already held); the "
        "sandbox is left paused in place instead of spending the user's disk"
    ).format(
        incoming=incoming // _MIB,
        total=(used + incoming) // _MIB,
        limit=limit_bytes // _MIB,
        used=used // _MIB,
    )


def checkpoint_no_room_reason(
    *, used_bytes: int | None, limit_bytes: int | None = None
) -> str | None:
    """The sentence for "the account is already full", or ``None`` when it is not.

    Separate from :func:`checkpoint_admission` because it is asked *before* a
    capture can be started: an image's size is only knowable by writing it, so a
    fleet with no room left at all must refuse on the number it already has
    rather than spend a whole capture to learn the same thing. ``None`` means
    "there is room to try" -- the real admission still runs on what was written.
    """
    if limit_bytes is None:
        limit_bytes = platform_budget_bytes()
    if limit_bytes <= 0:
        return None
    if used_bytes is None:
        # Asked *before* a capture: "is there room to try?" cannot be answered
        # for an unmeasured account, and guessing "yes" is the fail-open I-3
        # removed. Refuse with the reason; the sandbox is left paused in place.
        return UNKNOWN_ACCOUNT_REASON.format(
            decision="no image can be taken until the account can be measured"
        )
    used = max(0, int(used_bytes))
    if used < limit_bytes:
        return None
    return (
        "the platform's checkpoint account is already full: {used} MiB held of "
        "its {limit} MiB budget, so no image can be taken; the sandbox is left "
        "paused in place instead of spending the user's disk"
    ).format(used=used // _MIB, limit=limit_bytes // _MIB)
