"""N83 Phase 1 · Task 3: the agent's one-shot cgroup delegation.

形态 W（``docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md`` §3.2）把
"每沙箱一个 cgroup"拆成两半：**worker**（65534、零 capability）自己建 ``sbx_<id>``
并放置槽位进程；**agent 面 B**（root + ``CHOWN/DAC_OVERRIDE/FOWNER``）只做**一次性
委派** —— 把 worker **容器** cgroup 的目录与两个控制文件 chown 给 65534，之后不再参与。

这一层钉四件事，每一条都是"宁可具名拒绝，绝不静默跳过"：

* **定位**（``ProcLookup.worker_container_cgroup``）：按车道取锚点（k8s 的
  ``pod<uid>`` / compose 的容器 id），候选必须**同时**带着锚点**且是容器 init**
  （``NSpid`` 末位为 1）—— ``kubectl exec`` 的兄弟 cgroup 也带着锚点，只有 init 是
  worker 自己。零个 / 多于一个 ⇒ 具名 ``LookupRefusal``。
* **委派白名单**（``c3_agent.cgroups.delegate_worker_subtree``）：**只** chown
  *目录 + ``cgroup.procs`` + ``cgroup.subtree_control``*。``cpu.max`` **必须**留在
  白名单之外 —— §1.4 负例 N1 实测：不委派 ``cpu.max``，worker 写自己的 CPU 上限就是
  EACCES，这是"worker 抬不了自己的额度"的钉子。
* **视图路径**：``/proc/<pid>/cgroup`` 是**相对本进程 cgroup namespace 根**的读数
  （本机 Docker VM 实测：另一个容器读作 ``0::/../<container-id>``，而宿主视图里它在
  ``/docker/<container-id>``），所以落回 agent 的挂载视图靠**目录名**定位，直接拼接
  会把 ``..`` 走出挂载根。
* **worker uid 来自内核**：请求体（R-B）只带锚点、不带 uid；面 B 的 uid 取**被定位
  到的那个容器 init** 在 ``/proc/<pid>/status`` 里的读数（D25 的同一次读），绝不取
  agent 自己的 ``getuid()``（agent 是 root，那会把目录交给 root）。

本车道用合成的 ``/proc`` 与 ``tmp_path`` 里的合成 cgroupfs 驱动；真内核那条由既有的
``tests/contract/`` 车道负责。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from c3_agent.app import create_app
from c3_agent.cgroups import (
    CgroupRefusal,
    Delegation,
    ProcCgroupDelegator,
    container_cgroup_in_view,
    cpu_max_owner,
    delegate_worker_subtree,
)
from c3_agent.config import Settings
from c3_agent.lookup import LookupRefusal, ProcLookup

TOKEN = "c3-agent-sekret"
HOST = "k0s-worker-0"
WORKER = "e2b-worker-0"
#: 两个锚点，两条车道各一个。
POD_UID = "6d3cdd7b-3a5e-4a1f-9a6b-0c1d2e3f4a5b"
CONTAINER_ID = "e4a98a0c528215e380373d982fc1fccaf49787a8f29ad785b64220ce1e16ead9"
ANCHOR = CONTAINER_ID[:12]
#: 另一个容器的 id（同 pod 里的 ``kubectl exec`` 兄弟目录）。
EXEC_ID = "cfee67b0a1d2e3f405162738495a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6"
WORKER_UID = 65534
WORKER_GID = 65534
#: 容器 init 与 exec 兄弟在宿主 pid 空间里的 pid（合成树里的那两个）。
INIT_PID = 4321
EXEC_PID = 4322


# --------------------------------------------------------------------------- #
# 合成 /proc 与合成 cgroup 视图
# --------------------------------------------------------------------------- #
def _proc(proc_root: Path) -> Path:
    proc_root.mkdir(parents=True, exist_ok=True)
    return proc_root


def _process(
    proc_root: Path,
    pid: int,
    *,
    cgroup: str,
    nspid: str,
    uid: int = WORKER_UID,
    gid: int = WORKER_GID,
    comm: str = "python3",
) -> Path:
    """一个 ``/proc/<pid>`` 条目，带 lookup 会读的那两个文件。"""
    entry = _proc(proc_root) / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "status").write_text(
        f"Name:\t{comm}\n"
        f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        f"Gid:\t{gid}\t{gid}\t{gid}\t{gid}\n"
        f"NSpid:\t{nspid}\n",
        encoding="utf-8",
    )
    (entry / "cgroup").write_text(cgroup, encoding="utf-8")
    return entry


def _k8s_cgroup(container: str) -> str:
    """k8s 车道：读者是私有 cgroupns，所以读数带 ``..``（本机 Docker 实测同形）。"""
    return f"0::/../../../burstable/pod{POD_UID}/{container}"


def _compose_cgroup(container: str) -> str:
    """compose 车道：本机 Docker VM 实测 ``0::/../<container-id>``。"""
    return f"0::/../{container}"


def _cgroup_mount(tmp_path: Path, *, lane: str, container: str) -> tuple[Path, Path]:
    """一棵合成 cgroupfs：挂载根下摆着某个容器的 cgroup 目录。"""
    mount = tmp_path / "host-cgroup"
    parent = (
        mount / "kubepods" / "burstable" / f"pod{POD_UID}"
        if lane == "k8s"
        else mount / "docker"
    )
    container_dir = parent / container
    container_dir.mkdir(parents=True, exist_ok=True)
    (container_dir / "cgroup.procs").write_text("", encoding="utf-8")
    (container_dir / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (container_dir / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    return mount, container_dir


def _lookup(proc_root: Path) -> ProcLookup:
    return ProcLookup(proc_root=proc_root)


# --------------------------------------------------------------------------- #
# ① 定位：容器 init 而不是 exec 兄弟；两条车道各一个锚点
# --------------------------------------------------------------------------- #
def test_the_k8s_anchor_selects_the_container_init_dir_not_the_exec_sibling(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, INIT_PID, cgroup=_k8s_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1")
    # ``kubectl exec`` 的兄弟 cgroup 也带 ``pod<uid>``，但它不是容器 init。
    _process(
        root,
        EXEC_PID,
        cgroup=_k8s_cgroup(EXEC_ID),
        nspid=f"{EXEC_PID}\t71",
        uid=10000,
        gid=10000,
        comm="sh",
    )

    assert _lookup(root).worker_container_cgroup(
        node_id=WORKER, pod_uid=POD_UID
    ) == f"/../../../burstable/pod{POD_UID}/{CONTAINER_ID}"


def test_the_compose_anchor_selects_the_container_init_dir_not_the_exec_sibling(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(
        root, INIT_PID, cgroup=_compose_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1"
    )
    _process(
        root,
        EXEC_PID,
        cgroup=_compose_cgroup(EXEC_ID),
        nspid=f"{EXEC_PID}\t71",
        uid=10000,
        gid=10000,
        comm="sh",
    )

    assert _lookup(root).worker_container_cgroup(
        node_id=WORKER, container_id=ANCHOR
    ) == f"/../{CONTAINER_ID}"


def test_an_anchor_no_container_init_carries_is_refused_by_name(tmp_path: Path) -> None:
    root = _proc(tmp_path / "proc")
    # 只有 exec 兄弟带锚点：它是别的进程，不是 worker 自己。
    _process(root, EXEC_PID, cgroup=_k8s_cgroup(EXEC_ID), nspid=f"{EXEC_PID}\t71")

    with pytest.raises(LookupRefusal) as refused:
        _lookup(root).worker_container_cgroup(node_id=WORKER, pod_uid=POD_UID)

    assert str(refused.value) == (
        "worker e2b-worker-0's container holds no process this agent can identify "
        "as the container's init: refusing to locate its container cgroup"
    )


def test_two_container_inits_carrying_one_anchor_are_refused_by_name(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, INIT_PID, cgroup=_k8s_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1")
    _process(root, EXEC_PID, cgroup=_k8s_cgroup(EXEC_ID), nspid=f"{EXEC_PID}\t1")

    with pytest.raises(LookupRefusal) as refused:
        _lookup(root).worker_container_cgroup(node_id=WORKER, pod_uid=POD_UID)

    assert str(refused.value) == (
        "worker e2b-worker-0's container anchor matches more than one "
        "container-init process: refusing (ambiguous)"
    )


def test_a_neighbour_containers_init_is_never_the_answer(tmp_path: Path) -> None:
    """别的容器 init 也在树上，锚点决定谁被选中 —— 两条车道各选各的。"""
    root = _proc(tmp_path / "proc")
    _process(root, INIT_PID, cgroup=_k8s_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1")
    _process(root, EXEC_PID, cgroup=_compose_cgroup(EXEC_ID), nspid=f"{EXEC_PID}\t1")

    assert _lookup(root).worker_container_cgroup(
        node_id=WORKER, pod_uid=POD_UID
    ) == f"/../../../burstable/pod{POD_UID}/{CONTAINER_ID}"
    assert _lookup(root).worker_container_cgroup(
        node_id=WORKER, container_id=EXEC_ID[:12]
    ) == f"/../{EXEC_ID}"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"pod_uid": "not-a-pod-uid"},
            "the pod uid ('not-a-pod-uid') carried for worker e2b-worker-0 is not "
            "a pod uid: refusing",
        ),
        (
            {"container_id": "SHORT"},
            "the container id ('SHORT') carried for worker e2b-worker-0 is not a "
            "container id: refusing",
        ),
        (
            {},
            "worker e2b-worker-0 names no single cgroup anchor (pod uid or "
            "container id): refusing",
        ),
        (
            {"pod_uid": POD_UID, "container_id": ANCHOR},
            "worker e2b-worker-0 names both a pod uid and a container id: "
            "refusing to locate its container cgroup",
        ),
    ],
)
def test_an_unusable_anchor_is_refused_by_name(
    tmp_path: Path, kwargs: dict, message: str
) -> None:
    root = _proc(tmp_path / "proc")

    with pytest.raises(LookupRefusal) as refused:
        _lookup(root).worker_container_cgroup(node_id=WORKER, **kwargs)

    assert str(refused.value) == message


# --------------------------------------------------------------------------- #
# ② 视图定位：内核读数是相对的，落回挂载视图靠目录名
# --------------------------------------------------------------------------- #
def test_the_container_cgroup_is_located_in_the_view_by_its_kernel_name(
    tmp_path: Path,
) -> None:
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="compose", container=CONTAINER_ID
    )

    assert container_cgroup_in_view(
        mount=mount, kernel_cgroup=f"/../{CONTAINER_ID}"
    ) == container_dir


def test_a_container_directory_the_view_does_not_hold_is_a_named_refusal(
    tmp_path: Path,
) -> None:
    mount, _ = _cgroup_mount(tmp_path, lane="k8s", container=CONTAINER_ID)
    missing = EXEC_ID

    with pytest.raises(CgroupRefusal) as refused:
        container_cgroup_in_view(mount=mount, kernel_cgroup=f"/../{missing}")

    assert str(refused.value) == (
        f"the container cgroup {missing} named by the kernel is not in the "
        f"agent's cgroup view ({mount}): refusing to delegate"
    )


def test_a_missing_mount_is_a_named_refusal_not_a_silent_skip(tmp_path: Path) -> None:
    mount = tmp_path / "no-such-cgroup-mount"

    with pytest.raises(CgroupRefusal) as refused:
        container_cgroup_in_view(mount=mount, kernel_cgroup=f"/../{CONTAINER_ID}")

    assert str(refused.value) == (
        f"the cgroup mount {mount} is not present in this agent: refusing to "
        "delegate"
    )


def test_a_kernel_path_that_names_no_directory_is_a_named_refusal(
    tmp_path: Path,
) -> None:
    mount, _ = _cgroup_mount(tmp_path, lane="compose", container=CONTAINER_ID)

    with pytest.raises(CgroupRefusal) as refused:
        container_cgroup_in_view(mount=mount, kernel_cgroup="/")

    assert str(refused.value) == (
        "the kernel path '/' names no container cgroup directory: refusing to "
        "delegate"
    )


# --------------------------------------------------------------------------- #
# ③ 委派白名单：目录 + cgroup.procs + cgroup.subtree_control，cpu.max 不在其中
# --------------------------------------------------------------------------- #
def test_the_whitelist_chowns_the_dir_and_two_files_and_never_cpu_max(
    tmp_path: Path,
) -> None:
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="compose", container=CONTAINER_ID
    )
    chowned: list[tuple[Path, int, int]] = []

    delegated = delegate_worker_subtree(
        mount=mount,
        container_cgroup=container_dir,
        worker_uid=WORKER_UID,
        chown=lambda path, uid, gid: chowned.append((path, uid, gid)),
    )

    assert delegated == (".", "cgroup.procs", "cgroup.subtree_control")
    assert chowned == [
        (container_dir, WORKER_UID, WORKER_UID),
        (container_dir / "cgroup.procs", WORKER_UID, WORKER_UID),
        (container_dir / "cgroup.subtree_control", WORKER_UID, WORKER_UID),
    ]
    assert "cpu.max" not in delegated
    assert container_dir / "cpu.max" not in [entry[0] for entry in chowned]


def test_delegating_twice_is_idempotent(tmp_path: Path) -> None:
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="k8s", container=CONTAINER_ID
    )
    chowned: list[tuple[Path, int, int]] = []

    first = delegate_worker_subtree(
        mount=mount,
        container_cgroup=container_dir,
        worker_uid=WORKER_UID,
        chown=lambda path, uid, gid: chowned.append((path, uid, gid)),
    )
    second = delegate_worker_subtree(
        mount=mount,
        container_cgroup=container_dir,
        worker_uid=WORKER_UID,
        chown=lambda path, uid, gid: chowned.append((path, uid, gid)),
    )

    assert first == second
    assert len(chowned) == 6


def test_a_container_cgroup_outside_the_mount_is_a_named_refusal(
    tmp_path: Path,
) -> None:
    mount, _ = _cgroup_mount(tmp_path, lane="compose", container=CONTAINER_ID)
    elsewhere = tmp_path / "elsewhere" / "cgroup"
    elsewhere.mkdir(parents=True, exist_ok=True)
    (elsewhere / "cgroup.procs").write_text("", encoding="utf-8")
    (elsewhere / "cgroup.subtree_control").write_text("", encoding="utf-8")

    with pytest.raises(CgroupRefusal) as refused:
        delegate_worker_subtree(
            mount=mount,
            container_cgroup=elsewhere,
            worker_uid=WORKER_UID,
            chown=lambda path, uid, gid: None,
        )

    assert str(refused.value) == (
        f"the container cgroup {elsewhere} is not under the cgroup mount "
        f"{mount}: refusing to delegate"
    )


def test_a_whitelist_entry_the_directory_lacks_is_a_named_refusal(
    tmp_path: Path,
) -> None:
    """少一个白名单条目就不是容器 cgroup 的形状：具名拒绝，chown 一步不做。"""
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="k8s", container=CONTAINER_ID
    )
    (container_dir / "cgroup.subtree_control").unlink()
    chowned: list[Path] = []

    with pytest.raises(CgroupRefusal) as refused:
        delegate_worker_subtree(
            mount=mount,
            container_cgroup=container_dir,
            worker_uid=WORKER_UID,
            chown=lambda path, uid, gid: chowned.append(path),
        )

    assert str(refused.value) == (
        f"the container cgroup {container_dir} carries no cgroup.subtree_control: "
        "refusing to delegate"
    )
    assert chowned == []


def test_cpu_max_owner_reads_the_files_owner_in_uid_gid_form(tmp_path: Path) -> None:
    """真实读数是 ``stat``：线上 ``cpu.max`` 留在 root 手里，所以是 ``0:0``。

    本机车道不是 root，所以这里钉的是"它就是那个文件的属主"（逐字）；线上的形态由
    §1.4 的读数钉住 —— 委派之后 ``cpu.max`` 仍然是 ``0:0``。
    """
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="compose", container=CONTAINER_ID
    )

    assert cpu_max_owner(container_dir) == f"{os.getuid()}:{os.getgid()}"


def test_a_container_dir_without_cpu_max_is_a_named_refusal(tmp_path: Path) -> None:
    mount, container_dir = _cgroup_mount(
        tmp_path, lane="compose", container=CONTAINER_ID
    )
    (container_dir / "cpu.max").unlink()

    with pytest.raises(CgroupRefusal) as refused:
        cpu_max_owner(container_dir)

    assert str(refused.value) == (
        f"the container cgroup {container_dir} carries no cpu.max: refusing to "
        "delegate"
    )


# --------------------------------------------------------------------------- #
# ④ 服务：op、锚点、回答形状
# --------------------------------------------------------------------------- #
class _StubDelegator:
    """记录服务交给委派器的三个参数，回答一个固定结论。"""

    def __init__(
        self,
        *,
        container_cgroup: Path = Path("/host-cgroup/docker/fixed"),
        delegated: tuple[str, ...] = (".", "cgroup.procs", "cgroup.subtree_control"),
        cpu_max_owner: str = "0:0",
    ) -> None:
        self.calls: list[tuple[Path, str, int]] = []
        self._container_cgroup = container_cgroup
        self._delegated = delegated
        self._cpu_max_owner = cpu_max_owner

    def delegate(
        self, *, mount: Path, kernel_cgroup: str, worker_uid: int
    ) -> Delegation:
        self.calls.append((mount, kernel_cgroup, worker_uid))
        return Delegation(
            container_cgroup=self._container_cgroup,
            delegated=self._delegated,
            cpu_max_owner=self._cpu_max_owner,
        )


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        token=TOKEN, node_id=HOST, cgroup_mount=str(tmp_path / "host-cgroup")
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _app(*, settings: Settings, lookup: ProcLookup, delegator):
    return create_app(settings=settings, lookup=lookup, cgroup_delegator=delegator)


async def _post(app, body: dict, *, token: str | None = TOKEN):
    transport = httpx.ASGITransport(app=app)
    headers = {} if token is None else {"X-Internal-Key": token}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://agent"
    ) as client:
        return await client.post(
            f"/internal/nodes/{HOST}/agent/delegate-cgroup", headers=headers, json=body
        )


async def test_the_delegate_op_answers_the_k8s_anchor_with_the_view_and_cpu_owner(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, INIT_PID, cgroup=_k8s_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1")
    delegator = _StubDelegator(
        container_cgroup=Path(
            f"/host-cgroup/kubepods/burstable/pod{POD_UID}/{CONTAINER_ID}"
        )
    )
    app = _app(
        settings=_settings(tmp_path), lookup=_lookup(root), delegator=delegator
    )

    resp = await _post(app, {"worker": {"node_id": WORKER, "pod_uid": POD_UID}})

    assert resp.status_code == 200
    assert resp.json() == {
        "op": "delegate-cgroup",
        "containerCgroup": (
            f"/host-cgroup/kubepods/burstable/pod{POD_UID}/{CONTAINER_ID}"
        ),
        "delegated": [".", "cgroup.procs", "cgroup.subtree_control"],
        "cpuMaxOwner": "0:0",
    }
    # 交给委派器的是内核读数与**内核**给的 worker uid —— 不是请求里的，也不是
    # agent 自己的 getuid()（agent 是 root）。
    assert delegator.calls == [
        (
            tmp_path / "host-cgroup",
            f"/../../../burstable/pod{POD_UID}/{CONTAINER_ID}",
            WORKER_UID,
        )
    ]


async def test_the_delegate_op_carries_the_compose_anchor_to_the_delegator(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(
        root, INIT_PID, cgroup=_compose_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1"
    )
    delegator = _StubDelegator()
    app = _app(
        settings=_settings(tmp_path), lookup=_lookup(root), delegator=delegator
    )

    resp = await _post(app, {"worker": {"node_id": WORKER, "container_id": ANCHOR}})

    assert resp.status_code == 200
    assert delegator.calls == [
        (tmp_path / "host-cgroup", f"/../{CONTAINER_ID}", WORKER_UID)
    ]


async def test_the_delegate_op_refuses_by_name_when_nothing_carries_the_anchor(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(root, EXEC_PID, cgroup=_k8s_cgroup(EXEC_ID), nspid=f"{EXEC_PID}\t71")
    delegator = _StubDelegator()
    app = _app(
        settings=_settings(tmp_path), lookup=_lookup(root), delegator=delegator
    )

    resp = await _post(app, {"worker": {"node_id": WORKER, "pod_uid": POD_UID}})

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            "worker e2b-worker-0's container holds no process this agent can "
            "identify as the container's init: refusing to locate its container "
            "cgroup"
        )
    }
    assert delegator.calls == []


async def test_the_delegate_op_refuses_by_name_when_the_mount_is_absent(
    tmp_path: Path,
) -> None:
    root = _proc(tmp_path / "proc")
    _process(
        root, INIT_PID, cgroup=_compose_cgroup(CONTAINER_ID), nspid=f"{INIT_PID}\t1"
    )
    mount = tmp_path / "no-such-cgroup-mount"
    app = _app(
        settings=_settings(tmp_path, cgroup_mount=str(mount)),
        lookup=_lookup(root),
        delegator=ProcCgroupDelegator(),
    )

    resp = await _post(app, {"worker": {"node_id": WORKER, "container_id": ANCHOR}})

    assert resp.status_code == 502
    assert resp.json() == {
        "error": (
            f"the cgroup mount {mount} is not present in this agent: refusing "
            "to delegate"
        )
    }


@pytest.mark.parametrize(
    "worker",
    [
        {"node_id": WORKER, "pod_uid": POD_UID, "container_id": ANCHOR},
        {"node_id": WORKER},
    ],
)
async def test_a_body_that_does_not_name_exactly_one_anchor_is_a_shape_refusal(
    tmp_path: Path, worker: dict
) -> None:
    app = _app(
        settings=_settings(tmp_path),
        lookup=_lookup(_proc(tmp_path / "proc")),
        delegator=_StubDelegator(),
    )

    resp = await _post(app, {"worker": worker})

    assert resp.status_code == 422
    assert resp.json() == {
        "error": "the instruction body does not fit DelegateCgroupBody"
    }


async def test_the_delegate_op_needs_the_agent_token(tmp_path: Path) -> None:
    app = _app(
        settings=_settings(tmp_path),
        lookup=_lookup(_proc(tmp_path / "proc")),
        delegator=_StubDelegator(),
    )

    resp = await _post(
        app,
        {"worker": {"node_id": WORKER, "pod_uid": POD_UID}},
        token=None,
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}
