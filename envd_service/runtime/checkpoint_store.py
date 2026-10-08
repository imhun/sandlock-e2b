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
capture is performed *by the sandbox's own process tree* -- under own identity the
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

**The store's owner rule (E4, 2026-09-26).** The images are the largest thing
the platform holds for a sandbox, and the store they live in
(``_runtime/.checkpoints/``) is the one namespace no other scan walks: the
orphan-tree GC enumerates *trees*, so an image whose record is gone -- the
record was deleted and the image was not, or a capture finished and the record
vanished right after -- used to sit on the platform's account forever, until
the account was full and refused the next capture. The rule that ends that is
the one in :func:`remove_orphan_checkpoint_stores`: an image goes only when
**nothing** claims its id (no control-plane record anywhere in the fleet, no
in-memory runtime, no tree on this worker's disk), because deleting one by
mistake destroys a user's state. The other half is that a *refused* capture
leaves nothing behind: the store directory a refusal created is removed again
when it holds no image (:func:`_discard_empty_store`), so "the sandbox is left
exactly as it was found" is true of the directory tree too, not just of the
bytes the account measures.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from envd_service.runtime.platform_disk import (
    UNKNOWN_ACCOUNT_REASON,
    checkpoint_admission,
    checkpoint_no_room_reason,
    measure_platform_disk_bytes,
    platform_budget_bytes,
)
from gateway_common.paths import (
    CHECKPOINT_ROOT_NAME,
    RUNTIME_DIR_NAME,
    resolve_state_base,
    sandbox_checkpoint_dir,
    sandbox_runtime_dir,
)

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
    uid, because that is who writes the image: under own identity the slot runs as
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
    # The store's own gate: traversable **and listable**, owned by the worker.
    #
    # Listing matters, and it is not a privacy question: the platform-disk
    # account measures the store one child at a time (`platform_disk`), and with
    # a 0711 gate the worker cannot even enumerate the sandboxes whose images
    # are inside -- so from the first capture onwards the account came back
    # *unmeasurable*. The capture then refused to write (correctly: an unmeasured
    # account is not an empty one) while the pause silently kept the *previous*
    # image, and a later resume brought that older generation back (measured
    # 2026-10-05 on the k0s acceptance: the first ticker kept advancing while the
    # second stood still). 0755 costs no isolation: the state base is outside
    # every sandbox's root (N27), and each `<id>` below stays 0700 owned by that
    # sandbox's uid. The paths.py note that the worker measures this store "as
    # root, or through `e2b-maint`" predates C3, which retired `e2b-maint`; the
    # agent is the reader that replaced it.
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o755)
    except OSError:  # pragma: no cover - best effort, like the modes above
        pass
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        # A *re-pause* finds this directory already handed to the sandbox's uid,
        # and an unprivileged worker may not chmod another uid's directory --
        # EPERM, which used to abort the whole capture ("the checkpoint
        # directory could not be handed to the sandbox's uid ..."). In that
        # case the mode is already what this line wants (the hand-over set
        # 0700), so best effort is correct here, exactly as it is for the gate.
        pass
    owner = os.geteuid() if owner_uid is None else int(owner_uid)
    if owner != os.geteuid():
        # Hand it over only when it is not already that uid's: the second pause
        # of a sandbox finds its directory owned by the slot from the first, and
        # asking the agent to chown it again is at best a wasted round trip (and
        # at worst the same EPERM, reported as a failed capture).
        try:
            current_uid = parent.stat().st_uid
        except OSError:
            current_uid = None
        if current_uid != owner:
            _hand_to_sandbox(parent, owner, sandbox_id=sandbox_id)
    else:
        try:
            os.chown(parent, owner, owner)
        except OSError:  # pragma: no cover - best effort for the no-op case
            pass
    return image


def _hand_to_sandbox(
    path: Path, uid: int, *, recursive: bool = False, sandbox_id: str | None = None
) -> None:
    """Give ``path`` to the pooled uid that will write it (raises on failure).

    Root does it itself. Every other shape asks the agent; a non-root worker
    without one has no privileged file-step path left (the file-capability
    broker that used to be the third shape is retired, open-issues N52) and
    fails closed here rather than leaving the image worker-owned.

    C3 Task 4: in the agent shape the step is asked of the control plane as
    ``{sandbox_id, op}`` -- the checkpoint store's path is derived there from
    the same ``<state base>/_runtime/.checkpoints/<id>`` convention -- and no
    path leaves this process.
    """
    from envd_service import agent_fileops, priv_helpers

    client = agent_fileops.active()
    if client is not None:
        if sandbox_id is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id to hand a checkpoint "
                f"image over: {path} was not named by one"
            )
        client.chown_checkpoint(sandbox_id, recursive=recursive)
        return
    if os.geteuid() == 0:
        if not recursive:
            os.chown(path, uid, uid)
            return
        os.chown(path, uid, uid)
        for root, dirs, files in os.walk(path):
            for entry in (*dirs, *files):
                os.chown(Path(root) / entry, uid, uid)
        return
    raise priv_helpers.PrivHelperError(
        f"cannot hand {path} to sandbox uid {uid}: this worker has no "
        "privileged file-step path (no per-node agent is configured, and it "
        "is not root)"
    )


def image_bytes(image: Path, *, sandbox_id: str | None = None) -> int:
    """Allocated bytes of one image, ``0`` when it is not there.

    The image is ``0700`` owned by the sandbox's own uid, so a non-root worker
    needs the broker -- or, in the C3 agent shape, the agent's ``walk``
    (``walk-checkpoint``), which the control plane derives from the sandbox id
    for the same reason it derives every other path (hard rule 3).
    """
    from envd_service import agent_fileops, priv_helpers

    if not image.is_dir():
        return 0
    client = agent_fileops.active()
    if client is not None:
        if sandbox_id is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id to measure a "
                f"checkpoint image: {image} was not named by one"
            )
        return client.checkpoint_bytes(sandbox_id)
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
    image_mb = 0
    if has_image:
        # Same "nothing to report" contract as the reads above (C3 Task 4
        # review, N4): this is a *diagnostic*, and a transient control-plane
        # outage in the agent shape used to turn it into a 500 -- while a
        # half-written image two lines up is deliberately reported as absent.
        # Unknown bytes therefore read as 0 (the convention ``image_bytes``
        # already uses for a broker it cannot ask), with the reason on record.
        try:
            image_mb = image_bytes(image, sandbox_id=sandbox_id) // _MIB
        except Exception as exc:  # noqa: BLE001 - a diagnostic may not raise
            logger.warning(
                "sandbox %s: cannot measure the checkpoint image: %s: %s",
                sandbox_id,
                type(exc).__name__,
                exc,
            )
            image_mb = 0
    return {
        "sandboxID": sandbox_id,
        "hasImage": has_image,
        "imageMB": image_mb,
        "capturedAt": captured_at,
        "lastRestore": last,
    }


def _platform_numbers(
    workspace_base, state_base=None, *, child_bytes=None
) -> tuple[int | None, int]:
    """``(used bytes or None, budget bytes)`` -- ``None`` is "cannot measure" (I-3)."""
    _ensure_store_gate(workspace_base, state_base)
    return (
        measure_platform_disk_bytes(
            workspace_base, state_base=state_base, child_bytes=child_bytes
        ),
        platform_budget_bytes(),
    )


def _ensure_store_gate(workspace_base, state_base=None) -> None:
    """Repair the store gate's mode on a store an earlier build created.

    ``_prepare_image_parent`` writes ``0755`` from today on, but a store created
    before that is ``0711`` -- traversable, *not listable* -- and the platform
    account is measured by walking the store's children. Repairing it here (we
    own the directory; the sandbox cannot reach the state base at all, N27) is
    what makes the fix take effect on a running deployment instead of only on a
    freshly formatted volume.
    """
    root = (
        resolve_state_base(workspace_base, state_base)
        / RUNTIME_DIR_NAME
        / CHECKPOINT_ROOT_NAME
    )
    try:
        if root.is_dir() and stat.S_IMODE(root.stat().st_mode) != 0o755:
            os.chmod(root, 0o755)
    except OSError:  # pragma: no cover - best effort, like the modes above
        pass


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
    child_bytes=None,
) -> dict:
    """Take this sandbox's checkpoint, or say why there is none.

    Returns the shape the agent endpoint hands back: ``{"captured": bool,
    "reason": str, ...}``, with the image path, the process pid, the recorded fd
    count and both sides of the platform account on success. ``captured: False``
    is a normal answer -- the caller (a pause, an operator) continues with the
    behaviour it had before this feature existed -- so the reason is always a
    sentence, never an empty string.

    ``child_bytes(name) -> int | None`` measures one child of ``<state>/_runtime``
    that this process cannot read itself (a ``0700`` checkpoint store owned by a
    sandbox's uid); the caller wires it to the agent, which is the reader that
    replaced the retired ``e2b-maint``. Without it the platform account is
    unmeasurable from the first capture onwards and every later capture is
    refused.
    """
    used_before, limit = _platform_numbers(
        workspace_base, state_base, child_bytes=child_bytes
    )
    if used_before is None:
        # I-3: an unmeasured account is not an empty one. Refusing here (before
        # anything is written) is the same exit as "the account is full": the
        # sandbox is left frozen in place, and the caller gets the reason.
        reason = UNKNOWN_ACCOUNT_REASON.format(
            decision="no image can be taken until the account can be measured"
        )
        logger.warning("sandbox %s: %s", sandbox_id, reason)
        return _capture_reply(
            sandbox_id, False, reason, used=None, limit=limit
        )
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
        # The hand-off can fail *after* the directory exists (the mkdir ran, the
        # chown did not): a refusal keeps no half-created store either. The path
        # is recomputed rather than read back from the ``try`` above, which is
        # where ``image`` would have been bound if the call had returned.
        _discard_empty_store(
            checkpoint_image_dir(workspace_base, sandbox_id, state_base=state_base)
        )
        logger.warning("sandbox %s: %s", sandbox_id, reason)
        return _capture_reply(
            sandbox_id, False, reason, used=used_before, limit=limit
        )
    holder = _executor_of(ctx)
    capture = getattr(holder, "capture_checkpoint", None)
    if capture is None:
        _discard_empty_store(image)
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
        _discard_empty_store(image)
        return _capture_reply(
            sandbox_id, False, reason, used=used_before, limit=limit
        )

    written = image_bytes(image, sandbox_id=sandbox_id)
    allowed, over = checkpoint_admission(
        used_bytes=used_before, incoming_bytes=written, limit_bytes=limit
    )
    if not allowed:
        # The account is enforced on what was actually written: the size is only
        # knowable by capturing, so the image is removed again and the sandbox is
        # left exactly as it was found.
        _remove_image(image, sandbox_id=sandbox_id)
        _discard_empty_store(image)
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
        # ``used_before`` cannot be None here: an unmeasured account is refused
        # before the capture (see the top of this function).
        (int(used_before) + written) // _MIB,
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
    used: int | None,
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
        # ``None`` = the account could not be measured (I-3): the honest wire
        # value, and the one the CP's ``update_usage`` keeps its previous number
        # for. The *budget* still ships -- it is a configured number.
        "platformDiskUsedMB": None if used is None else used // _MIB,
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


def _image_is_there(image: Path) -> bool:
    """Is the checkpoint image present, judged without the *worker's* eyes?

    ``Path.is_dir()`` answers False for EACCES, and the image lives at
    ``<id>/latest`` inside the sandbox's own 0700 directory -- so the worker
    (uid 65534) cannot see it while the sandbox's uid can. Reading that False as
    "absent" is how a paused sandbox came back from a worker replacement with
    "no checkpoint image for this sandbox" (measured on the k0s cluster,
    2026-10-05: the image was on the shared volume the whole time, the replacing
    worker just could not look into the store). The slot that performs the
    resume runs as the sandbox's uid, so the honest answer here is "cannot look,
    not proven absent" -- it then reports the real reason itself.
    """
    try:
        return stat.S_ISDIR(os.stat(image).st_mode)
    except PermissionError:
        return True
    except OSError:
        return False


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
    if not _image_is_there(image):
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
            # `PermissionError` here is the *normal* shape, not a failure: the
            # store is `<id>/latest` inside the sandbox's 0700 directory, so this
            # worker (uid 65534) cannot stat into it while the sandbox's own uid
            # can. Only a uid we *can* read is checked for a hand-over.
            needs_hand = image.stat().st_uid != int(owner_uid)
        except PermissionError:
            needs_hand = False
        except Exception as exc:  # noqa: BLE001 - reported as "not restored"
            reason = (
                "the checkpoint image could not be inspected: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning("sandbox %s: %s", sandbox_id, reason)
            return {
                "sandbox_id": sandbox_id,
                "restored": False,
                "reason": reason,
                "image": str(image),
            }
        if needs_hand:
            try:
                _hand_to_sandbox(
                    image, int(owner_uid), recursive=True, sandbox_id=sandbox_id
                )
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
    _remove_image(image, sandbox_id=sandbox_id)
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


def _remove_image(image: Path, *, sandbox_id: str | None = None) -> None:
    """Delete one image tree, in-process first and through the broker on EACCES.

    The image is ``0700`` and owned by the sandbox's uid (see the module doc), so
    a *non-root* worker cannot walk into it: ``priv_helpers.remove_tree`` is the
    platform's own fallback for exactly that (`e2b-maint`, ``CAP_DAC_OVERRIDE``).
    A root worker removes it directly.

    C3 Task 4: the agent shape asks the control plane for ``remove-checkpoint``,
    which resolves to the same ``<state base>/_runtime/.checkpoints/<id>`` path.

    ⚠ **A recorded drift, not an oversight** (review Task 4 slice A, Minor 6):
    the callers pass the *image* (``<store>/latest``) while the op's target is
    the **store** -- ``remove-checkpoint`` removes the sandbox's whole
    checkpoint directory. That is exact rather than approximate because the
    store holds exactly one image by construction ("one image per sandbox,
    consumed on the way out", module doc), and the teardown's own hook
    (:func:`remove_checkpoint_images`) does mean the store. Narrowing the op to
    ``<store>/latest`` would leave an empty store behind after every consume --
    which the orphan sweep is what collects, and that sweep is exactly what the
    agent shape turns off. If a second image ever becomes legal here, this is
    the line to revisit.

    ⚠ **Also recorded** (C3 Task 4 second review, N6): unlike
    :func:`envd_service.agent._remove_agent_half`, this branch has no
    "already absent" arm -- ``e2b-maint rm`` refuses a path that is not there,
    so an image that vanished between the caller's own ``is_dir()`` check and
    the op raises instead of reading as "nothing to consume". Every caller
    checks first (``remove_checkpoint_images``, ``_discard_empty_store``, and
    the resume path's own guard), so the window is one syscall wide and the
    outcome is a named refusal, not a silent skip; recorded here so the final
    review can decide whether to fold this branch into ``_remove_agent_half``.
    """
    from envd_service import agent_fileops, priv_helpers

    client = agent_fileops.active()
    if client is not None:
        if sandbox_id is None:
            raise priv_helpers.PrivHelperError(
                "the C3 agent shape needs the sandbox id to remove a "
                f"checkpoint image: {image} was not named by one"
            )
        client.remove_checkpoint(sandbox_id)
        return

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
    _remove_image(store, sandbox_id=sandbox_id)
    return True


def checkpoint_store_is_empty(image: Path) -> bool:
    """Whether the store ``image`` would live in holds nothing at all.

    ``True`` for a store that was never created as well as for the empty
    directory a refused capture leaves behind -- both are the answer "there is
    no image here" to the question its callers ask. A store that holds anything
    (an image, a half-written one, a leftover) is ``False``, and so is one that
    cannot be read: neither caller may remove a directory it could not look
    into.
    """
    store = image.parent
    try:
        with os.scandir(store) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True
    except OSError:  # pragma: no cover - unreadable: assume it holds something
        return False


def _discard_empty_store(image: Path) -> None:
    """Take back the store directory a **refused** capture created.

    ``_prepare_image_parent`` creates ``<id>/`` before the slot is asked to
    write the image into it, so every refusal that comes after it used to leave
    an empty directory behind: measurable on the platform's account, claimed by
    nobody, and -- to the *next* capture -- "the directory is already there".
    Removing it is what makes "refused" mean the directory tree is the one the
    caller started with, rather than half an action.

    Only an *empty* store goes. One that holds an image (a previous capture, or
    a refusal that landed after one) is left exactly as it is.
    """
    if not checkpoint_store_is_empty(image):
        return
    try:
        image.parent.rmdir()
    except OSError:  # includes FileNotFoundError: nothing left to take back
        pass


def list_checkpoint_stores(workspace_base, *, state_base=None) -> list[str]:
    """The sandbox ids ``_runtime/.checkpoints/`` holds a store for (list only).

    The candidate set the orphan sweep starts from, and the one namespace no
    other scan walks: the orphan-tree GC and the fail-safe quota reconcile both
    enumerate *trees*, and this store is a sibling of the per-sandbox runtime
    dirs rather than a child of any of them (see the module doc). Without this
    entry an image whose record is gone is invisible to every collector.

    No name rule on purpose (M1's lesson): any ``[A-Za-z0-9_-]`` string is a
    legal sandbox id, so a filter here could drop a real store whose id happens
    to spell something the platform also uses as a name. Every directory under
    the store root is a candidate; whether it may go is
    :func:`remove_orphan_checkpoint_stores`'s question, not this one's.

    An unreadable store root reads as "no candidates": the caller is a
    maintenance round, and "I could not look" must never be read as "these
    images are ownerless".
    """
    root = (
        resolve_state_base(workspace_base, state_base)
        / RUNTIME_DIR_NAME
        / CHECKPOINT_ROOT_NAME
    )
    try:
        entries = sorted(root.iterdir())
    except OSError:  # no store root, or one this worker cannot read
        return []
    return [
        entry.name for entry in entries if entry.is_dir() and not entry.is_symlink()
    ]


def remove_orphan_checkpoint_stores(
    workspace_base, *, keep: set[str], state_base=None
) -> list[str]:
    """Remove the images of sandboxes ``keep`` does not claim; return those ids.

    ``keep`` is the caller's complete evidence of ownership, and it has to be
    *both* halves: every control-plane record in the fleet -- a store lives on
    the shared volume, so this worker sees other nodes' images, and only a
    fleet-wide answer separates those from orphans -- **and** every sandbox
    this worker still holds a record for, in memory or on disk. "There is no
    record" has to mean "nowhere": an image is the user's whole process, so the
    default here is *not* to delete, and one claimant anywhere is enough to
    keep it.

    **An image is only meaningful while a record claims it.** The record is the
    only thing that knows how to resume one, so an image no record anywhere
    claims can never be used again -- it only ever occupies the platform's
    account and refuses the next capture. That is what makes this rule
    *different* from the one next door: a **tree** is the user's data, and
    "its record cannot be read" is no reason to delete data, which is why the
    reconcile keeps a tree it could not verify (``unmaterialised``) and still
    collects an image that nothing claims. The two halves of one round
    disagreeing on the same id is the intended shape, not half an action.

    Nothing is guessed from a name or a size, and a store the worker cannot
    remove is reported and left for the next round instead of costing this one
    its remaining work.
    """
    removed: list[str] = []
    for sandbox_id in list_checkpoint_stores(workspace_base, state_base=state_base):
        if sandbox_id in keep:
            continue
        logger.warning(
            "checkpoint store of unknown sandbox %s: reclaiming", sandbox_id
        )
        try:
            if remove_checkpoint_images(
                workspace_base, sandbox_id, state_base=state_base
            ):
                removed.append(sandbox_id)
        except Exception:  # noqa: BLE001 - reported, never fatal to the round
            logger.warning(
                "checkpoint store of unknown sandbox %s: could not be removed; "
                "leaving it for the next reconcile",
                sandbox_id,
                exc_info=True,
            )
    return removed


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
    _remove_image(image, sandbox_id=sandbox_id)
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
