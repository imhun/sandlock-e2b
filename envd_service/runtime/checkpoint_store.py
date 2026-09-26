"""A sandbox's checkpoint image: where it lives, who pays for it, when it goes.

The engine's half is done -- a slot writes an image and can resume one into its
own session (``docs/checkpoint-restore-e2b-half.md`` §(g)) -- so what is left is
the deployment's half, and it is three decisions in one file.

**Storage (D1/D2).** An image goes to ``<base>/_runtime/.checkpoints/<id>/latest``
(`gateway_common.paths.sandbox_checkpoint_dir`): outside the tree the per-sandbox
quota measures, inside the platform's ``_runtime``, and -- because the capture is
performed by the sandbox's own slot -- ``0700`` owned by the **sandbox's own
uid**. No other sandbox can reach it, and the worker keeps measuring and removing
it (as root, or through the maintenance broker: ``priv_helpers.dir_size`` /
``remove_tree``).

**Who can read the image, stated honestly (D2 revised 2026-09-25).** The
capture is performed *by the sandbox's own process tree* -- under route B the
slot runs as the sandbox's pooled uid, and that is the only process that owns
the address space being captured -- so the directory has to be writable by that
uid. The earlier design said "worker uid, the sandbox never reads it"; that is
not implementable for the capture path, because the writer *is* the sandbox's
own identity. What is preserved: (a) another sandbox cannot read or write it
(different uids, ``0700``), (b) it stays outside the tree the user's quota
measures, (c) the platform can still measure and delete it. What is *not*
preserved: the sandbox can read (and forge) its own image. That is bounded --
the image is its own memory, the forged process would run with the identity it
already has, and the bytes are accounted for either way -- but it is a real
weakening, and the way to close it is to have the slot hand the blob *to the
worker* rather than write it where it sits (a protocol change, not done here).

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

**Where the answer to a read-only question lives (E3, 2026-09-26).** A sandbox's
image, its size and "how many fds did the last resume lose" are facts about what
*a worker* did, so the last of them is written by the worker into **its own**
runtime directory (``_runtime/<id>/last-restore.json``, beside ``sandbox.json``
and the same ``0700``) and the control plane only proxies
(``GET /sandboxes/{id}/checkpoint``). The alternative -- adding the outcome to
the control plane's sandbox record -- was rejected on purpose: every field there
has to survive a Redis round trip and both ``to_storage_dict``/``from_dict``
paths, and "the last restore" is the sandbox *node's* observation, so a copy in
the record would be a second source of truth about a process the control plane
never saw. The record keeps answering "is this sandbox paused"; the worker
answers "what did the last resume actually bring back", and a node that cannot
be reached says so rather than guessing (``unreachable`` in the proxy reply).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from envd_service.runtime.platform_disk import (
    checkpoint_admission,
    checkpoint_no_room_reason,
    measure_platform_disk_bytes,
    platform_budget_bytes,
)
from gateway_common.paths import sandbox_checkpoint_dir, sandbox_runtime_dir

logger = logging.getLogger(__name__)

#: The one image a sandbox holds. No dot in the name on purpose: the engine's
#: ``Checkpoint::save`` writes ``<dir>.tmp`` and renames, and a name that looked
#: like an extension would make that temporary directory a sibling of something
#: else entirely.
IMAGE_NAME = "latest"

#: The last restore this worker performed (see :func:`record_restore_outcome`).
#: It sits beside the runtime record (``sandbox.json``) and the command log, and
#: it is *never* inside :data:`IMAGE_NAME`: the image is written by the sandbox's
#: own slot under its own uid, and a read-only query's bookkeeping does not
#: belong in the one directory that hand-off is about.
LAST_RESTORE_NAME = "last-restore.json"

_MIB = 1024 * 1024


def checkpoint_image_dir(
    workspace_base, sandbox_id: str, *, state_base=None
) -> Path:
    """``<state base>/_runtime/.checkpoints/<id>/latest`` -- the image.

    ``state_base`` defaults to ``workspace_base`` (today's layout); with
    ``E2B_STATE_BASE`` set the image lands under the base the platform's own
    files live under (N27), which is where the teardown, the quarantine and the
    platform account all look for it.
    """
    return (
        sandbox_checkpoint_dir(workspace_base, sandbox_id, state_base=state_base)
        / IMAGE_NAME
    )


def _prepare_image_parent(
    workspace_base, sandbox_id: str, *, owner_uid: int | None, state_base=None
) -> Path:
    """The image path, with a ``0700`` parent **the slot can write**.

    The directory is created by the worker and then handed to the sandbox's own
    uid, because that is who writes the image: under route B the slot runs as
    the sandbox's pooled uid and it is the only process that owns the address
    space being captured. Without the hand-off the capture itself succeeds and
    the *save* dies with EACCES -- measured on the cluster (2026-09-25), which
    is why this is not "the worker's own directory" as the first design said.

    ``owner_uid is None`` means the deployment has no pooled uid (the shared-uid
    shape): the slot already runs as the worker, so nothing has to move.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id, state_base=state_base)
    root = image.parent.parent
    parent = image.parent
    # The store's own gate: traversable, not listable, owned by the worker. The
    # slot has to reach *its* directory through it.
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o711)
    except OSError:  # pragma: no cover - best effort, like the modes above
        pass
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    owner = os.geteuid() if owner_uid is None else int(owner_uid)
    if owner != os.geteuid():
        _hand_to_sandbox(parent, owner)
    else:
        try:
            os.chown(parent, owner, owner)
        except OSError:  # pragma: no cover - best effort for the no-op case
            pass
    return image


def _hand_to_sandbox(path: Path, uid: int, *, recursive: bool = False) -> None:
    """Give ``path`` to the pooled uid that will write it (raises on failure).

    Root does it directly; a non-root worker goes through ``e2b-maint``
    (``CAP_CHOWN``), which is the same broker the rest of the platform uses to
    move a path between the worker's identity and a sandbox's.
    """
    if os.geteuid() == 0:
        if not recursive:
            os.chown(path, uid, uid)
            return
        os.chown(path, uid, uid)
        for root, dirs, files in os.walk(path):
            for entry in (*dirs, *files):
                os.chown(Path(root) / entry, uid, uid)
        return
    from envd_service import priv_helpers

    priv_helpers.broker_chown(uid, path, recursive=recursive)


def image_bytes(image: Path) -> int:
    """Allocated bytes of one image, ``0`` when it is not there."""
    from envd_service import priv_helpers

    if not image.is_dir():
        return 0
    size = priv_helpers.dir_size(image)
    return 0 if size is None else int(size)


def restore_outcome_path(
    workspace_base, sandbox_id: str, *, state_base=None
) -> Path:
    """``<state base>/_runtime/<id>/last-restore.json`` -- the last restore's result.

    Beside the runtime record and the command log, not inside the image tree:
    the image belongs to the sandbox's own uid (see the module doc), while this
    file is the worker's own note about what it did with that image.
    """
    return (
        sandbox_runtime_dir(workspace_base, sandbox_id, state_base=state_base)
        / LAST_RESTORE_NAME
    )


def record_restore_outcome(
    workspace_base, sandbox_id: str, outcome: dict, *, state_base=None
) -> None:
    """Remember how the last resume went for this sandbox (D5/D6's readable half).

    Written to disk rather than kept in memory: a resume can be followed by
    another worker restart, and "the last resume lost two fds" is exactly the
    thing somebody reads *later*, while looking at a sandbox they did not watch
    fail. The file is replaced in one ``os.replace`` inside its own directory,
    so a reader sees either the whole previous record or the whole new one.

    A failed restore is recorded too -- the reason is the answer the reader came
    for -- and a recording that cannot happen is a warning, never a failure of
    the resume it describes.
    """
    record = restore_outcome_path(workspace_base, sandbox_id, state_base=state_base)
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        try:
            # The same mode the registry gives ``_runtime/<id>``
            # (``RuntimeRegistry._ensure_runtime_dir``): a first-record race must
            # not leave the directory of the record, this file and the command
            # log traversable by the sandbox whose record it holds. Best effort,
            # like every other mode in this module.
            os.chmod(record.parent, 0o700)
        except OSError:  # pragma: no cover - best effort
            pass
        payload = {
            "restored": bool(outcome.get("restored")),
            "reason": str(outcome.get("reason") or ""),
            "pid": outcome.get("pid"),
            "unrecoveredFdCount": int(outcome.get("unrecoveredFdCount") or 0),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = record.with_name(record.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, record)
    except OSError:  # pragma: no cover - a bookkeeping write cannot fail a resume
        logger.warning(
            "sandbox %s: could not record the restore outcome",
            sandbox_id,
            exc_info=True,
        )


def checkpoint_status(
    workspace_base, sandbox_id: str, *, state_base=None
) -> dict:
    """This sandbox's image and its last restore, in the read-only query's shape.

    Answers the three questions a user could not ask before E3 -- is there an
    image, how big is it, and what did the last resume bring back -- from the
    two places that know: the image directory (``gateway_common.paths``) and the
    outcome file this worker writes (:func:`record_restore_outcome`).

    Nothing here is a failure when absent: a sandbox that was never paused and a
    sandbox that was never resumed both answer with ``0``/``None`` rather than
    an error, because "there is no image" is the answer to the question.

    ``capturedAt`` is the image directory's mtime in whole seconds (the engine's
    save is a rename, so it is the moment the image appeared) and ``None`` when
    there is no image.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id, state_base=state_base)
    has_image = image.is_dir()
    captured_at: int | None = None
    if has_image:
        try:
            captured_at = int(image.stat().st_mtime)
        except OSError:  # pragma: no cover - a vanished image is "no image"
            captured_at = None
            has_image = False
    last: dict | None = None
    try:
        last = json.loads(
            restore_outcome_path(
                workspace_base, sandbox_id, state_base=state_base
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        # Never resumed, or a half-written file from an older shape: both read
        # as "nothing to report", which is what a diagnostic query may say.
        last = None
    if not isinstance(last, dict):
        last = None
    return {
        "sandboxID": sandbox_id,
        "hasImage": has_image,
        "imageMB": (image_bytes(image) // _MIB) if has_image else 0,
        "capturedAt": captured_at,
        "lastRestore": last,
    }


def _platform_numbers(workspace_base, state_base=None) -> tuple[int, int]:
    return (
        measure_platform_disk_bytes(workspace_base, state_base=state_base),
        platform_budget_bytes(),
    )


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


def capture_checkpoint_image(
    workspace_base,
    ctx,
    sandbox_id: str,
    *,
    owner_uid: int | None = None,
    state_base=None,
) -> dict:
    """Take this sandbox's checkpoint, or say why there is none.

    Returns the shape the agent endpoint hands back: ``{"captured": bool,
    "reason": str, ...}``, with the image path, the process pid, the recorded fd
    count and both sides of the platform account on success. ``captured: False``
    is a normal answer -- the caller (a pause, an operator) continues with the
    behaviour it had before this feature existed -- so the reason is always a
    sentence, never an empty string.
    """
    used_before, limit = _platform_numbers(workspace_base, state_base)
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

    try:
        image = _prepare_image_parent(
            workspace_base, sandbox_id, owner_uid=owner_uid, state_base=state_base
        )
    except Exception as exc:  # noqa: BLE001 - reported as "not captured"
        # The directory has to belong to the sandbox's uid or the *save* fails
        # inside the slot (EACCES, measured on the cluster). Saying so here is
        # better than letting that surface as a generic io error.
        reason = (
            "the checkpoint directory could not be handed to the sandbox's "
            f"uid {owner_uid}: {type(exc).__name__}: {exc}"
        )
        logger.warning("sandbox %s: %s", sandbox_id, reason)
        return _capture_reply(
            sandbox_id, False, reason, used=used_before, limit=limit
        )
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
        _remove_image(image)
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
        "sandbox %s: checkpoint image written to %s (%s MiB, pid %s, %s fd(s), "
        "captured %s %s); the platform account now holds %d MiB of %s",
        sandbox_id,
        image,
        written // _MIB,
        outcome.get("pid"),
        outcome.get("fds"),
        outcome.get("exe") or "<unknown>",
        list(outcome.get("argv") or []),
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
        # FUP-30: 谁被捕获了。空串 / 空表是真实答案（进程在捕获窗口里死了），
        # 不是一个"没接线"的信号。
        reply["exe"] = str(capture.get("exe") or "")
        reply["argv"] = list(capture.get("argv") or [])
    return reply


def restore_checkpoint_image(
    workspace_base,
    ctx,
    sandbox_id: str,
    *,
    owner_uid: int | None = None,
    state_base=None,
) -> dict:
    """Resume this sandbox's image into a session on **this** worker.

    The image is *consumed* on success (see the module doc). ``restored: False``
    with a reason is a normal answer: the caller's own state change has already
    happened by then, so this is a record of what the process tree looks like,
    not a gate.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id, state_base=state_base)
    if not image.is_dir():
        return {
            "sandbox_id": sandbox_id,
            "restored": False,
            "reason": "no checkpoint image for this sandbox",
            "image": None,
        }
    if owner_uid is not None:
        # The resuming slot reads the image as its own uid, so it has to own it.
        # Normally it already does (the capture ran as the same sandbox uid);
        # this covers the shapes where it does not -- a record whose uid changed,
        # or a legacy image taken before pooled uids.
        try:
            if image.stat().st_uid != int(owner_uid):
                _hand_to_sandbox(image, int(owner_uid), recursive=True)
        except Exception as exc:  # noqa: BLE001 - reported as "not restored"
            reason = (
                "the checkpoint image could not be handed to the sandbox's uid "
                f"{owner_uid}: {type(exc).__name__}: {exc}"
            )
            logger.warning("sandbox %s: %s", sandbox_id, reason)
            return {
                "sandbox_id": sandbox_id,
                "restored": False,
                "reason": reason,
                "image": str(image),
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
    _remove_image(image)
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


def _remove_image(image: Path) -> None:
    """Delete one image tree, in-process first and through the broker on EACCES.

    The image is ``0700`` and owned by the sandbox's uid (see the module doc), so
    a *non-root* worker cannot walk into it: ``priv_helpers.remove_tree`` is the
    platform's own fallback for exactly that (`e2b-maint`, ``CAP_DAC_OVERRIDE``).
    A root worker removes it directly.
    """
    from envd_service import priv_helpers

    priv_helpers.remove_tree(image)


def remove_checkpoint_images(
    workspace_base, sandbox_id: str, *, state_base=None
) -> bool:
    """Delete a sandbox's whole image directory; ``True`` if one was there.

    The teardown's and the quarantine's hook: images live *beside* the runtime
    dir (``_runtime/.checkpoints/<id>``), so removing ``_runtime/<id>`` -- which
    is what the teardown does, and what it has always done -- would leave them
    behind. Called from both places that make a sandbox's platform state go away.
    """
    store = sandbox_checkpoint_dir(workspace_base, sandbox_id, state_base=state_base)
    if not store.is_dir():
        return False
    _remove_image(store)
    return True


def consume_checkpoint_image(
    workspace_base, sandbox_id: str, *, state_base=None
) -> bool:
    """Drop the image of a sandbox whose process is back: ``True`` if one went.

    The thaw path of a resume (D5): the session was still on this worker, so the
    image is stale the moment the sandbox runs again. Removing it here -- rather
    than leaving it to the next pause to overwrite -- is what keeps the platform
    account honest for a sandbox that is resumed and then left running.
    """
    image = checkpoint_image_dir(workspace_base, sandbox_id, state_base=state_base)
    if not image.is_dir():
        return False
    _remove_image(image)
    return True


def resume_sandbox(
    workspace_base,
    ctx,
    sandbox_id: str,
    *,
    owner_uid: int | None = None,
    state_base=None,
) -> dict:
    """The resume half of the lifecycle: thaw what is here, resume what is not.

    Both paths end with no image left (D5 + the module doc). A worker that holds
    the session only has to thaw it -- and it does not matter what the caller
    thinks the state is, because ``ProcessManager.resume_all`` is itself a no-op
    on a session with nothing stopped.
    """
    if live_session_present(ctx):
        dropped = consume_checkpoint_image(
            workspace_base, sandbox_id, state_base=state_base
        )
        return {
            "sandbox_id": sandbox_id,
            "resumed": True,
            "restored": False,
            "reason": "",
            "staleImageRemoved": dropped,
        }
    outcome = restore_checkpoint_image(
        workspace_base,
        ctx,
        sandbox_id,
        owner_uid=owner_uid,
        state_base=state_base,
    )
    return {"sandbox_id": sandbox_id, "resumed": True, **outcome}
