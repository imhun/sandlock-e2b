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

The manager only owns the slot lifecycle (spawn at a free uid, wait for the
registered socket, shutdown on release).  It deliberately knows nothing about
policy building or exec semantics: callers pass the full-field supervise
policy JSON and the parking ``--program`` document (envd instances have no
main-program concept, so the generation's M0 is a parking shell that blocks
on ``read`` from ``/dev/zero`` — a shell builtin, zero CPU, no external
utility dependency).

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


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

    @property
    def stderr(self) -> str:
        if self.process.stderr is None:
            return ""
        try:
            return self.process.stderr.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - diagnostic only
            return ""


class W1SlotPool:
    """Fixed-uid route-B slot fleet (W1 recycle semantics).

    Not thread-safe by itself: call :meth:`acquire` / :meth:`release` from
    one async task (the sandbox lifecycle path); a cross-task lock is the
    caller's choice (envd serializes sandbox events per registry entry).
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
        self._socket_timeout_s = socket_timeout_s
        self._slots: dict[str, SlotHandle] = {}
        self._free: list[int] = list(self._uids)

    @property
    def live_slots(self) -> list[SlotHandle]:
        return list(self._slots.values())

    def acquired_uid(self, sandbox_id: str) -> int | None:
        slot = self._slots.get(sandbox_id)
        return slot.uid if slot is not None else None

    async def acquire(
        self,
        sandbox_id: str,
        policy_json: dict,
        program_json: dict | None = None,
    ) -> SlotHandle:
        """Lease the least-recently-freed uid and start its slot.

        ``program_json`` defaults to the parking main
        ``{"argv": ["/bin/sh", "-c", "read x < /dev/zero"]}``.
        """
        if sandbox_id in self._slots:
            raise ValueError(f"sandbox {sandbox_id} already holds a route-B slot")
        if not self._free:
            raise RuntimeError(
                "route-B slot pool exhausted (all uids live); "
                "raise E2B_ROUTE_B_SLOTS or wait for a release"
            )
        uid = self._free.pop(0)
        program = program_json or {
            "argv": ["/bin/sh", "-c", "read x < /dev/zero"]
        }
        name = f"rb-{sandbox_id}"
        token = secrets.token_hex(32)
        slot_dir = self._tmp_root / str(uid) / name
        slot_dir.mkdir(parents=True, exist_ok=True)
        policy_path = slot_dir / "policy.json"
        program_path = slot_dir / "program.json"
        policy_path.write_text(json.dumps(policy_json), encoding="utf-8")
        program_path.write_text(json.dumps(program), encoding="utf-8")

        def _start() -> SlotHandle:
            process = self._spawner(
                uid=uid,
                policy_path=policy_path,
                program_path=program_path,
                name=name,
                token=token,
                worker_uid=self._worker_uid,
            )
            sock_path = Path(
                f"/tmp/sandlock-ctl-{uid}-registry/"
                f"{_fnv1a_hex(name)}.d/control.sock"
            )
            deadline = time.monotonic() + self._socket_timeout_s
            while time.monotonic() < deadline:
                if sock_path.exists():
                    return SlotHandle(
                        sandbox_id=sandbox_id,
                        uid=uid,
                        name=name,
                        token=token,
                        sock_path=sock_path,
                        policy_path=policy_path,
                        program_path=program_path,
                        process=process,
                    )
                if process.poll() is not None:
                    err = ""
                    if process.stderr is not None:
                        err = process.stderr.read().decode("utf-8", "replace")
                    raise RuntimeError(
                        f"route-B slot {name} (uid {uid}) exited before binding "
                        f"{sock_path}: {err}"
                    )
                time.sleep(0.1)
            process.kill()
            raise RuntimeError(
                f"route-B slot {name} (uid {uid}) did not bind {sock_path} "
                f"within {self._socket_timeout_s}s"
            )

        try:
            handle = await asyncio.to_thread(_start)
        except BaseException:
            self._free.append(uid)
            raise
        self._slots[sandbox_id] = handle
        logger.info(
            "route-B slot %s leased uid %d (sandbox %s)",
            name,
            uid,
            sandbox_id,
        )
        return handle

    async def release(self, sandbox_id: str) -> None:
        """Shut the slot down (shutdown verb) and return its uid to the pool."""
        handle = self._slots.pop(sandbox_id, None)
        if handle is None:
            return
        from sandlock.supervise import SuperviseChannel

        try:
            with SuperviseChannel(str(handle.sock_path), handle.token) as ch:
                ch.request("shutdown")
        except Exception as e:  # noqa: BLE001 - best-effort teardown
            logger.warning(
                "route-B shutdown for %s failed (%s); killing slot pid %s",
                sandbox_id,
                e,
                handle.process.pid,
            )
        try:
            await asyncio.to_thread(handle.process.wait, 20)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            await asyncio.to_thread(handle.process.wait, 10)
        self._free.append(handle.uid)
        logger.info("route-B slot %s released uid %d", handle.name, handle.uid)
