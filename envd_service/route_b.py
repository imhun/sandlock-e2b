# SPDX-License-Identifier: Apache-2.0
"""route-B slot pool for the sandlock executor (backlog #5 / T5).

Route B runs one ``sandlock-supervise`` process per sandbox at the sandbox's
host uid, so path mediation (the ``fs_denied`` carve-out family) executes as
that uid and DAC ownership is correct by construction.  This module is the
envd-side **W1** slot manager (see
``docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md``):

* a fixed, non-overlapping uid segment (``uid_start..uid_start+size``);
* one uid = one supervise process = one sandbox generation; recycling a uid
  means restarting the process in place (W1), never re-using a live slot;
* the uid reuse window is the number of concurrently live slots.

Three layers live here, and the split is deliberate:

* :class:`W1SlotPool` owns the slot lifecycle only: publish the generation's
  startup documents at modes the slot can actually read, spawn it at a given
  uid, wait until the registered channel answers ``stats``, and ``shutdown``
  on release.  It knows nothing about exec semantics;
* :func:`supervise_policy_document` is the one-way translation from the
  executor's policy-ceiling kwargs to the full-field ``--policy`` document,
  and :data:`PARKING_PROGRAM` is the generation's M0 (envd instances have no
  main-program concept, so the main process is a shell that stops itself);
* :class:`RouteBInstance` / :class:`RouteBExecProcess` give the executor a
  ``SandboxInstance``/``ExecProcess``-shaped client over the slot verbs, so
  ``SandlockExecutor`` keeps exactly one code path for both backends.

Spawning a slot at another uid needs privilege (root / CAP_SETUID).  The
default spawner works when the caller is root (the privileged test runner /
local combined worker) and wraps supervise in util-linux ``setpriv`` (the
same shape the fork root-phase suites use); production deployments
should inject a launcher-based spawner (setuid helper, k8s ``runAsUser`` pod
creator, or an external W1 slot fleet) through ``spawner=`` — the pool never
assumes how the process got its uid, only that ``supervise --uid X`` will
self-check it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


try:  # the native module is Linux-only; the shim stays importable off-Linux
    from sandlock.exceptions import SandboxError, SandlockError
except Exception:  # pragma: no cover - macOS dev / missing wheel

    class SandlockError(RuntimeError):  # type: ignore[no-redef]
        """Stand-in for the native transport/refusal error class."""

    class SandboxError(RuntimeError):  # type: ignore[no-redef]
        """Stand-in for the native served-refusal error class."""


def _fnv1a_hex(name: str) -> str:
    """Mirror of ``sandlock_core::control::fnv1a_hex`` (64-bit FNV-1a)."""
    h = 0xCBF29CE484222325
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def default_supervise_bin() -> Path:
    """The supervise binary shipped inside the sandlock wheel."""
    import sandlock

    return Path(sandlock.__file__).resolve().parent / "bin" / "sandlock-supervise"


def _spawn_slot(
    supervise_bin: Path,
    uid: int,
    policy_path: Path,
    program_path: Path,
    name: str,
    token: str,
    worker_uid: int,
    stdout,
    stderr,
) -> subprocess.Popen:
    if os.geteuid() != 0:
        raise PermissionError(
            "route-B slots need a privileged starter (root / CAP_SETUID); "
            "a non-root worker cannot run sandlock-supervise at another uid "
            "(route-A fixed-uid fallback applies)"
        )
    env = dict(os.environ)
    # Force the per-uid default registry root (/tmp/sandlock-ctl-<uid>-registry)
    # so the worker-side socket path formula is deterministic regardless of
    # any inherited SANDBOX_CTL_ROOT test override.
    env.pop("SANDBOX_CTL_ROOT", None)
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        raise RuntimeError("route-B slot spawn needs util-linux setpriv")
    argv = [
        setpriv,
        "--reuid",
        str(uid),
        "--regid",
        str(uid),
        "--clear-groups",
        "--",
        str(supervise_bin),
        "--policy",
        str(policy_path),
        "--uid",
        str(uid),
        "--serve-path",
        name,
        "--token",
        token,
        "--peer-uid",
        str(worker_uid),
        "--program",
        str(program_path),
    ]
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=env,
    )


@dataclass
class SlotHandle:
    """A live route-B slot leased to one sandbox."""

    sandbox_id: str
    uid: int
    name: str
    token: str
    sock_path: Path
    policy_path: Path
    program_path: Path
    process: subprocess.Popen
    #: The generation's launched instance pid (from the slot's first
    #: ``stats`` reply); ``None`` only when the fleet was built by a caller
    #: that does not probe readiness.
    instance_pid: int | None = None

    @property
    def stderr(self) -> str:
        if self.process.stderr is None:
            return ""
        try:
            return self.process.stderr.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - diagnostic only
            return ""


# ------------------------------------------------------------------ slots


def _registry_sock_path(uid: int, name: str) -> Path:
    """The registered-path socket of slot ``name`` at ``uid``.

    Mirrors fork ``control.rs``: registry root ``/tmp/sandlock-ctl-<uid>-registry``
    (the per-uid isolation of the shared 1777+sticky registry) plus the
    ``<fnv1a16(name)>.d/control.sock`` hashed entry.
    """
    return Path(
        f"/tmp/sandlock-ctl-{uid}-registry/{_fnv1a_hex(name)}.d/control.sock"
    )


#: How long a route-B child may stay visible in procfs before its waiter
#: gives up polling and uses the slot-blocking ``wait_child`` verb.
CHILD_POLL_CAP_S = 300.0

#: The generation's parking main program (M0).
#:
#: ``--program`` is mandatory for a slot that must serve ``exec`` (launch-first
#: is what brings the instance up), but an envd sandbox has no main program:
#: its lifetime is the slot's. So the "workload" is a park that must never
#: exit (main exit collapses the whole generation) and must never cost
#: anything. ``kill -STOP $$`` re-stops the shell right after every SIGCONT, so
#: the steady state is one stopped shell: zero CPU, zero growth.
#:
#: ``read x < /dev/zero`` is the obvious candidate and is wrong: an exec
#: session's main stdio is wired to ``/dev/null`` by core
#: (``sandlock-core/src/instance.rs``, ``launch_exec_inner``), so the redirect
#: is the only input, and ``read`` never sees a newline -- both dash (one
#: ``read(2)`` per byte) and bash (NULs discarded, line never terminated) spin
#: on it at 100 % CPU for the life of the sandbox.
PARKING_SCRIPT = "while :; do kill -STOP $$; done"

PARKING_PROGRAM: dict[str, list[str]] = {
    "argv": ["/bin/sh", "-c", PARKING_SCRIPT]
}


def default_channel_factory(path: str, token: str):
    """The fork's worker-side client for a registered slot (F16)."""
    from sandlock.supervise import SuperviseChannel

    return SuperviseChannel(path, token)


class W1SlotPool:
    """Fixed-uid route-B slot fleet (W1 recycle semantics).

    Slots are leased **by uid**: a sandbox's route-B mediator must run as that
    sandbox's own host uid (that identity is the whole point of route B -- see
    ``docs/supervise-identity-handoff.md`` §5), so ``acquire(..., uid=X)``
    targets the uid the worker's host-uid pool allocated for the sandbox. A uid
    with a live slot is never handed out twice; recycle = restart in place.

    Acquisition and release block (spawn + wait for the registered socket +
    the slot's first ``stats`` reply), so the async callers use
    :meth:`acquire` / :meth:`release`, which run them on a worker thread.
    """

    def __init__(
        self,
        *,
        uid_start: int,
        size: int,
        worker_uid: int | None = None,
        tmp_root: Path | None = None,
        supervise_bin: Path | None = None,
        spawner: Callable[..., subprocess.Popen] | None = None,
        channel_factory: Callable[[str, str], object] | None = None,
        socket_timeout_s: float = 30.0,
    ) -> None:
        if size < 1:
            raise ValueError("route-B slot pool size must be >= 1")
        self._uids = list(range(uid_start, uid_start + size))
        self._worker_uid = worker_uid if worker_uid is not None else os.geteuid()
        self._tmp_root = tmp_root or Path("/tmp/sandlock-route-b")
        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self._supervise_bin = supervise_bin or default_supervise_bin()
        self._spawner = spawner or (
            lambda **kw: _spawn_slot(
                self._supervise_bin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **kw,
            )
        )
        self.channel_factory = channel_factory or default_channel_factory
        self._socket_timeout_s = socket_timeout_s
        # Guards the uid ledger (``_slots`` / ``_free``): the executor calls
        # in from worker threads (``asyncio.to_thread``) and from lifecycle
        # paths, so the pop/append pair must not interleave.
        self._ledger = threading.Lock()
        self._slots: dict[str, SlotHandle] = {}
        self._free: list[int] = list(self._uids)

    @property
    def live_slots(self) -> list[SlotHandle]:
        with self._ledger:
            return list(self._slots.values())

    def acquired_uid(self, sandbox_id: str) -> int | None:
        with self._ledger:
            slot = self._slots.get(sandbox_id)
        return slot.uid if slot is not None else None

    def slot(self, sandbox_id: str) -> SlotHandle | None:
        with self._ledger:
            return self._slots.get(sandbox_id)

    def _take_uid_locked(self, sandbox_id: str, uid: int | None) -> int:
        """Pick (and reserve) the uid for a new slot. Caller holds ``_ledger``."""
        if sandbox_id in self._slots:
            raise ValueError(
                f"sandbox {sandbox_id} already holds a route-B slot "
                "(one slot per sandbox; release it first)"
            )
        if uid is None:
            if not self._free:
                raise RuntimeError(
                    "route-B slot pool exhausted (all uids live); "
                    "raise E2B_ROUTE_B_SLOTS or wait for a release"
                )
            chosen = self._free.pop(0)
        else:
            if uid not in self._uids:
                raise ValueError(
                    f"route-B uid {uid} for sandbox {sandbox_id} is outside the "
                    f"slot segment {self._uids[0]}..{self._uids[-1]}"
                )
            live = {slot.uid for slot in self._slots.values()}
            if uid in live:
                raise RuntimeError(
                    f"route-B uid {uid} already has a live slot "
                    f"(sandbox {self._slot_for_uid_locked(uid)}); W1 recycles a "
                    "uid only by restarting its process, never by sharing it"
                )
            if uid not in self._free:
                raise RuntimeError(
                    f"route-B uid {uid} is not available (already leased or "
                    "released-but-not-returned)"
                )
            chosen = uid
            self._free.remove(uid)
        return chosen

    def _slot_for_uid_locked(self, uid: int) -> str | None:
        for slot in self._slots.values():
            if slot.uid == uid:
                return slot.sandbox_id
        return None

    def _return_uid_locked(self, uid: int) -> None:
        if uid not in self._free:
            self._free.append(uid)
            self._free.sort()

    def acquire_sync(
        self,
        sandbox_id: str,
        policy_json: dict,
        program_json: dict | None = None,
        *,
        uid: int | None = None,
        name: str | None = None,
    ) -> SlotHandle:
        """Lease a uid (``uid`` when given, else the least-recently-freed) and
        start its slot.

        ``program_json`` defaults to :data:`PARKING_PROGRAM`. Returns only once
        the slot's registered socket answers ``stats`` with a launched
        instance, so a caller can ``exec`` immediately.
        """
        with self._ledger:
            uid = self._take_uid_locked(sandbox_id, uid)
        program = program_json or PARKING_PROGRAM
        slot_name = name or f"rb-{sandbox_id}"
        token = secrets.token_hex(32)
        uid_dir = self._tmp_root / str(uid)
        slot_dir = uid_dir / slot_name
        slot_dir.mkdir(parents=True, exist_ok=True)
        policy_path = slot_dir / "policy.json"
        program_path = slot_dir / "program.json"
        self._write_slot_documents(self._tmp_root, uid_dir, slot_dir,
                                   policy_path, program_path, policy_json,
                                   program, uid, slot_name)
        sock_path = _registry_sock_path(uid, slot_name)

        def _start() -> SlotHandle:
            # A socket nobody listens on (a slot this pool had to kill, or a
            # crashed generation) would make the readiness wait below succeed
            # against nothing, so clear it before the spawn. Safe to do: the
            # only way another *live* process owns this path is a second
            # worker leasing the same uid, and the persistent host-uid pool
            # (``envd_service/uid_pool.py``) is what refuses that -- route B's
            # segment *is* that pool's segment.
            if sock_path.exists():
                try:
                    sock_path.unlink()
                    logger.warning(
                        "route-B slot %s: removed stale socket %s",
                        slot_name,
                        sock_path,
                    )
                except OSError as e:
                    raise RuntimeError(
                        f"route-B slot {slot_name}: stale socket {sock_path} "
                        f"cannot be removed: {e}"
                    ) from e
            process = self._spawner(
                uid=uid,
                policy_path=policy_path,
                program_path=program_path,
                name=slot_name,
                token=token,
                worker_uid=self._worker_uid,
            )
            deadline = time.monotonic() + self._socket_timeout_s
            while time.monotonic() < deadline:
                if sock_path.exists():
                    # The slot binds *before* launching the instance
                    # (main.rs: the worker may connect while the instance is
                    # coming up), so the socket alone does not mean "exec
                    # works". ``stats`` is served by the same single-threaded
                    # accept loop, so it blocks until the generation is up --
                    # one request is both the readiness probe and the proof
                    # that the token/path work.
                    reply = self._ready_probe(
                        sock_path,
                        token,
                        process,
                        deadline,
                        self.channel_factory,
                    )
                    return SlotHandle(
                        sandbox_id=sandbox_id,
                        uid=uid,
                        name=slot_name,
                        token=token,
                        sock_path=sock_path,
                        policy_path=policy_path,
                        program_path=program_path,
                        process=process,
                        instance_pid=reply.get("pid"),
                    )
                if process.poll() is not None:
                    err = ""
                    if process.stderr is not None:
                        err = process.stderr.read().decode("utf-8", "replace")
                    raise SlotDeadError(
                        f"route-B slot {slot_name} (uid {uid}) exited before "
                        f"binding {sock_path}: {err}"
                    )
                time.sleep(0.05)
            process.kill()
            raise SlotDeadError(
                f"route-B slot {slot_name} (uid {uid}) did not bind "
                f"{sock_path} within {self._socket_timeout_s}s"
            )

        try:
            handle = _start()
        except BaseException:
            with self._ledger:
                self._return_uid_locked(uid)
            raise
        with self._ledger:
            self._slots[sandbox_id] = handle
        logger.info(
            "route-B slot %s leased uid %d (sandbox %s, instance pid %s)",
            handle.name,
            uid,
            sandbox_id,
            handle.instance_pid,
        )
        return handle

    def _write_slot_documents(
        self,
        root: Path,
        uid_dir: Path,
        slot_dir: Path,
        policy_path: Path,
        program_path: Path,
        policy_json: dict,
        program: dict,
        uid: int,
        slot_name: str,
    ) -> None:
        """Publish the startup documents so **the slot can read them**.

        The lease is written by the worker (root) and read by a process whose
        euid is ``uid`` after ``setpriv``, so neither the ambient umask nor a
        private scratch root may decide the modes: a 0700 parent (the default
        under a pytest ``tmp_path``, and under umask 077 anywhere) makes the
        spawn fail with ``Permission denied`` on the policy read. Directories
        therefore go 0755/0711 -- listable by nobody below the root, traversed
        by name -- and the documents become 0440 ``root:<uid>``, which is the
        only mode that is both readable by the slot and closed to every other
        tenant: the policy carries egress-proxy credentials and secret paths.
        """
        for path, mode in (
            (root, 0o755),
            (uid_dir, 0o711),
            (slot_dir, 0o711),
        ):
            os.chmod(path, mode)
        for path, payload in (
            (policy_path, json.dumps(policy_json)),
            (program_path, json.dumps(program)),
        ):
            if path.exists():
                # A W1 restart rewrites the same lease document; the previous
                # generation left it read-only.
                os.chmod(path, 0o600)
            with open(path, "w", encoding="utf-8") as document:
                document.write(payload)
            try:
                os.chown(path, -1, uid)
            except PermissionError:
                # A worker that cannot chown (an unprivileged pool attached to
                # an externally started fleet) has no way to scope the group;
                # say so instead of shipping a silently world-readable policy.
                logger.warning(
                    "route-B slot %s: cannot scope %s to gid %d from this "
                    "worker; the document is world-readable at mode 0444 "
                    "(run the worker as root or place E2B_ROUTE_B_TMP_ROOT on "
                    "storage the slots own)",
                    slot_name,
                    path.name,
                    uid,
                )
                os.chmod(path, 0o444)
            else:
                os.chmod(path, 0o440)

    def _ready_probe(
        self,
        sock_path: Path,
        token: str,
        process: subprocess.Popen,
        deadline: float,
        factory: Callable[[str, str], object],
    ) -> dict:
        """Block until the slot answers ``stats`` with a launched instance.

        A refused/broken connection is normal here (the generation is still
        launching, and the socket is bound before that happens), so it is only
        *reported* -- as the last error seen -- if the deadline runs out.
        """
        last_error: str | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                err = ""
                if process.stderr is not None:
                    err = process.stderr.read().decode("utf-8", "replace")
                raise SlotDeadError(
                    f"route-B slot at {sock_path} exited before its first "
                    f"reply: {err}"
                )
            try:
                with factory(str(sock_path), token) as ch:
                    stats = ch.request("stats")
            except Exception as e:  # noqa: BLE001 - instance still launching
                last_error = f"{type(e).__name__}: {e}"
                time.sleep(0.05)
                continue
            if isinstance(stats, dict) and stats.get("launched"):
                return stats
            time.sleep(0.05)
        process.kill()
        raise SlotDeadError(
            f"route-B slot at {sock_path} never reported a launched instance "
            f"within {self._socket_timeout_s}s"
            + (f" (last channel error: {last_error})" if last_error else "")
        )

    async def acquire(
        self,
        sandbox_id: str,
        policy_json: dict,
        program_json: dict | None = None,
        *,
        uid: int | None = None,
        name: str | None = None,
    ) -> SlotHandle:
        return await asyncio.to_thread(
            self.acquire_sync,
            sandbox_id,
            policy_json,
            program_json,
            uid=uid,
            name=name,
        )

    def release_sync(self, sandbox_id: str) -> None:
        """End the generation leased to ``sandbox_id`` (uid back to the pool).

        The ledger lock is held across the teardown on purpose: returning the
        uid before its process is gone would let the next ``acquire`` restart
        that uid while the old slot is still alive -- the one thing W1
        forbids (one uid = one live supervise).
        """
        with self._ledger:
            handle = self._slots.pop(sandbox_id, None)
            if handle is not None:
                self._retire_locked(handle)

    def retire(self, handle: SlotHandle) -> None:
        """Tear one slot down and return its uid. Idempotent.

        Both teardown entry points converge here: the sandbox lifecycle
        releases by ``sandbox_id``, while a :class:`RouteBInstance` releases by
        the handle it holds -- and that handle outlives its ledger entry
        either way, so the second caller must neither warn nor double-kill.
        """
        with self._ledger:
            if self._slots.get(handle.sandbox_id) is handle:
                del self._slots[handle.sandbox_id]
            self._retire_locked(handle)

    def _retire_locked(self, handle: SlotHandle) -> None:
        """Teardown core; caller holds ``_ledger``."""
        if handle.process.poll() is None:
            try:
                with self.channel_factory(str(handle.sock_path), handle.token) as ch:
                    ch.request("shutdown")
            except Exception as e:  # noqa: BLE001 - best-effort teardown
                logger.warning(
                    "route-B shutdown for %s failed (%s); killing slot pid %s",
                    handle.sandbox_id,
                    e,
                    handle.process.pid,
                )
        try:
            handle.process.wait(20)
        except subprocess.TimeoutExpired:
            logger.warning(
                "route-B slot %s (pid %s) ignored shutdown; sending SIGKILL",
                handle.name,
                handle.process.pid,
            )
            handle.process.kill()
            try:
                handle.process.wait(10)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable
                logger.error(
                    "route-B slot %s (pid %s) survived SIGKILL; uid %d stays "
                    "reserved (this pool no longer owns it)",
                    handle.name,
                    handle.process.pid,
                    handle.uid,
                )
                return
        self._return_uid_locked(handle.uid)
        logger.info("route-B slot %s released uid %d", handle.name, handle.uid)

    async def release(self, sandbox_id: str) -> None:
        await asyncio.to_thread(self.release_sync, sandbox_id)


class SlotDeadError(RuntimeError):
    """A slot process is gone / never came up.

    Named ``... dead ...`` so the executor's rebuild-once path (idle or dead
    instance surfaced at exec time) treats it exactly like the in-process
    ``SandboxInstance`` failures.
    """


# ------------------------------------------------------------------ policy wire

#: The supervise ``--policy`` wire field set (mirror of fork
#: ``sandlock-supervise/src/policy.rs::POLICY_FIELDS``; the unit test
#: ``test_supervise_policy_fields_match_the_fork_wire`` pins it against the
#: Rust source, so a new fork field cannot be silently dropped here).
SUPERVISE_POLICY_FIELDS: frozenset[str] = frozenset(
    {
        "allow_degraded",
        "chroot",
        "clean_env",
        "cpu_cores",
        "cwd",
        "deterministic_dirs",
        "disable",
        "egress_proxy",
        "env",
        "extra_allow_syscalls",
        "extra_deny_syscalls",
        "fd_inject_connect",
        "fs_denied",
        "fs_mount",
        "fs_readable",
        "fs_storage",
        "fs_writable",
        "gid",
        "gpu_devices",
        "host_mask",
        "http_allow",
        "http_ca",
        "http_ca_out",
        "http_deny",
        "http_inject",
        "http_inject_ca",
        "http_key",
        "http_ports",
        "max_cpu",
        "max_disk",
        "max_memory",
        "max_open_files",
        "max_processes",
        "mediation_run_as",
        "net_allow",
        "net_allow_bind",
        "net_deny",
        "net_deny_bind",
        "net_isolation",
        "no_coredump",
        "no_huge_pages",
        "no_randomize_memory",
        "notify_rate_limit",
        "num_cpus",
        "on_error",
        "on_exit",
        "pid_ns",
        "port_mappings",
        "port_remap",
        "random_seed",
        "time_start",
        "uid",
        "workdir",
    }
)


def _mount_specs(mounts: dict | list) -> list[str]:
    """``fs_mount`` mapping -> wire ``VIRTUAL:HOST[:ro]`` specs.

    The Python builder API takes pairs (``_sdk.py`` calls
    ``sandlock_sandbox_builder_fs_mount(virtual, host)`` per entry); the
    supervise wire is the profile spelling parsed by
    ``sandlock_core::profile::parse_mount_spec``.
    """
    if isinstance(mounts, list):
        return [str(spec) for spec in mounts]
    return [f"{virtual}:{host}" for virtual, host in mounts.items()]


def supervise_policy_document(ceiling: dict) -> dict:
    """Turn the executor's policy-ceiling kwargs into a ``--policy`` document.

    Same fields, wire spellings only: ``fs_mount`` becomes the spec list, the
    ``None`` values the Python builder treats as "unset" are dropped (an
    explicit ``null`` is an unknown *value* on the wire, not an omission), and
    ``mediation_run_as`` is dropped because a route-B slot never uses the
    supervisor downgrade tier -- its mediator is this uid, which is the whole
    point of the route. Anything the wire does not know is refused by name
    before the slot is spawned, rather than by a fork that fails closed
    later.
    """
    doc: dict = {}
    for key, value in ceiling.items():
        if value is None:
            continue
        if key == "mediation_run_as":
            continue
        if key == "fs_mount":
            doc[key] = _mount_specs(value)
            continue
        doc[key] = value
    unknown = sorted(set(doc) - SUPERVISE_POLICY_FIELDS)
    if unknown:
        raise ValueError(
            "route-B policy ceiling carries field(s) the supervise wire does "
            f"not accept: {', '.join(unknown)}"
        )
    return doc


# ------------------------------------------------------------------ instance shim


class RouteBExecProcess:
    """A child of a route-B slot, shaped like the fork's ``ExecProcess``.

    The executor's ``SandlockRunningProcess`` only uses ``pid``,
    ``child_id``, ``stdin``/``stdout``/``stderr``/``pty`` file objects,
    ``wait()``, ``kill()`` and ``resize()``; those are reproduced here with the
    slot's verbs (``exec`` / ``wait_child`` / ``kill_child``) and
    worker-owned stdio. The stdio ends the child received were duplicated
    into the slot by ``SCM_RIGHTS``, so the worker closes its own copies right
    after the request -- otherwise the worker would keep its own pipes' write
    ends open and never see EOF.
    """

    def __init__(
        self,
        *,
        instance: "RouteBInstance",
        child_id: int,
        pid: int,
        argv: list[str],
        stdin=None,
        stdout=None,
        stderr=None,
        pty=None,
    ) -> None:
        self._instance = instance
        self._child_id = child_id
        self._pid = pid
        self.argv = argv
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pty = pty
        self._result = None
        self._pty_master_fd = pty.fileno() if pty is not None else None

    @property
    def pid(self) -> int | None:
        return self._pid if self._result is None else None

    @property
    def child_id(self) -> int:
        return self._child_id

    def _verb(self, verb: str, args: dict):
        return self._instance.request(verb, args)

    def wait(self, timeout: float | None = None) -> object:
        """Block until the child's exit status is back (idempotent).

        A registered slot serves **one request at a time** (the accept loop in
        ``sandlock-supervise``), so a ``wait_child`` issued while the child is
        still running would park the whole generation: no other command could
        exec, and no other exit could be collected. Waiting for the child's
        host pid to disappear first keeps the slot free; the fork buffers an
        exit that nobody waited for (``drain_early_exits``), so the status is
        still there afterwards. The poll is bounded -- if the pid stays
        visible past :data:`CHILD_POLL_CAP_S` (a pid we cannot observe, a
        hidden procfs) we fall back to the blocking verb, because a correct
        answer beats a polite one.
        """
        from types import SimpleNamespace

        if self._result is not None:
            return self._result
        self._wait_pid_gone(timeout)
        status = self._verb("wait_child", {"child_id": self._child_id})
        code = status.get("code")
        # Mirror the FFI exit-code surface (``sandlock_result_exit_code`` =
        # ``ExitStatus::code().unwrap_or(-1)``): a signal/kill/timeout reports
        # -1, never a synthetic 128+N.
        self._result = SimpleNamespace(
            exit_code=code if code is not None else -1,
            signal=status.get("signal"),
            killed=bool(status.get("killed")),
            timed_out=bool(status.get("timed_out")),
            stdout=None,
            stderr=None,
        )
        return self._result

    def _child_alive(self) -> bool:
        """Whether the child's **host** pid is still in procfs.

        ``exec`` reports a host pid (the slot's announced-pid translation), so
        the worker can watch it directly; off-Linux (and with no procfs) the
        probe reports "gone" and the verb path is used unchanged.
        """
        return os.path.exists(f"/proc/{self._pid}")

    def _wait_pid_gone(self, timeout: float | None) -> None:
        cap = time.monotonic() + CHILD_POLL_CAP_S
        deadline = cap if timeout is None else min(cap, time.monotonic() + timeout)
        while self._child_alive():
            if self._instance.slot_dead():
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def kill(self, sig: int = 9) -> None:
        """Deliver ``sig`` to the child's registered group (default SIGKILL).

        The in-process ``ExecProcess.kill()`` is SIGKILL-only; over the slot
        ``kill_child`` carries the signal number, so a pause/resume fallback
        gets a signal that genuinely arrives.
        """
        if self._result is not None:
            return
        self._verb("kill_child", {"child_id": self._child_id, "signum": int(sig)})

    def resize(self, rows: int, cols: int) -> None:
        """``TIOCSWINSZ`` on the worker-side pty master (PTY exec only)."""
        import fcntl
        import struct
        import termios

        if self._pty_master_fd is None:
            raise RuntimeError("child was exec'd without a pty")
        fcntl.ioctl(
            self._pty_master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", int(rows), int(cols), 0, 0),
        )

    def close(self) -> None:
        """Close the worker's own stdio ends (idempotent)."""
        for stream in (self.stdin, self.stdout, self.stderr, self.pty):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass


class RouteBInstance:
    """``SandboxInstance``-shaped client for one route-B slot.

    ``exec`` / ``update_network`` / ``close`` mean the slot's verbs; the
    executor therefore keeps one code path for both backends. The channel is
    the fork's F16 client, which opens a fresh connection per verb and keeps
    only the (path, token) identity between calls, so sharing one instance
    across the executor's threads is safe.
    """

    def __init__(
        self,
        *,
        pool: W1SlotPool,
        handle: SlotHandle,
        name: str | None = None,
        channel_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        self._pool = pool
        self._handle = handle
        self._channel_factory = channel_factory or pool.channel_factory
        self.name = name or handle.name
        self.policy = None
        self._channel = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._handle.instance_pid

    @property
    def sock_path(self) -> Path:
        return self._handle.sock_path

    def slot_dead(self) -> bool:
        """True once the slot process is gone (a verb cannot come back)."""
        return self._closed or self._handle.process.poll() is not None

    def request(self, verb: str, args: dict | None = None, fds=()):
        if self._closed:
            raise RuntimeError(
                f"route-B instance {self.name} is closed (slot released)"
            )
        if self._handle.process.poll() is not None:
            raise SlotDeadError(
                f"route-B instance {self.name} is dead: slot "
                f"{self._handle.name} (pid {self._handle.process.pid}) exited"
            )
        try:
            ch = self._channel
            if ch is None:
                ch = self._channel_factory(
                    str(self._handle.sock_path), self._handle.token
                )
                self._channel = ch
            return ch.request(verb, args, fds=tuple(fds))
        except SandboxError:
            # A served ``ok:false`` answer: the slot is alive and *refused*
            # this verb (unknown child, execvp failure, ceiling conflict).
            # It must not read as a dead instance -- ``SandboxError`` is a
            # subclass of ``SandlockError`` in this fork, so the order of the
            # two handlers below is load-bearing.
            raise
        except (SandlockError, OSError, AttributeError) as exc:
            # Transport level: the slot stopped answering (killed, crashed, or
            # its socket removed). Same treatment as a dead in-process
            # instance -- the executor rebuilds once.
            #
            # ``OSError``/``AttributeError`` are listed because the F16 client's
            # own error path cannot build its exception (it hands
            # ``ctypes.byref(...)`` to a helper that dereferences ``.contents``,
            # so a refused connect surfaces as ``AttributeError`` and hides the
            # server's text). Classified here rather than propagated as an
            # opaque error; registered as fork issue SL-9.
            raise SlotDeadError(
                f"route-B instance {self.name} is dead: verb "
                f"{verb!r} lost the slot: {type(exc).__name__}: {exc}"
            ) from exc

    def exec(
        self,
        cmd,
        stdio=None,
        *,
        cwd: str | None = None,
        env: dict | None = None,
        clean_env: bool = False,
        extra_writable=None,
        bind_ports=None,
    ) -> RouteBExecProcess:
        """Exec ``cmd`` on the slot with worker-side stdio (PIPED or PTY).

        ``stdio`` is accepted for signature compatibility with
        ``SandboxInstance.exec`` (``ExecStdio.PIPED`` / ``PTY``); the ends are
        built here because on this route the *worker* owns them and hands the
        child ends over as ``SCM_RIGHTS``.
        """
        argv = [str(arg) for arg in cmd]
        if not argv:
            raise ValueError("exec requires a non-empty argv sequence")
        args: dict = {"argv": argv}
        if cwd is not None:
            args["cwd"] = str(cwd)
        if env:
            args["env"] = dict(env)
        if clean_env:
            args["clean_env"] = True
        if extra_writable:
            args["extra_writable"] = [str(p) for p in extra_writable]
        if bind_ports:
            args["bind_ports"] = [int(p) for p in bind_ports]

        pty_mode = _stdio_is_pty(stdio)
        master = stdout_reader = stderr_reader = stdin_writer = None
        slave_fd = None
        child_stdin = child_stdout = child_stderr = None
        if pty_mode:
            master_fd, slave_fd = os.openpty()
            child_stdin = child_stdout = child_stderr = slave_fd
            master = os.fdopen(master_fd, "r+b", buffering=0)
        else:
            child_stdin, stdin_write = os.pipe()
            stdout_reader_fd, child_stdout = os.pipe()
            stderr_reader_fd, child_stderr = os.pipe()
            stdin_writer = os.fdopen(stdin_write, "wb", buffering=0)
            stdout_reader = os.fdopen(stdout_reader_fd, "rb", buffering=0)
            stderr_reader = os.fdopen(stderr_reader_fd, "rb", buffering=0)

        try:
            reply = self.request(
                "exec", args, fds=(child_stdin, child_stdout, child_stderr)
            )
        except BaseException:
            for fd in dict.fromkeys(
                f
                for f in (slave_fd, child_stdin, child_stdout, child_stderr)
                if f is not None
            ):
                _close_fd(fd)
            if master is not None:
                master.close()
            for stream in (stdin_writer, stdout_reader, stderr_reader):
                if stream is not None:
                    stream.close()
            raise

        # The child ends are now duplicated inside the slot; holding our copies
        # open would keep our own pipes from ever reporting EOF. One close per
        # distinct number: PTY exec passes the same slave fd three times.
        for fd in dict.fromkeys(
            f for f in (slave_fd, child_stdin, child_stdout, child_stderr)
            if f is not None
        ):
            _close_fd(fd)
        slave_fd = None

        return RouteBExecProcess(
            instance=self,
            child_id=int(reply["child_id"]),
            pid=int(reply["pid"]),
            argv=argv,
            stdin=stdin_writer,
            stdout=stdout_reader,
            stderr=stderr_reader,
            pty=master,
        )

    def update_network(self, ips) -> list[int]:
        """F4.3/S2 update for **new execs**; returns the stale child ids.

        The in-process surface reports a request wider than the ceiling as
        :class:`PermissionError` (EPERM), which the executor maps to HTTP 409
        *before* the record is persisted. A refusal on this route means
        exactly the same thing -- the slot will not carry a set the ceiling
        cannot represent -- so it is translated rather than allowed to surface
        as a generic error (which would leak a 500 with a half-applied
        update).
        """
        try:
            reply = self.request(
                "update_network", {"ips": [str(ip) for ip in ips]}
            )
        except SandboxError as exc:
            raise PermissionError(str(exc)) from exc
        return list(reply.get("stale_child_ids") or [])

    def stats(self) -> dict:
        return self.request("stats")

    def close(self) -> None:
        """End the generation and give the uid back to the pool (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._pool.retire(self._handle)
        finally:
            if self._channel is not None:
                try:
                    self._channel.close()
                except Exception:  # noqa: BLE001 - handle already gone
                    pass
                self._channel = None


def _close_fd(fd) -> None:
    if fd is None:
        return
    try:
        os.close(int(fd))
    except OSError:
        pass


def _stdio_is_pty(stdio) -> bool:
    """``ExecStdio.PTY`` without importing the native enum off-Linux."""
    if stdio is None:
        return False
    return int(stdio) == 3


# ------------------------------------------------------------------ pool registry

_POOLS: dict[tuple, W1SlotPool] = {}


def slot_pool_for(
    config: "RouteBConfig",
    *,
    channel_factory: Callable[[str, str], object] | None = None,
    supervise_bin: Path | None = None,
) -> W1SlotPool:
    """The worker's route-B slot fleet (one per uid segment).

    The segment is the host-uid pool itself: a slot is started at the uid the
    worker already allocated to the sandbox, so route B cannot widen the uid
    space and ``E2B_UID_POOL_*`` stays the single source of truth for
    identity. ``E2B_ROUTE_B_SLOTS`` caps live slots below the segment size.
    """
    size = int(config.uid_size)
    if config.slots > 0:
        size = min(size, int(config.slots))
    key = (int(config.uid_start), size, str(config.tmp_root))
    pool = _POOLS.get(key)
    if pool is None:
        pool = W1SlotPool(
            uid_start=key[0],
            size=size,
            tmp_root=Path(config.tmp_root),
            spawner=config.spawner,
            channel_factory=channel_factory,
            supervise_bin=supervise_bin,
        )
        _POOLS[key] = pool
    return pool


@dataclass
class RouteBConfig:
    """The worker-side route-B knobs resolved from :class:`Settings`.

    Built by the executor factory (unit tests construct the executor without
    it, which keeps the in-process backend).  ``spawner`` overrides how a slot
    process is started: with an external slot fleet / privileged launcher the
    worker itself need not be root, without one the default ``setpriv`` spawn
    needs it.
    """

    mode: str = "auto"
    slots: int = 0
    uid_start: int = 10000
    uid_size: int = 1000
    tmp_root: Path = Path("/tmp/sandlock-route-b")
    spawner: Callable[..., subprocess.Popen] | None = None

    @classmethod
    def from_settings(cls, settings) -> "RouteBConfig":
        """Resolve from worker settings.

        Every field falls back to the ``Settings`` default, so a caller with a
        partial settings object (the factory's unit stubs) simply gets the
        documented default -- route B off unless its own switches say
        otherwise.
        """
        return cls(
            mode=str(getattr(settings, "route_b", "auto")).lower(),
            slots=int(getattr(settings, "route_b_slots", 0) or 0),
            uid_start=int(getattr(settings, "uid_pool_start", 10000)),
            uid_size=int(getattr(settings, "uid_pool_size", 1000)),
            tmp_root=Path(
                getattr(settings, "route_b_tmp_root", "/tmp/sandlock-route-b")
            ),
        )

    @property
    def privileged_starter(self) -> bool:
        """Can this worker start a slot at another uid?"""
        return self.spawner is not None or os.geteuid() == 0


def reset_slot_pools() -> None:
    """Drop the cached fleets (tests / worker restart)."""
    _POOLS.clear()
