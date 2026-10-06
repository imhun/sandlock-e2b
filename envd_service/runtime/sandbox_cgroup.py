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

N83 phase 2 (Task 1) adds the *ceiling* half here too, and for the same reason
this module exists: both ceilings are cgroup facts. The **policy** ceiling (what
one sandbox may be configured to) comes from the environment, never from a
kernel read -- see :mod:`gateway_common.sandbox_ceiling`. The **kernel** ceiling
(``cpu.max``/``memory.max``/``pids.max`` on the worker's own cgroup) is read by
:func:`read_kernel_ceiling`, and :func:`check_policy_ceiling` cross-checks the
two at worker startup (plan D5b): a policy above the kernel is refused **by
name**, and a kernel that sets no ceiling at all -- the compose lanes' measured
shape, where nothing sets ``cpus``/``mem_limit`` -- starts normally with one
explicit WARN, because there the policy and the platform's ledger are the only
bounds left.

N83 phase 2 (Task 3) adds the *writing* half: ``setup()`` enables ``memory``
and ``pids`` beside phase 1's ``cpu`` (one command, the same drain -- a write
while the cgroup still holds tasks is ``EBUSY``), and ``attach()`` writes
``memory.high``/``memory.max``/``pids.max`` beside ``cpu.max``, each one read
back verbatim. ``memory.high`` and ``memory.max`` carry the same value (D2:
reclaim first, kill only if the process really cannot come down) and
``memory.oom.group`` is deliberately never written (D3: the default 0 keeps an
over-budget sandbox from dragging its neighbours, or the parent container,
down with it). The **policy** ceiling is injected into this class (R3), never
read from the kernel: a declared size above it is refused by name, because
"run smaller silently" is the one outcome the API promise must not have.

N83 phase 2 (Task 5) adds the *reading* half of those two endings, and it has
to happen inside this module for the same reason everything else does: the
counters are kernfs files **inside** the box. ``memory.events`` counts the
times a charge hit ``memory.max`` (``oom_kill``, plus the whole-group variant
``oom_group_kill`` that stays 0 because D3 never writes ``memory.oom.group``);
``pids.events``'s ``max`` counts the times a *task* creation hit ``pids.max``
-- tasks, so threads count (D4). The kernel removes both files with the
directory, so :meth:`SandboxCgroups.release` reads them **before**
``cgroup.kill``/``rmdir`` (that is the last chance to see them; a reading that
cannot be taken is logged by name and the teardown proceeds, because nothing
reclaims a leftover ``sbx_*`` directory) while
:meth:`SandboxCgroups.sample_events` reads the live boxes for the heartbeat
(best effort: a heartbeat must never be lost over a cgroup file). Both feed the
same per-sandbox map, which rides the heartbeat so a kill becomes a named event
instead of a process that "just vanished".
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from gateway_common.paths import validate_sandbox_id
from gateway_common.sandbox_ceiling import (
    MAX_SANDBOX_CPU_PERCENT_ENV,
    MAX_SANDBOX_MEMORY_MB_ENV,
    MAX_SANDBOX_PROCESSES_ENV,
    SandboxCeiling,
)

logger = logging.getLogger(__name__)

#: How deep the compose-lane search walks the mounted tree before giving up.
_WALK_MAX_DEPTH = 6

#: Every sandbox cgroup this module builds is named this plus the sandbox id.
_SANDBOX_PREFIX = "sbx_"

#: The kernfs files a cgroup directory exposes. On cgroupfs they are removed
#: *with* the directory, so the first ``rmdir`` succeeds; a plain filesystem
#: keeps them, so teardown falls back to unlinking precisely these names (they
#: are unremovable on a real cgroupfs, where that fallback is never reached)
#: before trying ``rmdir`` again. A file that is not one of these still blocks
#: the second ``rmdir``, which is the failure the unit lane pins.
_KERNFS_FILES = (
    "cpu.max",
    "memory.high",
    "memory.max",
    "memory.events",
    "pids.max",
    "pids.events",
    "cgroup.procs",
    "cgroup.kill",
    "cgroup.subtree_control",
)

#: The counters the kernel keeps for the two endings phase 2 introduced, under
#: the names the heartbeat uses. ``oom_kill``/``oom_group_kill`` are
#: ``memory.events``'s own lines; ``pids_max`` is ``pids.events``'s ``max``
#: line, prefixed by its file because a bare ``max`` would collide with the
#: memory file's line of that name (and with ``memory.max`` itself).
MEMORY_EVENT_COUNTERS = ("oom_kill", "oom_group_kill")
PIDS_EVENT_COUNTER = "max"

#: The three controllers ``setup()`` enables on the delegated parent. One
#: command, because they arrive through one delegation and one drain:
#: ``cgroup.subtree_control`` is already writable by the worker, so phase 1's
#: word and phase 2's two are the same write with two more words in it, and the
#: same "no internal process" rule applies to the whole command (measured: the
#: write is ``EBUSY`` while the parent still holds tasks).
CGROUP_CONTROLLERS = ("cpu", "memory", "pids")


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


def memory_max_for(memory_mb: int) -> str:
    """The ``memory.high``/``memory.max`` byte count for a declared MiB budget."""
    return f"{memory_mb * 1024 * 1024}"


def pids_max_for(max_processes: int) -> str:
    """The ``pids.max`` task count for a declared process budget.

    Tasks, not processes: threads share this one budget, so the number is the
    declared budget verbatim -- exactly the semantics the mediator's own
    ``EAGAIN`` count has today (plan D4).
    """
    return str(max_processes)


def _read_limit_file(path: Path) -> str:
    """One line of a kernfs limit file, or a named refusal.

    A limit this module cannot read is never silently absent: the whole point
    of the cross-check is that "no number" must not read as "no limit".
    """
    try:
        return path.read_text().strip()
    except OSError as exc:
        raise CgroupRefusal(f"cgroup-refusal kernel-ceiling-read: {path}") from exc


def _cpu_percent_limit(text: str, path: Path) -> int | None:
    """``cpu.max`` as a percentage of one core; ``max <period>`` is ``None``.

    The division **floors** on purpose: the kernel's capacity is never
    overstated, so the comparison below can only refuse more, never let a
    sandbox past what the container layer would actually give it.
    """
    if text.split(" ", 1)[0] == "max":
        return None
    quota, _, period = text.partition(" ")
    try:
        quota_us, period_us = int(quota), int(period)
    except ValueError as exc:
        raise CgroupRefusal(
            f"cgroup-refusal kernel-ceiling-format: {path} reads {text!r}, "
            "expected '<quota> <period>' in microseconds or 'max <period>'"
        ) from exc
    if period_us <= 0:
        raise CgroupRefusal(
            f"cgroup-refusal kernel-ceiling-format: {path} reads {text!r} "
            "(a non-positive period is not a cpu.max)"
        )
    return quota_us * 100 // period_us


def _memory_mb_limit(text: str, path: Path) -> int | None:
    """``memory.max`` in MiB (floored, for the same reason as cpu above)."""
    if text == "max":
        return None
    try:
        limit_bytes = int(text)
    except ValueError as exc:
        raise CgroupRefusal(
            f"cgroup-refusal kernel-ceiling-format: {path} reads {text!r}, "
            "expected a byte count or 'max'"
        ) from exc
    if limit_bytes < 0:
        raise CgroupRefusal(
            f"cgroup-refusal kernel-ceiling-format: {path} reads {text!r}"
        )
    return limit_bytes // (1024 * 1024)


def _processes_limit(path: Path) -> int | None:
    """``pids.max`` (tasks, threads included) -- informational, so optional.

    Both shipped lanes delegate the ``pids`` controller, so the file is
    normally there; a mount that does not expose it reads as "no limit known"
    rather than refusing, because the plan's startup cross-check (D5b) is about
    ``cpu.max``/``memory.max``: a ``pids.max`` of ``max`` is the *measured* k8s
    shape, not a policy/physical mismatch, and warning about it on every k8s
    start would train operators to ignore the warning that matters.
    """
    if not path.is_file():
        return None
    text = _read_limit_file(path)
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise CgroupRefusal(
            f"cgroup-refusal kernel-ceiling-format: {path} reads {text!r}, "
            "expected a task count or 'max'"
        ) from exc


def read_kernel_ceiling(mount: Path) -> SandboxCeiling:
    """The kernel's own ceilings on the cgroup at ``mount`` (the *physical* half).

    ``mount`` is the worker's own cgroup view (``E2B_CGROUP_MOUNT``). On k8s
    that view is already narrowed by ``subPathExpr`` to this pod's cgroup -- the
    directory the plan's measurement table reads, whose ``cpu.max`` is the same
    ``400000 100000`` the container carries (one container per pod); on compose
    it is the whole VM tree, where the measured answer is ``max`` for every
    dimension because those stacks set no ``cpus``/``mem_limit``.

    ``None`` in a field is the kernel's ``max``: no limit on this dimension.
    """
    cpu_path = mount / "cpu.max"
    memory_path = mount / "memory.max"
    return SandboxCeiling(
        cpu_percent=_cpu_percent_limit(_read_limit_file(cpu_path), cpu_path),
        memory_mb=_memory_mb_limit(_read_limit_file(memory_path), memory_path),
        processes=_processes_limit(mount / "pids.max"),
    )


def _event_counters(path: Path, wanted: tuple[str, ...]) -> dict[str, int]:
    """The named counters in one ``*.events`` file.

    An **absent** file is not a failure: a box whose kernel does not keep the
    account (the synthetic lane's tree, or a kernel without that line) has
    nothing to count, and every wanted counter reads 0 -- "this kernel does not
    track it" is the honest value for a counter, unlike the limit files above,
    where "no number" must never read as "no limit".

    A file that **is there** but cannot be read or parsed is a named refusal,
    and that asymmetry is the whole point of this function: these numbers are
    the only record of a sandbox that was killed, so "I could not read it" must
    never be silently reported as "nothing happened" (plan Task 5 -- Review
    Focus §4). The refusal carries the underlying error (``(Is a directory)``,
    ``(Operation not permitted)``), because a *permission* problem is a
    different fact from a malformed file and the caller's log has to say which
    one it hit (review Minor #3).

    The *absent* case is decided by the read itself and not by ``exists()``:
    ``exists()`` swallows a permission error and answers "False", which would
    turn "I am not allowed to look" into "this kernel keeps no account here".

    Callers decide what to do with the refusal: :meth:`SandboxCgroups.release`
    logs it by name and tears the box down anyway (nothing reclaims a leftover
    ``sbx_*``), :meth:`SandboxCgroups.sample_events` skips the box, and
    :meth:`SandboxCgroups.attach` refuses to reuse the directory.
    """
    counters = {name: 0 for name in wanted}
    try:
        text = path.read_text()
    except FileNotFoundError:
        # Genuinely absent (or a dangling link): nothing to count.
        return counters
    except OSError as exc:
        raise CgroupRefusal(
            f"cgroup-refusal events-read: {path} ({exc.strerror or exc})"
        ) from exc
    for line in text.splitlines():
        name, _, raw = line.partition(" ")
        if name not in counters:
            # `memory.events` carries other lines (`low`, `high`, `max`,
            # `oom`); a kernel that grows a new one is not this module's
            # business.
            continue
        try:
            counters[name] = int(raw.strip())
        except ValueError as exc:
            raise CgroupRefusal(
                f"cgroup-refusal events-format: {path} reads {line!r} for "
                f"{name}, expected '<name> <count>'"
            ) from exc
    return counters


def read_sandbox_events(directory: Path) -> dict[str, int]:
    """One box's kernel event counters, under the names the heartbeat uses.

    ``directory`` is a live ``sbx_<id>``: ``oom_kill``/``oom_group_kill`` come
    from its ``memory.events`` and ``pids_max`` from its ``pids.events``. All
    three are monotonic kernel counters -- *the count grew* is the event, and
    there is no "current" reading here on purpose: ``pids.current`` counts
    **tasks, threads included**, so it is not a process count and does not
    belong in a section whose every other number only ever moves forward.

    Raises :class:`CgroupRefusal` for a file that exists but cannot be read or
    parsed (see :func:`_event_counters`); what each caller does with that refusal
    differs and is documented there -- :meth:`SandboxCgroups.release` logs it and
    tears the box down anyway, :meth:`SandboxCgroups.sample_events` skips the
    box, and :meth:`SandboxCgroups._refuse_a_leftover_account` refuses the reuse.
    """
    memory = _event_counters(directory / "memory.events", MEMORY_EVENT_COUNTERS)
    pids = _event_counters(directory / "pids.events", (PIDS_EVENT_COUNTER,))
    return {
        "oom_kill": memory["oom_kill"],
        "oom_group_kill": memory["oom_group_kill"],
        "pids_max": pids[PIDS_EVENT_COUNTER],
    }


def check_policy_ceiling(policy: SandboxCeiling, *, mount: Path) -> SandboxCeiling:
    """D5b: cross-check the configured per-sandbox ceiling against the kernel's.

    The policy comes from ``E2B_MAX_SANDBOX_*`` (never from this read); the
    kernel's comes from the worker's own cgroup. Two outcomes, both loud:

    * **policy above the kernel** -- a named ``cgroup-refusal
      ceiling-exceeds-kernel`` that the worker's startup path turns into a
      refusal to run. Not a clamp and not a warning: the API would otherwise
      promise a sandbox 8 GiB while the container layer OOM-kills it at 2.
    * **the kernel sets no ceiling** (``max``) -- one WARN naming the
      dimensions, then normal startup. This is the compose lane's measured
      shape, and it is legal: the policy and the platform's ledger are then the
      only bounds, which is exactly what an operator has to know.

    Only ``cpu.max``/``memory.max`` are compared (D5b). ``pids.max`` is read
    into the returned ceiling for callers, but its ``max`` is the *measured*
    k8s shape, not a mismatch.

    Returns the kernel ceiling it read, so a caller can log or report it.
    """
    kernel = read_kernel_ceiling(mount)
    above: list[str] = []
    if (
        policy.cpu_percent is not None
        and kernel.cpu_percent is not None
        and policy.cpu_percent > kernel.cpu_percent
    ):
        above.append(
            f"E2B_MAX_SANDBOX_CPU_PERCENT={policy.cpu_percent} > "
            f"{kernel.cpu_percent}% (cpu.max)"
        )
    if (
        policy.memory_mb is not None
        and kernel.memory_mb is not None
        and policy.memory_mb > kernel.memory_mb
    ):
        above.append(
            f"E2B_MAX_SANDBOX_MEMORY_MB={policy.memory_mb} > "
            f"{kernel.memory_mb} MiB (memory.max)"
        )
    if above:
        raise CgroupRefusal(
            "cgroup-refusal ceiling-exceeds-kernel: this worker's cgroup allows "
            f"less than the configured per-sandbox ceiling ({'; '.join(above)}) "
            "-- lower the env or raise the worker container's limits; refusing "
            "to start rather than accepting sandboxes the container layer would "
            "throttle or OOM-kill"
        )
    unbounded = [
        name
        for name, value in (
            ("cpu.max", kernel.cpu_percent),
            ("memory.max", kernel.memory_mb),
        )
        if value is None
    ]
    if unbounded:
        logger.warning(
            "cgroup ceiling: %s sets no kernel limit for %s (read 'max'): the "
            "physical layer caps nothing, so the configured per-sandbox ceiling "
            "is the only bound and aggregate admission rests on the platform's "
            "ledger alone",
            mount,
            ", ".join(unbounded),
        )
    return kernel


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
    exactly those names and try again.

    Raises a named :class:`CgroupRefusal` when the directory is still there, and
    it **names the step that actually blocked it**: a kernfs file that could not
    be unlinked (a permission problem -- measured on a real cgroupfs: ``unlink``
    of ``memory.events`` is ``EPERM``) is reported as such instead of as a bare
    "could not rmdir", which would name a step that ran and failed while hiding
    its cause (review Minor #3).
    """
    try:
        target.rmdir()
        return
    except OSError:
        pass
    blocked: list[str] = []
    for name in _KERNFS_FILES:
        try:
            (target / name).unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            blocked.append(f"{name} ({exc.strerror or exc})")
    try:
        target.rmdir()
    except OSError as exc:
        detail = f"; could not unlink {', '.join(blocked)}" if blocked else ""
        raise CgroupRefusal(
            f"cgroup-refusal release-rmdir: could not rmdir {target}{detail}"
        ) from exc


class SandboxCgroups:
    """Build and tear down ``sbx_<id>`` cgroups under the delegated directory."""

    #: How many retired boxes' last-chance readings are kept for the heartbeat.
    #: Only boxes that hit a wall are kept and the numbers are a few bytes, so
    #: this bounds a worker that lives for months rather than a busy one -- and
    #: because it is only *evicted* (never lowered), losing an entry costs at
    #: most a repeated WARN on the control plane, never a lost event within a
    #: heartbeat's reach.
    RETIRED_EVENT_KEEP = 256

    def __init__(
        self,
        *,
        mount: Path,
        worker_uid: int,
        proc_root: Path = Path("/proc"),
        container_token: str | None = None,
        policy_ceiling: SandboxCeiling | None = None,
    ) -> None:
        self._mount = mount
        self._worker_uid = worker_uid
        self._proc_root = proc_root
        self._container_token = container_token
        self._policy_ceiling = policy_ceiling
        self._parent: Path | None = None
        #: N83 phase 2 (Task 5): the counters of boxes that have already been
        #: torn down, kept from ``release`` (they only exist while the
        #: directory does) so the next ``sample_events`` can still report them.
        #: A successful ``attach`` for the same id drops its entry -- a new
        #: occupant's account starts at zero (Task 5 review, fix 2).
        self._retired_events: dict[str, dict[str, int]] = {}

    # -- startup -------------------------------------------------------

    def setup(self, *, wait_s: float) -> str:
        """Run the two-stage self-check, drain the worker, enable the controllers.

        Returns the evidence line the caller logs: which directory was taken as
        the parent, how many pids were drained, and the enabled controller set.
        On any failure the ``worker/`` cgroup this call created is removed and
        its pids are moved back before a :class:`CgroupRefusal` is raised.

        The drain is what makes the enable legal: ``cgroup.subtree_control``
        refuses a controller while the cgroup still holds tasks (``EBUSY``),
        and a cgroup with tasks *and* an enabled domain controller is "domain
        invalid", so the order below is not a preference.
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
            enabled = self._enable_controllers(parent)
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

    def _enable_controllers(self, parent: Path) -> str:
        """Enable ``cpu``/``memory``/``pids`` on the (now empty) parent, and verify it.

        One write of three words, then a readback of the *enabled set* the
        kernel echoes: the enabled set is a fact the module must see for every
        controller it asked for, because a silently absent ``memory`` would
        leave the files ``attach`` is about to write non-existent (or, worse,
        the write landing nowhere).
        """
        control = parent / "cgroup.subtree_control"
        command = " ".join(f"+{name}" for name in CGROUP_CONTROLLERS)
        try:
            control.write_text(command)
        except OSError as exc:
            raise CgroupRefusal(
                f"cgroup-refusal subtree-control-write: {control}"
            ) from exc
        read_back = control.read_text().strip()
        # The kernel echoes the *enabled set* ("cpu memory pids"), not the command.
        enabled = {token.lstrip("+-") for token in read_back.split()}
        missing = [name for name in CGROUP_CONTROLLERS if name not in enabled]
        if missing:
            raise CgroupRefusal(
                f"cgroup-refusal subtree-control: wrote {command!r} to {control}, "
                f"read {read_back!r} (missing: {', '.join(missing)})"
            )
        return " ".join(sorted(enabled))

    # -- per-sandbox ---------------------------------------------------

    @property
    def kernel_ceiling(self) -> SandboxCeiling:
        """The kernel's ceilings on this worker's own container cgroup.

        Read at the delegated container cgroup once :meth:`setup` has settled on
        one (that is the directory ``sbx_<id>`` is built under, and the one
        whose ``cpu.max`` the deployment actually configured), and at the mount
        root before that -- which on k8s *is* this pod's cgroup, narrowed by the
        manifest's ``subPathExpr``.
        """
        return read_kernel_ceiling(self._parent or self._mount)

    def attach(
        self,
        *,
        sandbox_id: str,
        pid: int,
        cpu_percent: int,
        memory_mb: int | None,
        max_processes: int | None,
    ) -> str:
        """Create ``sbx_<id>``, write its three limits, and place ``pid`` in it.

        Returns the cgroup directory. Reuse-or-refuse: an existing directory
        that already holds pids is refused, never silently shared.

        ``cpu_percent``, ``memory_mb`` and ``max_processes`` are the sandbox's
        **declared** sizes -- the control plane's ``cpuPercent``/``memoryMB``/
        ``maxProcesses``, travelling unchanged from the worker's own record.
        ``None`` for either of the last two is "the caller did not declare this
        dimension": the worker's per-sandbox ceiling is then written, which is
        a bound and the only value in sight that nobody had to invent.

        Before anything is created, all three are checked against the policy
        ceiling injected into this handle (R3, the worker's second gate -- the
        control plane ran the first one): a declared size above it is a named
        refusal, never a clamp and never a silent smaller run. Every limit that
        is written is then read back **verbatim**; a disagreement is a refusal,
        and the directory this call created is removed with it.
        """
        self._validate_sandbox_id(sandbox_id)
        parent = self._require_parent()
        memory, pids = self._checked_declared_sizes(
            sandbox_id=sandbox_id,
            cpu_percent=cpu_percent,
            memory_mb=memory_mb,
            max_processes=max_processes,
        )
        target = parent / f"{_SANDBOX_PREFIX}{sandbox_id}"
        created = False
        try:
            if target.exists():
                held = _cgroup_pids(target)
                if held:
                    raise CgroupRefusal(
                        f"cgroup-refusal sbx-in-use: {target} already holds pids {held!r}"
                    )
                self._refuse_a_leftover_account(target, sandbox_id)
            else:
                try:
                    target.mkdir()
                except OSError as exc:
                    raise CgroupRefusal(
                        f"cgroup-refusal sbx-mkdir: {target}"
                    ) from exc
                created = True
            self._write_limit(
                target / "cpu.max", cpu_max_for(cpu_percent), "cpu-max"
            )
            # D2: the same line for both memory files -- `memory.high` reclaims
            # (throttles) first and `memory.max` is the wall behind it, so an
            # allocation is only killed when the process really cannot come
            # down. D3: `memory.oom.group` is never written; the default 0
            # keeps the kill on the allocating task instead of the whole box.
            self._write_limit(target / "memory.high", memory, "memory-high")
            self._write_limit(target / "memory.max", memory, "memory-max")
            # D4: tasks, not processes -- threads share this one budget, which
            # is exactly what today's mediator-side count enforces (EAGAIN).
            self._write_limit(target / "pids.max", pids, "pids-max")
            procs = target / "cgroup.procs"
            procs.write_text(f"{pid}\n")
            placed = procs.read_text().split()
            if placed != [str(pid)]:
                raise CgroupRefusal(
                    f"cgroup-refusal procs: wrote {pid} to {procs}, read {placed!r}"
                )
            expected = f"0::/{_SANDBOX_PREFIX}{sandbox_id}"
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
        # Task 5 review, fix 2 (sampler half): a new occupant starts a clean
        # account, so whatever the sweep remembered for this id -- the previous
        # generation's last-chance reading -- must not be merged into its
        # report and read as a wall the new sandbox hit.
        self._retired_events.pop(sandbox_id, None)
        return str(target)

    def _refuse_a_leftover_account(self, target: Path, sandbox_id: str) -> None:
        """Task 5 review, fix 2: a leftover box must never hand over its account.

        The directory is there and holds no pids, so it looks reusable -- but
        ``memory.events``/``pids.events`` are **cumulative per cgroup** and
        nothing resets them: writing ``memory.max``/``pids.max`` again does not,
        and a real cgroupfs refuses to let them be cleared at all (measured
        2026-10-07 on the local cgroup v2 lane: ``unlink(memory.events)`` is
        ``EPERM`` and writing it back to zero is ``EINVAL``). Reusing such a
        directory would report the *previous* occupant's kill as the new
        sandbox's -- a stored record and a named WARN for a sandbox that never
        hit a wall -- so the reuse is refused by name instead.

        A directory whose account reads all zeros is reused as before: the
        previous occupant hit nothing there, so there is nothing to inherit. An
        account that *cannot* be read is refused by the read's own name
        (``events-read``): "cannot prove it is clean" is not "clean".
        """
        counters = read_sandbox_events(target)
        nonzero = ", ".join(
            f"{name}={count}" for name, count in sorted(counters.items()) if count
        )
        if nonzero:
            raise CgroupRefusal(
                f"cgroup-refusal sbx-stale-account: sandbox {sandbox_id}'s cgroup "
                f"directory {target} still carries a previous generation's kernel "
                f"event counters ({nonzero}), and a kernfs counter cannot be "
                "cleared in place -- refusing to place a new sandbox in it rather "
                "than report the previous occupant's wall as its own"
            )

    def _checked_declared_sizes(
        self,
        *,
        sandbox_id: str,
        cpu_percent: int,
        memory_mb: int | None,
        max_processes: int | None,
    ) -> tuple[str, str]:
        """R3's second gate: the declared sizes vs this worker's own ceiling.

        The ceiling is the one injected at construction (the worker's
        ``E2B_MAX_SANDBOX_*`` policy, resolved by
        ``envd_service.route_b.policy_ceiling_for``) -- explicitly **not** the
        kernel read: the kernel's half is the startup cross-check's business,
        and a kernel that sets no limit is a legal lane where the policy is the
        only bound left.

        Three outcomes, none of them silent:

        * a declared dimension is above the ceiling -- one named refusal naming
          every offending dimension, its value and the env that carries the
          limit (no clamp: the API promise and the kernel have to agree);
        * a dimension nobody declared -- the ceiling is written, so the sandbox
          is still bounded by the deployment's own number;
        * no ceiling on this handle at all -- refused by name, because "checked
          against nothing" is the fail-open direction this whole module exists
          to close.
        """
        ceiling = self._policy_ceiling
        if ceiling is None:
            raise CgroupRefusal(
                "cgroup-refusal ceiling-unavailable: this handle carries no "
                f"per-sandbox ceiling, so sandbox {sandbox_id}'s declared size "
                "cannot be checked against anything (N83 phase 2 R3) -- build "
                "it through sandbox_cgroups_for(settings), or set "
                "E2B_SANDBOX_CGROUP=off"
            )
        memory = ceiling.memory_mb if memory_mb is None else int(memory_mb)
        processes = ceiling.processes if max_processes is None else int(max_processes)
        above: list[str] = []
        if ceiling.cpu_percent is not None and int(cpu_percent) > ceiling.cpu_percent:
            above.append(
                f"cpuPercent {cpu_percent} > {ceiling.cpu_percent} "
                f"({MAX_SANDBOX_CPU_PERCENT_ENV})"
            )
        if ceiling.memory_mb is not None and memory > ceiling.memory_mb:
            above.append(
                f"memoryMB {memory} > {ceiling.memory_mb} "
                f"({MAX_SANDBOX_MEMORY_MB_ENV})"
            )
        if ceiling.processes is not None and processes > ceiling.processes:
            above.append(
                f"maxProcesses {processes} > {ceiling.processes} "
                f"({MAX_SANDBOX_PROCESSES_ENV})"
            )
        if above:
            raise CgroupRefusal(
                f"cgroup-refusal size-exceeds-ceiling: sandbox {sandbox_id} "
                "declares more than this worker's per-sandbox ceiling "
                f"({'; '.join(above)}) -- refusing the create rather than "
                "running a smaller sandbox silently; lower the request or "
                "raise E2B_MAX_SANDBOX_*"
            )
        return memory_max_for(memory), pids_max_for(processes)

    def _write_limit(self, path: Path, value: str, reason: str) -> None:
        """Write one kernfs limit and read it back **verbatim**.

        The readback is the only proof there is: kernfs is where the kernel's
        answer lives, and a value it rounded (or a parent layer that applied
        "the smaller one") must be a refusal naming the file, never a shrug.
        """
        try:
            path.write_text(value)
        except OSError as exc:
            raise CgroupRefusal(f"cgroup-refusal {reason}-write: {path}") from exc
        read_back = path.read_text().strip()
        if read_back != value:
            raise CgroupRefusal(
                f"cgroup-refusal {reason}: wrote {value!r} to {path}, "
                f"read {read_back!r}"
            )

    def release(self, *, sandbox_id: str) -> bool:
        """Kill and remove ``sbx_<id>``. Absent is ``False``, not an error.

        N83 phase 2 (Task 5) reads the box's kernel event counters here, first
        and **before** ``cgroup.kill`` and the ``rmdir``: the kernel removes
        ``memory.events``/``pids.events`` with the directory, so this is the
        last chance to see them, and the reading is kept for the next heartbeat
        (:meth:`sample_events`) -- which is what turns "the process just
        vanished" into a named event on the control plane.

        **A reading that cannot be taken costs the reading, not the teardown**
        (Task 5 review, fix 1). Nothing in this worker reclaims a leftover
        ``sbx_*`` directory, so keeping a box alive "until the account can be
        read" is a permanent leak: the caller (route-B's retire) logs one
        warning and returns the uid, and there is no retry. A failed read is
        therefore logged as one named WARNING -- sandbox id, path, and the
        refusal that carries the underlying error -- and ``cgroup.kill`` +
        ``rmdir`` proceed. What is lost is exactly this reading: the counters a
        sweep had already cached still reach the control plane, and one that was
        never swept does not.

        An *absent* file is not a failure (a lane whose kernel keeps no such
        account has nothing to count), and a box that hit nothing keeps no entry
        -- "the count grew" is the event, and this method is called for every
        sandbox that ever lived.
        """
        self._validate_sandbox_id(sandbox_id)
        parent = self._require_parent()
        target = parent / f"{_SANDBOX_PREFIX}{sandbox_id}"
        events: dict[str, int] = {}
        try:
            if not target.exists():
                return False
            try:
                events = read_sandbox_events(target)
            except CgroupRefusal as exc:
                logger.warning(
                    "cgroup events: sandbox %s: the last-chance reading of %s "
                    "failed: %s; tearing the box down anyway -- this reading is "
                    "lost, because the kernel removes the counters with the "
                    "directory (the values the sweep already cached still reach "
                    "the control plane)",
                    sandbox_id,
                    target,
                    exc,
                )
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
        self._remember_events(sandbox_id, events)
        return True

    def sample_events(self) -> dict[str, dict[str, int]]:
        """Every live box's kernel event counters, plus the retired readings.

        This is the heartbeat's copy (N83 phase 2, Task 5), and it is **best
        effort by construction**: it never raises, because a heartbeat must not
        be lost over a cgroup file (the last-chance read of the same numbers is
        :meth:`release`'s). A box whose account cannot be read is **skipped**,
        never reported as zeros: "I could not read it" is not "nothing
        happened".

        Boxes that have hit no wall are omitted too -- they carry no event, and
        this rides a channel that speaks every few seconds. The retired
        readings ``release`` kept are merged in and never lowered, so a box that
        was killed *and* torn down between two samples still reaches the control
        plane at least once; an ``attach`` for that id drops its entry (Task 5
        review, fix 2), so a retired reading is never reported as a later
        occupant's own -- the merge keeps the *maximum* as belt and braces, not
        as the guarantee.
        """
        merged: dict[str, dict[str, int]] = {
            sandbox_id: dict(counters)
            for sandbox_id, counters in self._retired_events.items()
        }
        parent = self._parent
        if parent is not None:
            for entry in self._live_box_dirs(parent):
                sandbox_id = entry.name[len(_SANDBOX_PREFIX) :]
                try:
                    counters = read_sandbox_events(entry)
                except (CgroupRefusal, OSError) as exc:
                    logger.debug("cgroup events: cannot read %s (%s)", entry, exc)
                    continue
                seen = merged.get(sandbox_id)
                merged[sandbox_id] = (
                    counters
                    if seen is None
                    else {
                        name: max(counters.get(name, 0), seen.get(name, 0))
                        for name in counters.keys() | seen.keys()
                    }
                )
        return {
            sandbox_id: counters
            for sandbox_id, counters in merged.items()
            if any(counters.values())
        }

    def _live_box_dirs(self, parent: Path) -> list[Path]:
        """The ``sbx_*`` directories under the delegated parent, best effort."""
        try:
            listing = list(parent.iterdir())
        except OSError:
            # A view that went away mid-read must not cost a heartbeat.
            return []
        return sorted(
            path
            for path in listing
            if path.name.startswith(_SANDBOX_PREFIX) and path.is_dir()
        )

    def _remember_events(self, sandbox_id: str, counters: dict[str, int]) -> None:
        """Keep one box's last-chance reading for the next heartbeat."""
        if not any(counters.values()):
            return
        previous = self._retired_events.get(sandbox_id)
        if previous is not None:
            counters = {
                name: max(value, previous.get(name, 0))
                for name, value in counters.items()
            }
        self._retired_events.pop(sandbox_id, None)  # keep the eviction FIFO
        self._retired_events[sandbox_id] = counters
        while len(self._retired_events) > self.RETIRED_EVENT_KEEP:
            self._retired_events.pop(next(iter(self._retired_events)))

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
        except (CgroupRefusal, OSError):
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
        except (CgroupRefusal, OSError):
            logger.warning("cgroup-refusal cleanup: could not rmdir %s", target)
