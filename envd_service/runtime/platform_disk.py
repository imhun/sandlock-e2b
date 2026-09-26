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

from pathlib import Path

from gateway_common.env import env_int
from gateway_common.paths import RUNTIME_DIR_NAME, resolve_state_base

#: MiB -> bytes, the unit the knob and every message below are written in.
_MIB = 1024 * 1024


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
    workspace_base: str | Path, state_base: str | Path | None = None
) -> int:
    """Allocated bytes under ``<state base>/_runtime``; 0 when it is absent.

    Measured with the same accounting the per-sandbox ledger uses
    (``priv_helpers.dir_size``: files plus every directory's own allocated size),
    so "the platform's number" and "the user's number" are comparable quantities
    rather than two different definitions of usage. ``None`` from the walk means
    the bytes could not be measured -- reported as 0 here, with the caller free to
    decide what to do about it; the admission rule below never *grants* because a
    measurement failed.

    ``state_base`` defaults to the workspace base, i.e. the directory this
    measured before N27; with ``E2B_STATE_BASE`` set the account follows the
    images to the base they actually live under, or a fleet would read 0 while
    the images pile up on the other base (see :func:`resolve_state_base`).
    """
    from envd_service import priv_helpers

    runtime_dir = resolve_state_base(workspace_base, state_base) / RUNTIME_DIR_NAME
    if not runtime_dir.is_dir():
        return 0
    size = priv_helpers.dir_size(runtime_dir)
    return 0 if size is None else int(size)


def checkpoint_admission(
    *, used_bytes: int, incoming_bytes: int, limit_bytes: int | None = None
) -> tuple[bool, str]:
    """Whether an image of ``incoming_bytes`` fits the platform's account.

    Returns ``(allowed, reason)``; ``reason`` is empty when allowed and a sentence
    naming the numbers when not, because the caller has to say *why* a checkpoint
    was refused while the sandbox kept running.
    """
    if limit_bytes is None:
        limit_bytes = platform_budget_bytes()
    if limit_bytes <= 0:
        return True, ""
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
    *, used_bytes: int, limit_bytes: int | None = None
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
    used = max(0, int(used_bytes))
    if used < limit_bytes:
        return None
    return (
        "the platform's checkpoint account is already full: {used} MiB held of "
        "its {limit} MiB budget, so no image can be taken; the sandbox is left "
        "paused in place instead of spending the user's disk"
    ).format(used=used // _MIB, limit=limit_bytes // _MIB)
