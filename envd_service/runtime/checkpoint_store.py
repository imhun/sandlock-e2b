"""A sandbox's checkpoint image: where it lives, who pays for it, when it goes.

The engine's half is done -- a slot writes an image and can resume one into its
own session (``docs/checkpoint-restore-e2b-half.md`` §(g)) -- so what is left is
the deployment's half, and it is three decisions in one file.

**Storage (D1/D2).** An image goes to ``<base>/_runtime/<id>/checkpoint/latest``:
a sibling of the tree the per-sandbox quota measures, created ``0700`` for the
worker's own uid, exactly like the runtime record next to it. The sandbox has no
access to that directory at all, which is what makes holding a *process's
memory* there acceptable -- an image can contain credentials.

**The account (D3).** Those bytes are billed to the platform
(:mod:`envd_service.runtime.platform_disk`), never to the sandbox's ``diskMB``,
and an image that does not fit is refused **and removed**. Billing it to the
sandbox instead would turn ``pause`` -- the action that *frees* a node -- into
"you may not write any more": the write side of that budget is enforced with
``RLIMIT_FSIZE=0`` plus ``ENOSPC`` on creates, so a paused sandbox would be
pushed over by the very call that was supposed to free it, while none of its own
files changed.

**One image per sandbox, consumed on the way out.** ``pause`` overwrites it (the
engine's save is a rename, so an interrupted capture never leaves a half image
in its place) and ``resume`` removes it, on *both* of its paths: the thaw of a
session that is still here, and the restore of one that is gone. That is what
keeps "an image exists" meaning "a paused sandbox whose process is not on any
worker", so a later resume can never rewind a sandbox to an older process.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from envd_service.runtime.platform_disk import (
    checkpoint_admission,
    checkpoint_no_room_reason,
    measure_platform_disk_bytes,
    platform_budget_bytes,
)
from gateway_common.paths import sandbox_checkpoint_dir

logger = logging.getLogger(__name__)

#: The one image a sandbox holds. No dot in the name on purpose: the engine's
#: ``Checkpoint::save`` writes ``<dir>.tmp`` and renames, and a name that looked
#: like an extension would make that temporary directory a sibling of something
#: else entirely.
IMAGE_NAME = "latest"

_MIB = 1024 * 1024


def checkpoint_image_dir(workspace_base, sandbox_id: str) -> Path:
    """``<base>/_runtime/<id>/checkpoint/latest`` -- this sandbox's image."""
    return sandbox_checkpoint_dir(workspace_base, sandbox_id) / IMAGE_NAME


def _prepare_image_parent(workspace_base, sandbox_id: str) -> Path:
    """The image path, with a ``0700`` worker-owned parent (see the module doc).

    Mirrors ``RuntimeRegistry._ensure_runtime_dir`` for the record one level up:
    the sandbox's own host uid is neither the owner nor in the worker's group,
    so it cannot traverse in -- not to read an image, and not to unlink one.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id)
    parent = image.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
        os.chown(parent, os.geteuid(), os.getegid())
    except OSError:  # pragma: no cover - best effort, like the modes above
        pass
    return image


def image_bytes(image: Path) -> int:
    """Allocated bytes of one image, ``0`` when it is not there."""
    from envd_service import priv_helpers

    if not image.is_dir():
        return 0
    size = priv_helpers.dir_size(image)
    return 0 if size is None else int(size)


def _platform_numbers(workspace_base) -> tuple[int, int]:
    return measure_platform_disk_bytes(workspace_base), platform_budget_bytes()


def _executor_of(ctx):
    return getattr(ctx, "executor", None)


def live_session_present(ctx) -> bool:
    """Whether this worker holds a live session for the sandbox behind ``ctx``.

    The question the resume path asks (D5): a session that is *here* is thawed,
    and only a session that is gone is rebuilt from the image. ``instance_handle``
    is the executor's own answer to "is there a session", so a worker that never
    launched one (a sandbox created but never used, or a worker that came up
    after a restart) answers ``False`` rather than guessing from state.
    """
    holder = _executor_of(ctx)
    return getattr(holder, "instance_handle", None) is not None


def capture_checkpoint_image(workspace_base, ctx, sandbox_id: str) -> dict:
    """Take this sandbox's checkpoint, or say why there is none.

    Returns the shape the agent endpoint hands back: ``{"captured": bool,
    "reason": str, ...}``, with the image path, the process pid, the recorded fd
    count and both sides of the platform account on success. ``captured: False``
    is a normal answer -- the caller (a pause, an operator) continues with the
    behaviour it had before this feature existed -- so the reason is always a
    sentence, never an empty string.
    """
    used_before, limit = _platform_numbers(workspace_base)
    full = checkpoint_no_room_reason(used_bytes=used_before, limit_bytes=limit)
    if full is not None:
        # Refuse *before* writing: with no room at all there is nothing to learn
        # from the capture, and the image is the largest thing this worker writes.
        return _capture_reply(
            sandbox_id, False, full, used=used_before, limit=limit
        )
    if ctx is None:
        return _capture_reply(
            sandbox_id,
            False,
            "no live session on this worker to capture",
            used=used_before,
            limit=limit,
        )

    image = _prepare_image_parent(workspace_base, sandbox_id)
    holder = _executor_of(ctx)
    capture = getattr(holder, "capture_checkpoint", None)
    if capture is None:
        return _capture_reply(
            sandbox_id,
            False,
            "this executor cannot take checkpoints (no checkpoint verb)",
            used=used_before,
            limit=limit,
        )
    outcome = capture(str(image))
    if not outcome.get("captured"):
        reason = str(outcome.get("reason") or "the slot did not capture")
        return _capture_reply(
            sandbox_id, False, reason, used=used_before, limit=limit
        )

    written = image_bytes(image)
    allowed, over = checkpoint_admission(
        used_bytes=used_before, incoming_bytes=written, limit_bytes=limit
    )
    if not allowed:
        # The account is enforced on what was actually written: the size is only
        # knowable by capturing, so the image is removed again and the sandbox is
        # left exactly as it was found.
        shutil.rmtree(image, ignore_errors=True)
        logger.warning(
            "sandbox %s: checkpoint image of %d MiB refused and removed: %s",
            sandbox_id,
            written // _MIB,
            over,
        )
        return _capture_reply(
            sandbox_id, False, over, used=used_before, limit=limit, image_bytes=0
        )

    logger.info(
        "sandbox %s: checkpoint image written to %s (%s MiB, pid %s, %s fd(s)); "
        "the platform account now holds %d MiB of %s",
        sandbox_id,
        image,
        written // _MIB,
        outcome.get("pid"),
        outcome.get("fds"),
        (used_before + written) // _MIB,
        "an unlimited budget" if limit <= 0 else f"{limit // _MIB} MiB",
    )
    return _capture_reply(
        sandbox_id,
        True,
        "",
        used=used_before,
        limit=limit,
        image=image,
        image_bytes=written,
        capture=outcome,
    )


def _capture_reply(
    sandbox_id: str,
    captured: bool,
    reason: str,
    *,
    used: int,
    limit: int,
    image: Path | None = None,
    image_bytes: int = 0,
    capture: dict | None = None,
) -> dict:
    reply: dict = {
        "sandbox_id": sandbox_id,
        "captured": captured,
        "reason": reason,
        "image": str(image) if image is not None else None,
        "imageMB": image_bytes // _MIB,
        "platformDiskUsedMB": used // _MIB,
        "platformDiskBudgetMB": 0 if limit <= 0 else limit // _MIB,
    }
    if capture:
        reply["pid"] = capture.get("pid")
        reply["fds"] = capture.get("fds")
    return reply


def restore_checkpoint_image(workspace_base, ctx, sandbox_id: str) -> dict:
    """Resume this sandbox's image into a session on **this** worker.

    The image is *consumed* on success (see the module doc). ``restored: False``
    with a reason is a normal answer: the caller's own state change has already
    happened by then, so this is a record of what the process tree looks like,
    not a gate.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id)
    if not image.is_dir():
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": "no checkpoint image for this sandbox",
            "image": None,
        }
    if live_session_present(ctx):
        # Never quietly give a sandbox two processes: resuming an image next to a
        # running session is not what "resume" means, and the lifecycle's thaw
        # path is where a sandbox with a live session belongs.
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": (
                "this worker already holds a live session for the sandbox; a "
                "resume thaws that session instead of adding the image's process "
                "next to it"
            ),
            "image": str(image),
        }
    if ctx is None:
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": (
                "no runtime context on this worker to resume the image into "
                "(the image is kept)"
            ),
            "image": str(image),
        }
    holder = _executor_of(ctx)
    restore = getattr(holder, "restore_checkpoint", None)
    if restore is None:
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": "this executor cannot resume checkpoints (no restore verb)",
            "image": str(image),
        }
    outcome = restore(str(image))
    if not outcome.get("restored"):
        reason = str(outcome.get("reason") or "the slot did not resume the image")
        logger.warning(
            "sandbox %s: checkpoint image %s was not resumed: %s",
            sandbox_id,
            image,
            reason,
        )
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": reason,
            "image": str(image),
        }

    skipped = outcome.get("restore_skipped")
    skipped = skipped if isinstance(skipped, list) else []
    # D6/S4: the honest half. A restored process comes back without the sockets,
    # pipes and memfds it held, so "what could not be brought back" is part of
    # the result and part of the log -- never a quiet success whose first read
    # is what reports the loss.
    logger.info(
        "sandbox %s: resumed %s into the session (child %s, pid %s); "
        "%d fd(s) could not come back (sockets/pipes/memfds): %s",
        sandbox_id,
        image,
        outcome.get("child_id"),
        outcome.get("pid"),
        len(skipped),
        skipped if skipped else "none",
    )
    shutil.rmtree(image, ignore_errors=True)
    return {
        "sandbox_id": sandbox_id,
        "restored": True,
        "reason": "",
        "image": str(image),
        "consumed": True,
        "child_id": outcome.get("child_id"),
        "pid": outcome.get("pid"),
        # The engine's own list (fd number + the path it had). Empty is a real
        # answer: this process held nothing but its stdio.
        "unrecoveredFds": skipped,
        "unrecoveredFdCount": len(skipped),
    }


def consume_checkpoint_image(workspace_base, sandbox_id: str) -> bool:
    """Drop the image of a sandbox whose process is back: ``True`` if one went.

    The thaw path of a resume (D5): the session was still on this worker, so the
    image is stale the moment the sandbox runs again. Removing it here -- rather
    than leaving it to the next pause to overwrite -- is what keeps the platform
    account honest for a sandbox that is resumed and then left running.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id)
    if not image.is_dir():
        return False
    shutil.rmtree(image, ignore_errors=True)
    return True


def resume_sandbox(workspace_base, ctx, sandbox_id: str) -> dict:
    """The resume half of the lifecycle: thaw what is here, resume what is not.

    Both paths end with no image left (D5 + the module doc). A worker that holds
    the session only has to thaw it -- and it does not matter what the caller
    thinks the state is, because ``ProcessManager.resume_all`` is itself a no-op
    on a session with nothing stopped.
    """
    if live_session_present(ctx):
        dropped = consume_checkpoint_image(workspace_base, sandbox_id)
        return {
            "sandbox_id": sandbox_id,
            "resumed": True,
            "restored": False,
            "reason": "",
            "staleImageRemoved": dropped,
        }
    outcome = restore_checkpoint_image(workspace_base, ctx, sandbox_id)
    return {"sandbox_id": sandbox_id, "resumed": True, **outcome}
