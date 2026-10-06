"""Face B: the agent's **one-shot** delegation of the worker's container cgroup.

N83 Phase 1（``docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md`` §3.2，
形态 W）把"每沙箱一个 cgroup"交给 **worker 自己**：它建 ``sbx_<id>``、写 ``cpu.max``、
把槽位进程放进去。它要能这么做，只需要一样东西 —— 它自己的**容器 cgroup 目录**被交给
它（65534）。那正是本模块做的唯一一件事，而且只做一次：

* **谁做**：面 B（root + ``CHOWN/DAC_OVERRIDE/FOWNER`` + 一块 rw 的 cgroupfs 视图）。
  §1.4 实测：这个形状 ``mkdir``/``chown``/写限额文件都可以，**迁移 pid 不行**
  （``cgroup.procs`` 写 = ENOENT，cgroupns 边界）—— 所以放置归 worker，面 B 只 chown。
* **chown 什么（白名单，穷举）**：容器 cgroup **目录**（worker 要在里面 ``mkdir``）+
  ``cgroup.procs``（迁移的"公共祖先"必须可写，本地 lane 实测：漏掉它放置是 EACCES）+
  ``cgroup.subtree_control``（腾空后要写 ``+cpu``）。
  **``cpu.max`` 故意不在名单里**（§1.4 负例 N1：不委派它，worker 写自己的上限就是
  EACCES ⇒ "worker 抬不了自己的额度"这条钉子）。
* **路径从哪来**：**agent 自己推**，绝不接受调用方给的路径（R-D）。内核那半在
  :meth:`c3_agent.lookup.ProcLookup.worker_container_cgroup`；这里做的是"把它落回
  **本进程的挂载视图**"。

**为什么不是简单拼接。** ``/proc/<pid>/cgroup`` 的路径是**相对读进程自己的 cgroup
namespace 根**的读数（本机 Docker VM 实测 2026-10-06：另一个容器读作
``0::/../<container-id>``，而同一个容器在宿主视图里是 ``/docker/<container-id>``；
k8s 上同形，只是多两段 ``..``）。面 B 拿不到 host cgroup namespace（k8s 里那只有
``privileged: true`` 才有，§3.1 F1），所以直接 ``挂载根 / 读数`` 会让 ``..`` 走出挂载根。
落回视图靠**目录名**：内核读数的最后一段就是容器 cgroup 目录的名字（container id 是
64 位十六进制、节点内唯一），在挂载视图里按这个名字找，找到且唯一才算数。

每一次拒绝都是具名的（``CgroupRefusal``）：挂载不在、目录不在视图里、重名、目录里少
了它必须有的文件 —— 一条都不许静默跳过，因为"委派没发生"必须让 worker 起不来，
而不是让它以为有额度。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol

logger = logging.getLogger(__name__)

#: The delegation whitelist, in the order it is handed over (R-C). ``"."`` is the
#: container cgroup directory itself; the two files are the ones shape W needs
#: (``mkdir`` inside it, place a pid in it, force ``+cpu`` on it). ``cpu.max`` is
#: absent **on purpose** -- see the module docstring and §1.4's negative N1.
DELEGATED_ENTRIES: tuple[str, ...] = (".", "cgroup.procs", "cgroup.subtree_control")

#: The one file the delegation deliberately leaves to root. Named here so the
#: report and the tests can point at the same string.
CPU_MAX_ENTRY = "cpu.max"

#: cgroup v2's single hierarchy line in ``/proc/<pid>/cgroup``.
_CGROUP_V2_PREFIX = "0::"

#: A cgroup tree is shallow (``kubepods/<qos>/pod<uid>/<container>`` is four
#: levels, ``docker/<container>`` is two); the bound keeps a walk over a busy
#: node's cgroupfs cheap and keeps it from descending into a bind-mounted
#: filesystem someone hung below it.
_MAX_WALK_DEPTH = 6


class CgroupRefusal(Exception):
    """A named, fail-closed refusal from the delegation: never a silent skip."""


@dataclass(frozen=True)
class Delegation:
    """What one ``delegate-cgroup`` did, as the wire answer spells it.

    ``container_cgroup`` is the path **in this agent's view** (the wire's
    ``containerCgroup``), ``delegated`` is the whitelist that was chowned (the
    names relative to that directory, ``"."`` for the directory itself), and
    ``cpu_max_owner`` is the owner ``cpu.max`` still has afterwards -- ``0:0`` in
    production, which is the evidence that the worker cannot raise its own cap.
    """

    container_cgroup: Path
    delegated: tuple[str, ...]
    cpu_max_owner: str


def container_cgroup_name(kernel_cgroup: str) -> str:
    """The container cgroup directory's name, as the kernel spells it.

    ``/../<container-id>`` (compose) and ``/../../../burstable/pod<uid>/<cri>``
    (k8s) both end in the directory that holds the container's init, and that
    name is what the agent's own view is searched by. A reading that names no
    directory (empty, ``.``, ``..``) is a named refusal: nothing downstream may
    turn it into a guess.
    """
    name = kernel_cgroup.rstrip("/").rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        raise CgroupRefusal(
            f"the kernel path {kernel_cgroup!r} names no container cgroup "
            "directory: refusing to delegate"
        )
    return name


def container_cgroup_in_view(*, mount: Path, kernel_cgroup: str) -> Path:
    """The container cgroup directory, located in **this agent's** view.

    Zero matches is "the agent's cgroupfs view does not reach the worker's
    container" (a missing mount, a view narrowed to another pod) and more than
    one is a collision the agent will not choose between; both are named
    refusals, and neither is ever answered with "the first one".
    """
    mount = Path(mount)
    if not mount.is_dir():
        raise CgroupRefusal(
            f"the cgroup mount {mount} is not present in this agent: refusing to "
            "delegate"
        )
    name = container_cgroup_name(kernel_cgroup)
    hits = [
        directory
        for directory in _iter_cgroup_dirs(mount)
        if directory.name == name and (directory / "cgroup.procs").is_file()
    ]
    if not hits:
        raise CgroupRefusal(
            f"the container cgroup {name} named by the kernel is not in the "
            f"agent's cgroup view ({mount}): refusing to delegate"
        )
    if len(hits) > 1:
        raise CgroupRefusal(
            f"more than one cgroup directory in the agent's view ({mount}) is "
            f"named {name}: refusing (ambiguous)"
        )
    return hits[0]


def delegate_worker_subtree(
    *,
    mount: Path,
    container_cgroup: Path,
    worker_uid: int,
    chown: Callable[[Path, int, int], None] | None = None,
) -> tuple[str, ...]:
    """Hand the whitelist to the worker uid; return the delegated entry names.

    ``chown`` is injectable for the one reason the rest of this agent injects its
    privileged steps: the local lanes do not run as root, so the *judgement* is
    testable without the privilege (the container lanes drive the real
    ``os.chown`` against a real kernel). Idempotent by construction -- a second
    delegation is the same three chowns.

    The target must be a real container cgroup (the directory, ``cgroup.procs``
    and ``cgroup.subtree_control`` all present) **and** inside the mount: the
    path was derived by the agent, but a view that has been narrowed or replaced
    must fail as a named refusal rather than chown something else.
    """
    mount = Path(mount)
    container_cgroup = Path(container_cgroup)
    chown = chown or os.chown
    if not mount.is_dir():
        raise CgroupRefusal(
            f"the cgroup mount {mount} is not present in this agent: refusing to "
            "delegate"
        )
    if not _is_within(mount=mount, path=container_cgroup):
        raise CgroupRefusal(
            f"the container cgroup {container_cgroup} is not under the cgroup "
            f"mount {mount}: refusing to delegate"
        )
    if not container_cgroup.is_dir():
        raise CgroupRefusal(
            f"the container cgroup {container_cgroup} is not a directory in the "
            "agent's view: refusing to delegate"
        )
    targets: list[tuple[str, Path]] = []
    for name in DELEGATED_ENTRIES:
        target = container_cgroup if name == "." else container_cgroup / name
        if not target.exists():
            raise CgroupRefusal(
                f"the container cgroup {container_cgroup} carries no {name}: "
                "refusing to delegate"
            )
        targets.append((name, target))
    for _name, target in targets:
        # uid and gid are the same value: the worker image runs as
        # ``65534:65534`` (and R-C hands over "the worker uid"), so the two are
        # one number here -- the caller read it from the kernel, not from this
        # process's ``getuid()`` (which is root on face B).
        #
        # A ``chown`` that fails (``EPERM``/``EROFS`` on face A, ``ENOENT`` for a
        # directory that raced away between the shape check and here) is turned
        # into the same named :class:`CgroupRefusal` as every other failure
        # above: ``app.py`` only maps ``LookupRefusal``/``CgroupRefusal`` onto
        # its 502, so an uncaught ``OSError`` would answer the op with an
        # anonymous 500 instead of a greppable ``cgroup-refusal``. The
        # direction is unchanged either way (the control plane sees a failure,
        # the worker's create is refused by name), but the name is what an
        # operator greps for.
        try:
            chown(target, int(worker_uid), int(worker_uid))
        except OSError as exc:
            raise CgroupRefusal(
                f"the delegation could not hand {target} to uid "
                f"{int(worker_uid)} ({type(exc).__name__}: {exc}): refusing to "
                "delegate"
            ) from exc
    return tuple(name for name, _target in targets)


def cpu_max_owner(container_cgroup: Path) -> str:
    """``uid:gid`` of the container cgroup's ``cpu.max`` -- ``0:0`` in production.

    Read *after* the delegation on purpose: it is the wire answer's evidence
    that ``cpu.max`` stayed root's. A container cgroup without one is not the
    directory shape W expects, so it is a named refusal.
    """
    container_cgroup = Path(container_cgroup)
    try:
        info = os.stat(container_cgroup / CPU_MAX_ENTRY)
    except OSError as exc:
        raise CgroupRefusal(
            f"the container cgroup {container_cgroup} carries no {CPU_MAX_ENTRY}: "
            "refusing to delegate"
        ) from exc
    return f"{info.st_uid}:{info.st_gid}"


class WorkerCgroupDelegator(Protocol):
    """The service's seam: one anchor's kernel path in, one delegation out."""

    def delegate(
        self, *, mount: Path, kernel_cgroup: str, worker_uid: int
    ) -> Delegation: ...


class ProcCgroupDelegator:
    """The shipped delegator: locate, chown the whitelist, name ``cpu.max``'s owner.

    Nothing here resolves a path from the request: ``kernel_cgroup`` comes from
    :class:`c3_agent.lookup.ProcLookup` (the anchor), and the directory in the
    agent's own view is found by the name the kernel spelled.
    """

    def delegate(
        self, *, mount: Path, kernel_cgroup: str, worker_uid: int
    ) -> Delegation:
        container_cgroup = container_cgroup_in_view(
            mount=mount, kernel_cgroup=kernel_cgroup
        )
        delegated = delegate_worker_subtree(
            mount=mount, container_cgroup=container_cgroup, worker_uid=worker_uid
        )
        logger.info(
            "c3-agent delegate-cgroup: %s -> uid %d (%s)",
            container_cgroup,
            worker_uid,
            ",".join(delegated),
        )
        return Delegation(
            container_cgroup=container_cgroup,
            delegated=delegated,
            cpu_max_owner=cpu_max_owner(container_cgroup),
        )


def _is_within(*, mount: Path, path: Path) -> bool:
    """Is ``path`` strictly below ``mount``? Lexical, so a missing dir answers.

    No symlink resolution: cgroupfs is a kernfs tree whose directories are the
    thing being handed over, and following links out of the view is exactly what
    this check exists to refuse.
    """
    mount_parts = os.path.normpath(str(mount)).split(os.sep)
    path_parts = os.path.normpath(str(path)).split(os.sep)
    return (
        len(path_parts) > len(mount_parts)
        and path_parts[: len(mount_parts)] == mount_parts
    )


def _iter_cgroup_dirs(mount: Path) -> Iterator[Path]:
    """Every directory below ``mount``, depth-bounded, never following links."""
    stack: list[tuple[Path, int]] = [(mount, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth:
            yield directory
        if depth >= _MAX_WALK_DEPTH:
            continue
        try:
            children = sorted(
                entry
                for entry in directory.iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
        except OSError:
            # A cgroup directory the walk cannot list is not a match; the
            # candidate the kernel named is still looked for elsewhere.
            continue
        stack.extend((child, depth + 1) for child in children)


if __name__ == "__main__":  # pragma: no cover - there is nothing to run
    raise SystemExit("c3_agent.cgroups is a library: the agent imports it")
