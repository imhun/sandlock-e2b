"""Workspace writes, performed by the sandbox rather than by the worker (N28).

Before this module the worker wrote the sandbox's tree *itself*: an upload was
``open()`` + ``os.replace`` as the worker's uid, a ``MakeDir`` was ``mkdir`` as
the worker's uid, and the sandbox's own commands wrote as the sandbox's uid.
Two writers into one tree, which is what made the disk question unanswerable:

* a per-writer limit (``RLIMIT_FSIZE``, N28/C) is per-process, so it bounds
  only whichever writer set it;
* "who wrote this byte" has no single answer, so an accounting ledger has to
  trust a *file size after the fact* rather than an amount anybody declared;
* the workspace's DAC grants are a group write for the worker (E3.2's
  ``0770 <sandbox uid>:<worker gid>``), so a proxy write is invisible to the
  sandbox's own permission model -- it can create entries under a directory the
  sandbox made ``0500``.

So the worker stops writing. Every workspace write runs as a *command inside
the sandbox*: same uid, same mount, same policy, same view of the tree -- the
identity the tree's owner already is. The caller's bytes travel over the same
process channel the sandbox's own commands use (stdin), and the worker only
*reads* the result back to answer the request.

Path semantics are unchanged on purpose. ``gateway_common.paths`` resolves an
RPC path relative to the workspace root, and the sandbox sees that root at
``/home/user`` (``SandlockExecutor._view_cwd``), so the writer runs its helper
with the *root* as cwd and hands it root-relative paths. ``/foo`` therefore
still lands at ``<tree>/foo``, exactly where it landed before this change; the
SDK-visible semantics are not part of this change.

The one prerequisite is a shell in the image's rootfs (``/bin/sh``), which is
also what the sandbox's own commands need: every image this fleet runs is a
Debian- or Alpine-derived rootfs with one, and a scratch image could not run a
command at all. A write against an image without one fails loudly with the
image named, instead of quietly falling back to a second writer identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid
from collections.abc import AsyncIterable
from pathlib import Path

from envd_service.runtime.registry import state_clause
from gateway_common.errors import ConnectError, failed_precondition, invalid_argument
from gateway_common.paths import PathTraversalError, resolve_under_root
from gateway_common.upload import UploadTooLargeError

logger = logging.getLogger(__name__)

#: The interpreter the helpers run under. ``sh`` (not ``bash``) on purpose:
#: dash and busybox sh both run these snippets, and every image that can run a
#: sandbox command has one.
SHELL = "/bin/sh"

#: ``argv[0]`` for ``sh -c``. The snippets read their operands from positional
#: parameters, never from an interpolated string, so a path containing a quote
#: or a newline is just an argument (this is why the helpers take ``$1``/``$2``
#: instead of being built by string formatting).
ARGV0 = "e2b-write"

#: ``MakeDir``: the existence refusal is the API's (see
#: ``FilesystemOps.require_creatable``), so this is the operation itself.
_MAKE_DIR = "set -e\nmkdir -p -- \"$1\"\n"

#: ``Move``: ``mkdir -p`` on the destination's parent, which the worker-side
#: implementation did as well (``FilesystemOps.move``).
_MOVE = (
    "set -e\n"
    "mkdir -p -- \"$(dirname -- \"$2\")\"\n"
    "mv -- \"$1\" \"$2\"\n"
)

#: ``Remove``: ``rm -rf`` covers the file, symlink and directory cases in one
#: call, exactly like the ``unlink`` / ``shutil.rmtree`` pair it replaces.
_REMOVE = "set -e\nrm -rf -- \"$1\"\n"

#: A streamed write: create the parent directory, drain stdin into a sibling
#: temp file, then rename it over the target. The temp-and-rename is E4.2's
#: rule -- a body that exceeds the limit, or a sandbox-side ``cat`` that fails
#: midway, must not leave a half-written file in the caller's tree -- and doing
#: it inside the sandbox keeps that guarantee with the sandbox as the writer.
#:
#: ``set -e`` is what makes a failed ``cat`` abort before the rename; without
#: it the rename would publish the truncated bytes.
_WRITE = (
    "set -e\n"
    "mkdir -p -- \"$(dirname -- \"$1\")\"\n"
    "cat > \"$2\"\n"
    "mv -f -- \"$2\" \"$1\"\n"
)

#: Upload chunk size handed to the sandbox's stdin.
CHUNK_SIZE = 64 * 1024

#: How long a helper may take to *finish* once it has been handed everything
#: it needs (the last stdin byte, or nothing at all for the metadata-less
#: operations). The streaming phase is not covered: it is bounded by the
#: caller's own transfer, and a body that takes minutes to arrive is the
#: caller's business, not a hang.
COMPLETE_TIMEOUT_S = 60.0

#: How much of a helper's stderr is kept for the error message.
STDERR_TAIL_BYTES = 4096


class SandboxWriteError(ConnectError):
    """A helper ran but failed: the reason is the sandbox's own stderr."""


class SandboxWriter:
    """Runs the workspace's writes as the sandbox's own commands."""

    def __init__(self, context) -> None:
        self._ctx = context
        self._root = Path(context.record.workspace_dir).resolve()
        #: N28/B: the metadata xattr is best effort *per filesystem*, and this
        #: fleet's NAS answers ENOTSUP for the ``user.`` namespace -- once. One
        #: line per sandbox is diagnosis; one per upload would be noise.
        self._metadata_warned = False

    # -- public surface: the four writing RPCs -----------------------------

    async def make_dir(self, path: str) -> None:
        """``filesystem.Filesystem/MakeDir`` -- as the sandbox."""
        target = self._relative(self._ctx.files.require_creatable(path))
        await self._run(_MAKE_DIR, [target], operation="MakeDir")

    async def move(self, source: str, destination: str) -> None:
        """``filesystem.Filesystem/Move`` -- as the sandbox."""
        src, dst = self._ctx.files.require_movable(source, destination)
        await self._run(
            _MOVE,
            [self._relative(src), self._relative(dst)],
            operation="Move",
        )

    async def remove(self, path: str) -> None:
        """``filesystem.Filesystem/Remove`` -- as the sandbox."""
        target = self._ctx.files.require_removable(path)
        if target == self._root:
            # ``rm -rf .`` refuses anyway (GNU rm will not remove ``.``), and
            # the message that produces is not one to hand a caller. Removing
            # the workspace root through the files API is never a request the
            # platform can honour: the tree is the sandbox's home.
            raise invalid_argument("the sandbox root cannot be removed")
        await self._run(_REMOVE, [self._relative(target)], operation="Remove")

    async def write_stream(
        self,
        path: str | Path,
        chunks: AsyncIterable[bytes],
        *,
        limit_bytes: int | None,
    ) -> Path:
        """``POST /files`` -- stream ``chunks`` into ``path``, as the sandbox.

        Returns the resolved target, which is what the caller needs to build
        its response entry. Raises :class:`UploadTooLargeError` when
        ``limit_bytes`` is crossed (nothing is published: the temp file is
        removed) and ``invalid_argument`` for a path that leaves the root --
        the same two failures the worker-side sink raised, so the HTTP mapping
        above is unchanged.
        """
        target = self._resolve(path)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        rel_target = self._relative(target)
        rel_tmp = self._relative(tmp)

        proc = await self._start(_WRITE, [rel_target, rel_tmp], stdin=True)
        total = 0
        try:
            async for chunk in chunks:
                total += len(chunk)
                if limit_bytes is not None and total > limit_bytes:
                    raise UploadTooLargeError(limit_bytes)
                await self._feed(proc, chunk)
            self._close_stdin(proc)
            code, stderr = await self._await_end(proc)
        except BaseException:
            await self._abort(proc, rel_tmp)
            raise
        if code != 0:
            await self._abort(proc, rel_tmp)
            raise self._failed("Upload", code, stderr)
        return target

    async def persist_metadata(
        self, path: str | Path, metadata: dict[str, str]
    ) -> bool:
        """Best-effort ``user.e2b.*`` xattrs on ``path``, set by the sandbox.

        The worker cannot do it any more: the entry belongs to the sandbox's
        uid, and ``setxattr`` on ``user.*`` needs write access to the file --
        which is precisely the access the worker gave up. ``False`` means the
        metadata did not stick (no ``python3`` in the image, an NFS that
        refuses the namespace, a lost race with a concurrent delete); the
        upload itself still succeeded, which is the contract this had before
        the split as well (``_persist_metadata`` swallowed the same failures).
        """
        if not metadata:
            return True
        script = (
            "import os,sys;"
            "d=__import__('json').loads(sys.argv[1]);"
            "p=sys.argv[2];"
            "[os.setxattr(p,'user.e2b.'+k,v.encode()) for k,v in d.items()]"
        )
        try:
            await self._run(
                "exec python3 -c \"$3\" \"$1\" \"$2\"\n",
                [
                    json.dumps(metadata, separators=(",", ":")),
                    self._relative(self._resolve(path)),
                    script,
                ],
                operation="SetMetadata",
            )
        except Exception:
            if not self._metadata_warned:
                self._metadata_warned = True
                logger.warning(
                    "sandbox %s could not persist upload metadata on %s (the "
                    "file itself was written; this filesystem does not have to "
                    "support the user. namespace -- the fleet's NAS answers "
                    "ENOTSUP); further failures for this sandbox are debug",
                    self._ctx.record.sandbox_id,
                    path,
                    exc_info=True,
                )
            else:
                logger.debug(
                    "upload metadata still not persisted on %s", path
                )
            return False
        return True

    # -- the helper's lifecycle --------------------------------------------

    async def _run(
        self, script: str, args: list[str], *, operation: str
    ) -> tuple[int, bytes]:
        proc = await self._start(script, args, stdin=False)
        code, stderr = await self._await_end(proc)
        if code != 0:
            raise self._failed(operation, code, stderr)
        return code, stderr

    async def _start(self, script: str, args: list[str], *, stdin: bool):
        """Spawn one helper: ``sh -c <script> e2b-write <args...>``."""
        ctx = self._ctx
        env = dict(ctx.record.env_vars)
        # ``clean_env`` (sandlock) starts the child from an empty environment,
        # so PATH has to be handed to it explicitly or the wrappers the script
        # calls (mkdir/mv/rm/cat) cannot be resolved.
        env.setdefault(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        try:
            return await ctx.processes.start(
                cmd=[SHELL, "-c", script, ARGV0, *args],
                env=env,
                cwd=ctx.record.workspace_dir,
                stdin_enabled=stdin,
                internal=True,
                # N25/C: the upload itself runs inside the sandbox, so it is
                # subject to the same "what is left" ceiling as any other
                # command -- otherwise the one write the platform performs on
                # the caller's behalf would be the one that ignores the budget.
                max_file_size=ctx.max_file_size_for_exec(),
            )
        except Exception as exc:  # noqa: BLE001 - reported as a write failure
            raise SandboxWriteError(
                "internal",
                (
                    f"the sandbox could not start {SHELL} to perform this "
                    f"write; the image must carry a shell for the platform to "
                    f"write through it ({type(exc).__name__}: {exc})"
                ),
                500,
            ) from exc

    async def _feed(self, proc, chunk: bytes) -> None:
        """Hand one chunk to the helper's stdin, waiting for room (N28/C).

        Through the manager, not the process table: the manager is what knows
        the running process behind the pid, and a chunk that arrives after the
        helper died must raise rather than be dropped on the floor.
        """
        await self._ctx.processes.feed_stdin(proc.pid, chunk)

    def _close_stdin(self, proc) -> None:
        """Deliver EOF so ``cat`` finishes (see ``_feed``: same reason)."""
        self._ctx.processes.close_stdin(proc.pid)

    async def _await_end(self, proc) -> tuple[int, bytes]:
        """Drain the helper to its end event; return ``(exit code, stderr)``."""
        queue = proc.subscribe(replay=False)
        stderr = bytearray()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=COMPLETE_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    self._kill(proc)
                    raise SandboxWriteError(
                        "deadline_exceeded",
                        (
                            "the sandbox did not finish the write within "
                            f"{COMPLETE_TIMEOUT_S:g}s; it has been killed"
                        ),
                        504,
                    ) from None
                if item[0] == "data":
                    if item[1] == "stderr":
                        stderr.extend(item[2])
                        if len(stderr) > STDERR_TAIL_BYTES:
                            del stderr[: len(stderr) - STDERR_TAIL_BYTES]
                elif item[0] == "end":
                    return int(item[1]), bytes(stderr)
        finally:
            proc.unsubscribe(queue)

    async def _abort(self, proc, tmp_rel: str | None) -> None:
        """Stop the helper and take its temp file with it (best effort).

        A killed ``cat`` leaves the partial temp file behind -- the script's
        own cleanup only runs on the paths the script itself reaches -- so the
        remove has to happen as a second sandbox command. Its failure is
        logged, never raised: the caller is already being told why its write
        failed, and losing that for a cleanup errno would be worse.
        """
        try:
            self._kill(proc)
        except Exception:  # pragma: no cover - defensive
            pass
        if tmp_rel is None:
            return
        try:
            await self._run(_REMOVE, [tmp_rel], operation="Cleanup")
        except Exception:  # noqa: BLE001 - see the docstring
            logger.warning(
                "sandbox %s left a partial upload temp file %s behind",
                self._ctx.record.sandbox_id,
                tmp_rel,
                exc_info=True,
            )

    def _failed(self, operation: str, code: int, stderr: bytes) -> ConnectError:
        detail = stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = f"the sandbox's {SHELL} exited {code} with no output"
        state = getattr(self._ctx.record, "state", "running")
        if code < 0 and state != "running":
            # Killed by a signal while its sandbox left ``running``: the pause
            # is the cause, and -- unlike a helper that failed on its own --
            # it is the caller's to fix (resume and retry). Answering with the
            # state (and the platform's reason for it) is what keeps a write
            # that *raced* the pause from surfacing as an unexplained 500.
            return failed_precondition(
                (
                    f"{state_clause(self._ctx.record, state)}; the {operation} "
                    "was interrupted by the pause rather than frozen "
                    "(resume it and retry)"
                ),
                http_status=409,
            )
        return SandboxWriteError(
            "internal",
            f"{operation} failed inside the sandbox: {detail}",
            500,
        )

    def _kill(self, proc) -> None:
        """SIGKILL the helper through the manager (never a bare ``proc.kill``).

        Going through ``ProcessManager`` is what keeps the process table
        honest: a killed helper is dropped from it at once, so a later
        ``kill_all``/pause walk cannot signal a pid that has been reused.
        """
        try:
            self._ctx.processes.send_signal(proc.pid, signal.SIGKILL)
        except ConnectError:
            pass

    # -- paths --------------------------------------------------------------

    def _resolve(self, path: str | Path) -> Path:
        """Resolve an RPC path under the root (``invalid_argument`` on escape).

        A ``Path`` is taken as already resolved by the caller (the HTTP layer
        resolves first so it can answer a traversal with its own 400); it is
        still checked against the root, because "already resolved" is the
        caller's claim, not a fact this module should assume.
        """
        if isinstance(path, Path):
            target = path.resolve()
            if target != self._root and self._root not in target.parents:
                raise invalid_argument(f"path escapes the sandbox root: {path}")
            return target
        try:
            return resolve_under_root(self._root, path)
        except PathTraversalError as e:
            raise invalid_argument(str(e)) from e

    def _relative(self, target: Path) -> str:
        """``target`` as the sandbox's helper sees it: relative to its cwd."""
        return target.relative_to(self._root).as_posix()


__all__ = ["SandboxWriter", "SandboxWriteError", "SHELL"]
