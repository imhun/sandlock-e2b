"""Per-sandbox cgroups, managed by the worker itself (N83 phase 1, form W).

The worker runs as uid 65534 with no capabilities, inside its own private
cgroup namespace, with a read-write cgroupfs view at ``E2B_CGROUP_MOUNT``. A
one-shot delegation (Task 3/4, the agent's face B) has already chowned *this*
container's cgroup directory -- its ``cgroup.procs`` and its
``cgroup.subtree_control``, but deliberately not ``cpu.max`` -- to 65534, so
this module can build a subtree under it and cannot lift its own ceiling.

Measured facts this module is written against (2026-10-06, k0s and the local
Docker VM, both reproduced):

* ``+cpu`` into ``cgroup.subtree_control`` fails ``EBUSY`` while the cgroup
  still has tasks, so the parent must be emptied first -- ``mkdir worker/``,
  move our own pids into ``worker/cgroup.procs``, then ``+cpu``;
* a cgroup that has tasks *and* an enabled domain controller is "domain
  invalid" and later migrations into its descendants fail ``EOPNOTSUPP`` -- so
  the controller must never be enabled before the drain;
* a directory the worker creates is owned by the worker, so it can write the
  ``cpu.max`` it just made;
* teardown is ``cgroup.kill`` (mode 0200, write-only) then ``rmdir``.

The module is *fail closed*: every syscall that builds, writes, places, or
removes a cgroup is wrapped, and any failure is translated into a
:class:`CgroupRefusal` with a stable, greppable ``cgroup-refusal <reason>:``
name -- a bare ``OSError`` never escapes :meth:`SandboxCgroups.setup`,
:meth:`SandboxCgroups.attach`, or :meth:`SandboxCgroups.release`. A failed
``setup`` removes the ``worker/`` cgroup it created and moves any drained pids
back, and a failed ``attach`` removes the ``sbx_<id>`` it created, so a refusal
leaves the node as it was. ``sandbox_id`` is validated before it ever reaches a
path, so it cannot climb out of the delegated subtree.

The only injected state is the two facts a unit lane cannot observe:
``proc_root`` for ``/proc/<pid>/cgroup`` placement, and (for the compose lane)
``container_token`` to narrow the search to our container's cgroup among the
whole mounted VM tree.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from gateway_common.paths import validate_sandbox_id

logger = logging.getLogger(__name__)

#: How deep the compose-lane search walks the mounted tree before giving up.
_WALK_MAX_DEPTH = 6

#: The kernfs files a cgroup directory exposes. On cgroupfs they are removed
#: *with* the directory, so the first ``rmdir`` succeeds; a plain filesystem
#: keeps them, so teardown falls back to unlinking precisely these names (they
#: are unremovable on a real cgroupfs, where that fallback is never reached)
#: before trying ``rmdir`` again. A file that is not one of these still blocks
#: the second ``rmdir``, which is the failure the unit lane pins.
_KERNFS_FILES = ("cpu.max", "cgroup.procs", "cgroup.kill", "cgroup.subtree_control")


class CgroupRefusal(Exception):
    """A cgroup operation was refused; the caller must fail closed.

    Raised for every failure of this module -- a mount root that is not this
    pod's cgroup, a delegation that never arrived or arrived twice, a readback
    that does not match what was written, a sandbox cgroup that is already
    occupied, or any syscall that made one of those steps impossible. The
    message is a stable, greppable ``cgroup-refusal <reason>:`` line so a
    startup failure names its cause instead of leaving a mystery.
    """


def cpu_max_for(cpu_percent: int) -> str:
    """The ``cpu.max`` quota string for a percentage of one core, 100 ms period."""
    return f"{cpu_percent * 1000} 100000"


def _cgroup_pids(cgroup_dir: Path) -> list[int]:
    """The kernel's placement oracle: the pids a cgroup currently holds.

    ``cgroup.procs`` is authoritative -- the kernel rewrites it on every
    migration -- so this reads that file directly. Unit tests stand in a fake,
    because a synthetic tree on ``tmp_path`` has no kernel to perform the move
    the module just asked for.
    """
    text = (cgroup_dir / "cgroup.procs").read_text()
    try:
        return [int(token) for token in text.split()]
    except ValueError as exc:
        raise CgroupRefusal(
            f"cgroup-refusal procs-format: {cgroup_dir}/cgroup.procs is not a pid "
            f"list: {text!r}"
        ) from exc


def _remove_cgroup_dir(target: Path) -> None:
    """Remove a cgroup directory, emulating cgroupfs' file-with-directory model.

    On a real cgroupfs the first ``rmdir`` removes the directory *and* its
    kernfs files; a plain filesystem refuses while they are present, so we drop
    exactly those names and try again. Raises ``OSError`` when something else
    still blocks the removal.
    """
    try:
        target.rmdir()
        return
    except OSError:
        pass
    for name in _KERNFS_FILES:
        try:
            (target / name).unlink()
        except OSError:
            pass
    target.rmdir()


class SandboxCgroups:
    """Build and tear down ``sbx_<id>`` cgroups under the delegated directory."""

    def __init__(
        self,
        *,
        mount: Path,
        worker_uid: int,
        proc_root: Path = Path("/proc"),
        container_token: str | None = None,
    ) -> None:
        self._mount = mount
        self._worker_uid = worker_uid
        self._proc_root = proc_root
        self._container_token = container_token
        self._parent: Path | None = None

    # -- startup -------------------------------------------------------

    def setup(self, *, wait_s: float) -> str:
        """Run the two-stage self-check, drain the worker, and enable ``+cpu``.

        Returns the evidence line the caller logs: which directory was taken as
        the parent, how many pids were drained, and the enabled controller set.
        On any failure the ``worker/`` cgroup this call created is removed and
        its pids are moved back before a :class:`CgroupRefusal` is raised.
        """
        parent: Path | None = None
        worker: Path | None = None
        created = False
        try:
            self._precheck()
            parent = self._await_delegated(wait_s)
            self._parent = parent
            worker = parent / "worker"
            created = self._prepare_worker_dir(worker)
            drained = self._drain_into(parent, worker)
            enabled = self._enable_cpu(parent)
        except CgroupRefusal:
            if parent is not None and worker is not None:
                self._discard_worker(parent, worker, created)
            raise
        except OSError as exc:
            if parent is not None and worker is not None:
                self._discard_worker(parent, worker, created)
            raise CgroupRefusal(f"cgroup-refusal setup-io: {parent or self._mount}") from exc
        return (
            f"cgroup ready parent={parent} worker_uid={self._worker_uid} "
            f"drained={len(drained)} subtree_control={enabled}"
        )

    def _precheck(self) -> None:
        """The cheap guard: a wrong-QoS kubelet-fabricated directory has neither."""
        root = self._mount
        if not (root / "cpu.max").is_file():
            raise CgroupRefusal(f"cgroup-refusal precheck: {root}/cpu.max is missing")
        if not any(child.is_dir() for child in root.iterdir()):
            raise CgroupRefusal(
                f"cgroup-refusal precheck: {root} has no child cgroup directories"
            )

    def _await_delegated(self, wait_s: float) -> Path:
        """Wait for the delegation to land: exactly one directory owned by us."""
        deadline = time.monotonic() + max(wait_s, 0.0)
        while True:
            owned = self._owned_candidates()
            if len(owned) == 1:
                return owned[0]
            if len(owned) > 1:
                listed = ", ".join(str(path) for path in owned)
                raise CgroupRefusal(
                    f"cgroup-refusal ambiguous-delegation: {len(owned)} cgroup "
                    f"directories owned by uid {self._worker_uid} under "
                    f"{self._mount}: {listed}"
                )
            if time.monotonic() >= deadline:
                raise CgroupRefusal(
                    f"cgroup-refusal delegation-timeout: no cgroup directory "
                    f"owned by uid {self._worker_uid} under {self._mount}"
                )
            time.sleep(0.02)

    def _owned_candidates(self) -> list[Path]:
        owned: list[Path] = []
        for path in self._candidate_dirs():
            try:
                uid = path.stat().st_uid
            except OSError:  # a directory that came and went during the wait
                continue
            if uid == self._worker_uid:
                owned.append(path)
        return sorted(owned)

    def _candidate_dirs(self) -> list[Path]:
        """Where the delegated directory may be, given the lane we are on."""
        token = self._container_token
        if token is None:
            # k8s: the mount root *is* this pod's cgroup, so the delegated
            # container cgroup is a direct child -- one level, no walk.
            return [child for child in self._mount.iterdir() if child.is_dir()]
        # compose: the mount is the whole VM cgroup tree; narrow by the
        # container id (its hostname) first, then confirm by ownership.
        root = self._mount
        base_depth = len(root.parts)
        found: list[Path] = []
        for dirpath, dirnames, _files in os.walk(root):
            if len(Path(dirpath).parts) - base_depth >= _WALK_MAX_DEPTH:
                dirnames[:] = []
                continue
            for name in dirnames:
                candidate = Path(dirpath) / name
                if token in str(candidate):
                    found.append(candidate)
        return found

    def _prepare_worker_dir(self, worker: Path) -> bool:
        """Create ``worker/`` (or reuse an empty one). Returns whether we made it."""
        try:
            worker.mkdir()
            return True
        except FileExistsError:
            try:
                occupied = (worker / "cgroup.procs").read_text().split()
            except OSError as exc:
                raise CgroupRefusal(
                    f"cgroup-refusal worker-reuse-read: {worker}"
                ) from exc
            if occupied:
                raise CgroupRefusal(
                    f"cgroup-refusal drain-reused: {worker}/cgroup.procs is not empty"
                )
            return False
        except OSError as exc:
            raise CgroupRefusal(f"cgroup-refusal worker-mkdir: {worker}") from exc

    def _drain_into(self, parent: Path, worker: Path) -> list[int]:
        """Move our own pids into ``worker/`` so ``+cpu`` on the parent is allowed."""
        pids = _cgroup_pids(parent)
        if os.getpid() not in pids:
            raise CgroupRefusal(
                f"cgroup-refusal self-placement: worker pid {os.getpid()} is not "
                f"in {parent}/cgroup.procs"
            )
        procs = worker / "cgroup.procs"
        try:
            procs.write_text("".join(f"{pid}\n" for pid in pids))
        except OSError as exc:
            raise CgroupRefusal(f"cgroup-refusal worker-drain-write: {procs}") from exc
        read_back = _cgroup_pids(worker)
        if sorted(read_back) != sorted(pids):
            raise CgroupRefusal(
                f"cgroup-refusal drain-readback: {procs} holds {read_back!r}, "
                f"expected {pids!r}"
            )
        remaining = _cgroup_pids(parent)
        if remaining:
            raise CgroupRefusal(
                f"cgroup-refusal parent-not-drained: {parent}/cgroup.procs still "
                f"lists {remaining!r}"
            )
        return pids

    def _enable_cpu(self, parent: Path) -> str:
        """Enable the cpu controller on the (now empty) parent, then verify it."""
        control = parent / "cgroup.subtree_control"
        try:
            control.write_text("+cpu")
        except OSError as exc:
            raise CgroupRefusal(
                f"cgroup-refusal subtree-control-write: {control}"
            ) from exc
        read_back = control.read_text().strip()
        # The kernel echoes the *enabled set* ("cpu"), not the "+cpu" command.
        enabled = {token.lstrip("+-") for token in read_back.split()}
        if "cpu" not in enabled:
            raise CgroupRefusal(
                f"cgroup-refusal subtree-control: wrote '+cpu' to {control}, "
                f"read {read_back!r}"
            )
        return " ".join(sorted(enabled))

    # -- per-sandbox ---------------------------------------------------

    def attach(self, *, sandbox_id: str, pid: int, cpu_percent: int) -> str:
        """Create ``sbx_<id>``, set its quota, and place ``pid`` in it.

        Returns the cgroup directory. Reuse-or-refuse: an existing directory
        that already holds pids is refused, never silently shared.
        """
        self._validate_sandbox_id(sandbox_id)
        parent = self._require_parent()
        target = parent / f"sbx_{sandbox_id}"
        created = False
        try:
            if target.exists():
                held = _cgroup_pids(target)
                if held:
                    raise CgroupRefusal(
                        f"cgroup-refusal sbx-in-use: {target} already holds pids {held!r}"
                    )
            else:
                try:
                    target.mkdir()
                except OSError as exc:
                    raise CgroupRefusal(
                        f"cgroup-refusal sbx-mkdir: {target}"
                    ) from exc
                created = True
            quota = cpu_max_for(cpu_percent)
            cpu_max = target / "cpu.max"
            cpu_max.write_text(quota)
            read_quota = cpu_max.read_text().strip()
            if read_quota != quota:
                raise CgroupRefusal(
                    f"cgroup-refusal cpu-max: wrote {quota!r} to {cpu_max}, "
                    f"read {read_quota!r}"
                )
            procs = target / "cgroup.procs"
            procs.write_text(f"{pid}\n")
            placed = procs.read_text().split()
            if placed != [str(pid)]:
                raise CgroupRefusal(
                    f"cgroup-refusal procs: wrote {pid} to {procs}, read {placed!r}"
                )
            expected = f"0::/sbx_{sandbox_id}"
            actual = self._pid_cgroup(pid)
            if actual != expected:
                raise CgroupRefusal(
                    f"cgroup-refusal placement: pid {pid} is in {actual!r}, "
                    f"expected {expected!r}"
                )
        except CgroupRefusal:
            self._discard(target, created)
            raise
        except OSError as exc:
            self._discard(target, created)
            raise CgroupRefusal(f"cgroup-refusal attach-io: {target}") from exc
        return str(target)

    def release(self, *, sandbox_id: str) -> bool:
        """Kill and remove ``sbx_<id>``. Absent is ``False``, not an error."""
        self._validate_sandbox_id(sandbox_id)
        parent = self._require_parent()
        target = parent / f"sbx_{sandbox_id}"
        try:
            if not target.exists():
                return False
            kill = target / "cgroup.kill"
            if kill.exists():
                # Write-only (mode 0200) on a real cgroupfs: no readback is possible.
                try:
                    kill.write_text("1")
                except OSError as exc:
                    raise CgroupRefusal(
                        f"cgroup-refusal release-kill: {target}"
                    ) from exc
            _remove_cgroup_dir(target)
        except CgroupRefusal:
            raise
        except OSError as exc:
            raise CgroupRefusal(
                f"cgroup-refusal release-rmdir: could not rmdir {target}"
            ) from exc
        return True

    # -- helpers -------------------------------------------------------

    def _validate_sandbox_id(self, sandbox_id: str) -> None:
        """Reject an id that could climb out of the delegated subtree."""
        if not validate_sandbox_id(sandbox_id):
            raise CgroupRefusal(
                f"cgroup-refusal sandbox-id: {sandbox_id!r} is not a valid sandbox id"
            )

    def _require_parent(self) -> Path:
        if self._parent is None:
            raise CgroupRefusal(
                "cgroup-refusal setup-not-run: call setup() before attach() or release()"
            )
        return self._parent

    def _pid_cgroup(self, pid: int) -> str:
        path = self._proc_root / str(pid) / "cgroup"
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("0::"):
                return line.strip()
        raise CgroupRefusal(
            f"cgroup-refusal proc-cgroup: {path} is not a cgroup v2 line: {text!r}"
        )

    def _discard_worker(self, parent: Path, worker: Path, created: bool) -> None:
        """Best-effort undo of a ``worker/`` cgroup this call created."""
        if not created:
            return
        # Put the drained pids back so the cgroup is empty and can be removed.
        try:
            drained = _cgroup_pids(worker)
            if drained:
                (parent / "cgroup.procs").write_text("".join(f"{p}\n" for p in drained))
        except OSError:
            pass
        try:
            kill = worker / "cgroup.kill"
            if kill.exists():
                kill.write_text("1")
        except OSError:
            pass
        try:
            _remove_cgroup_dir(worker)
        except OSError:
            logger.warning("cgroup-refusal cleanup: could not rmdir %s", worker)

    def _discard(self, target: Path, created: bool) -> None:
        """Best-effort removal of a cgroup this call created, before refusing."""
        if not created:
            return
        kill = target / "cgroup.kill"
        try:
            if kill.exists():
                kill.write_text("1")
        except OSError:
            pass
        try:
            _remove_cgroup_dir(target)
        except OSError:
            logger.warning("cgroup-refusal cleanup: could not rmdir %s", target)
