"""Task 0（根重切）：介质归属与命名空间归属。

今天的形状是**一根** ``E2B_WORKSPACE_BASE`` 同时决定三件事 —— 沙箱树在哪、平台
共享命名空间（``_snapshots`` / ``_migrate``）挂在它下面的哪、以及"树是不是共享"
这个迁移判据取什么值。三件事共用一根，"把树搬去节点本地"就不是改一个值：改了它，
平台命名空间会一起被拖到本地盘，跨节点的读者再也看不到。

本模块钉住拆开的头三条不变量：

1. 快照载荷与迁移中转的根是**平台命名空间根**（部署命名了共享根时是它），不是树根；
2. ``E2B_NODE_STATE_BASE`` 是白名单里**自己的一根**（第五根），没命名时不凭空多出来；
3. 迁移判据是具名的 ``E2B_TREES_SHARED``，**不再**从 ``shared_workspace_root``
   的真假推 —— 那两个值重切之后会同时设着。
"""

from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from control_plane import api
from control_plane.config import Settings as ControlPlaneSettings
from control_plane.file_ops import ControlPaths
from envd_service import agent as agent_module
from gateway_common import paths

WS = Path("/ws")
STATE = Path("/st")
SHARED = Path("/sh")
CACHE = Path("/ic")
NODE_STATE = Path("/ns")

REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOXES_PY = Path(inspect.getfile(api.sandboxes))
KUBECTL = shutil.which("kubectl")
K8S_BASE = REPO_ROOT / "deploy" / "k8s"
K8S_OVERLAY = REPO_ROOT / "deploy" / "k8s-k0s"
COMPOSE_LANES = (
    REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml",
    REPO_ROOT / "deploy" / "compose" / "docker-compose.multinode.yml",
    REPO_ROOT / "deploy" / "stack" / "docker-compose.prod.yml",
)


# --- 1. 平台命名空间根：共享根优先，没有共享根才回落到树根 -------------------


def test_the_snapshot_payload_root_is_the_shared_volume():
    """``_snapshots`` 的记录由控制面写在共享导出根上（``control_plane/app.py``
    的 ``platform_root``），载荷必须跟着它走 —— 否则就是今天那两个命名空间。"""
    assert (
        paths.snapshot_payload_dir(WS, "snap_1", shared_root=SHARED)
        == SHARED / "_snapshots" / "snap_1"
    )


def test_the_migrate_staging_root_is_the_shared_volume():
    """中转目录的读者是**另一个节点**的目标 agent（``_import_sandbox_archive``），
    挂在树根命名空间下那边看不到。"""
    assert paths.migrate_staging_dir(WS, shared_root=SHARED) == SHARED / "_migrate"


def test_without_a_shared_root_both_fall_back_to_the_workspace_base():
    """没命名共享根的部署（compose、单机、测试）字面就是今天的形状，一个字符
    都不该变。"""
    assert paths.snapshot_payload_dir(WS, "snap_1") == WS / "_snapshots" / "snap_1"
    assert paths.migrate_staging_dir(WS) == WS / "_migrate"


def test_the_agent_derives_no_snapshot_or_migrate_path_from_the_workspace_base():
    """``envd_service/agent.py`` 里那四处（``:3082``、``:4199``、``:4260``、
    ``:4357``、``:4424``）都必须走 helper —— 漏一处就是"一半写在共享、一半写在
    本地"，而那种错在单节点冒烟里看不出来。"""
    source = Path(inspect.getfile(agent_module)).read_text(encoding="utf-8")
    assert 'workspace_base / "_snapshots"' not in source
    assert 'workspace_base / "_migrate"' not in source


def test_the_control_plane_stages_migrations_under_the_shared_root():
    source = SANDBOXES_PY.read_text(encoding="utf-8")
    assert 'workspace_base / "_migrate"' not in source


# --- 2. 第五根：``E2B_NODE_STATE_BASE`` -------------------------------------


def test_the_node_state_base_is_its_own_root_and_comes_second():
    """顺序与 ``priv_common.c::priv_root_paths()`` 逐字同序：树根、节点本地
    state、共享 state、共享根、镜像缓存。"""
    control = ControlPaths(
        workspace_base=WS,
        state_base=STATE,
        node_state_base=NODE_STATE,
        shared_volume_root=SHARED,
        image_cache_dir=CACHE,
    )
    assert control.roots() == (WS, NODE_STATE, STATE, SHARED, CACHE)


def test_a_deployment_that_names_no_node_state_base_gains_no_fifth_root():
    """重切**不加介质**：今天没命名这个根的部署，根列表逐字与今天相同。"""
    control = ControlPaths(
        workspace_base=WS,
        state_base=STATE,
        shared_volume_root=SHARED,
        image_cache_dir=CACHE,
    )
    assert control.roots() == (WS, STATE, SHARED, CACHE)


def test_the_node_state_root_is_deduped_like_every_other_root():
    """根列表的去重规则不变：指到同一目录的两根只算一根（``state`` 与
    ``workspace`` 相同是 N27 前的形状，今天仍然合法）。"""
    control = ControlPaths(workspace_base=WS, state_base=WS, node_state_base=WS)
    assert control.roots() == (WS,)


def test_the_privileged_helper_whitelists_the_node_state_root():
    """白名单在 C 侧（``c3_agent/priv/priv_common.c``）与
    ``ControlPaths.roots()`` 各有一份，两边不一致时特权操作会按"不在根下"拒绝。"""
    common = (REPO_ROOT / "c3_agent/priv/priv_common.c").read_text(encoding="utf-8")
    header = (REPO_ROOT / "c3_agent/priv/priv_common.h").read_text(encoding="utf-8")
    assert "E2B_NODE_STATE_BASE" in common
    assert "priv_node_state_base" in common
    assert "#define PRIV_MAX_ROOTS 5" in header


# --- 3. 迁移判据具名 --------------------------------------------------------


def test_trees_shared_defaults_to_todays_shape(monkeypatch):
    """没显式命名时沿用今天的行为：``E2B_SHARED_WORKSPACE_ROOT`` 设着 ⇒ 树共享。"""
    monkeypatch.delenv("E2B_TREES_SHARED", raising=False)
    monkeypatch.setenv("E2B_SHARED_WORKSPACE_ROOT", "/sh")
    assert ControlPlaneSettings(workspace_base=WS).trees_shared is True


def test_trees_shared_defaults_to_false_when_nothing_is_shared(monkeypatch):
    monkeypatch.delenv("E2B_TREES_SHARED", raising=False)
    monkeypatch.delenv("E2B_SHARED_WORKSPACE_ROOT", raising=False)
    assert ControlPlaneSettings(workspace_base=WS).trees_shared is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("0", False), ("true", True), ("false", False), ("", True)],
)
def test_trees_shared_is_named_explicitly(monkeypatch, raw, expected):
    """重切之后 ``E2B_SHARED_WORKSPACE_ROOT`` **仍然要设着**（它就是共享根），
    所以判据必须能独立命名，否则"树在本地"这条永远翻不过去。

    空串算**没命名**（回落到旧问题 ⇒ ``/sh`` 设着就还是共享）—— 与仓库里
    每一个 base 的规则一致（``resolve_state_base``），这样 k8s 清单里一个
    ``value: ""`` 不会把整个舰队的树翻到本地盘上。
    """
    monkeypatch.setenv("E2B_SHARED_WORKSPACE_ROOT", "/sh")
    monkeypatch.setenv("E2B_TREES_SHARED", raw)
    assert ControlPlaneSettings(workspace_base=WS).trees_shared is expected


def test_the_migration_route_reads_the_named_judge_not_the_shared_root():
    source = SANDBOXES_PY.read_text(encoding="utf-8")
    assert "shared = bool(settings.shared_workspace_root)" not in source
    assert "shared = settings.trees_shared" in source


# --- 4. 清单：`_snapshots` / `_migrate` 的落点跟着平台命名空间根 -------------
#
# 代码把这两条路径挂到平台命名空间根上只是前半步：根换了，**谁在那里建目录、
# 谁把它挂成 subPath、谁保证属主**也都要跟着换。漏一处的形状是"一半写在共享、
# 一半写在本地"，而它在单节点冒烟里看不出来（两边恰好是同一个目录）。


def _rendered(overlay: Path) -> str:
    proc = subprocess.run(
        [KUBECTL, "kustomize", str(overlay)],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _rendered_workload(rendered: str, kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == kind
        and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}: {len(matches)}"
    return matches[0]


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_the_control_plane_mounts_the_migration_staging_at_the_shared_root():
    """控制面的那份写权限必须落在**平台命名空间根**上。

    它的整卷是只读的（OBS-9），可写的每一处都是一条 subPath —— 而 subPath 的
    源就是"这个目录在卷上的位置"。N58 之后 `_migrate` 在 export 根上，所以源也
    只能是 `_migrate`：写成 `workspaces/_migrate` 会让 pod 停在
    ContainerCreating（源不存在），或者更糟 —— 迁移暂存又回到节点本地的树根
    命名空间里，目标节点看不见它。
    """
    plane = _rendered_workload(_rendered(K8S_OVERLAY), "Deployment", "control-plane")
    container = plane["spec"]["template"]["spec"]["containers"][0]
    shared = [m for m in container["volumeMounts"] if m["name"] == "shared"]
    subs = {m["subPath"]: m for m in shared if "subPath" in m}
    assert subs["_migrate"] == {
        "name": "shared",
        "mountPath": "/var/lib/e2b-sandboxes/_migrate",
        "subPath": "_migrate",
    }
    assert [m for m in shared if m.get("subPath", "").startswith("workspaces")] == []


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_workspace_root_init_creates_both_namespaces_on_the_shared_export_root():
    """`workspace-root-init` 是唯一在新卷上建这些根的容器。

    两个失败它挡着：① `_migrate` 是控制面唯一可写 subPath 的**源**，缺了那个 pod
    就停在 ContainerCreating；② `_snapshots` 的**属主**必须是 worker 的 65534 ——
    载荷端点写它下面的每一个 id，root:0755 是"第一次建快照才 EACCES"的那种静默故障。
    N58 之后两条都在 export 根上，所以判据也必须在 export 根上。
    """
    agent = _rendered_workload(_rendered(K8S_BASE), "DaemonSet", "e2b-c3-agent")
    inits = {c["name"]: c for c in agent["spec"]["template"]["spec"]["initContainers"]}
    lines = [line.strip() for line in inits["workspace-root-init"]["command"][2].splitlines()]
    for expected in (
        'mkdir -p "$shared/_migrate"',
        'mkdir -p "$shared/_snapshots"',
        'chown 65534:65534 "$shared/_snapshots" 2>/dev/null ||',
        'chmod 0755 "$shared/_snapshots" 2>/dev/null ||',
        'snap_owner="$(stat -c %u "$shared/_snapshots")"',
        'for target in "$base" "$state" "$shared/_migrate"; do',
    ):
        assert expected in lines, expected
    # ...and the tree-root spellings are gone: an init that recreates
    # `<workspaces>/_snapshots` on every pod start would resurrect the second
    # namespace the reslice just merged away.
    assert [line for line in lines if '"$base/_snapshot' in line] == []
    assert [line for line in lines if '"$base/_migrate' in line] == []


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_the_control_plane_names_the_trees_shared_judge():
    """部署自己把判据写出来，而不是靠"共享根设着"这个推论。

    重切之后 `E2B_SHARED_WORKSPACE_ROOT` **仍然要设着**（它就是共享根），所以
    `bool(shared_workspace_root)` 再也回答不了"树在哪"。清单里显式写 `1` 让这一行
    可 grep、可评审，也让 Task 3 的翻转就是这一个字符。
    """
    plane = _rendered_workload(_rendered(K8S_OVERLAY), "Deployment", "control-plane")
    env = {
        e["name"]: e.get("value")
        for e in plane["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["E2B_TREES_SHARED"] == "1"
    # 判据与被判据的对象同时可见：共享根还在（它就是 `_snapshots` 的根），
    # 而树根是它下面的一层。
    assert env["E2B_SHARED_WORKSPACE_ROOT"] == "/var/lib/e2b-sandboxes"
    assert env["E2B_WORKSPACE_BASE"] == "/var/lib/e2b-sandboxes/workspaces"


def test_every_compose_lane_already_owns_the_two_namespaces_at_its_own_root():
    """compose 车道**不需要**跟着 N58 改，而且这条要能被失败。

    三条 compose 车道今天就把树根与平台命名空间根放在同一个目录
    （`E2B_WORKSPACE_BASE` = `E2B_SHARED_*_ROOT` = 那个挂载点），所以
    `_snapshots`/`_migrate` 本来就在 export 根上 —— `OWNED_DIRS` 早就是重切后的
    形状。把这条写下来是为了挡住"顺手把 compose 也改成 `workspaces/_migrate`"：
    那会让属主交接去 chown 一个不存在的路径，而 compose 的 `image-cache-init`
    对缺失条目是**新建**，不是报错。
    """
    for path in COMPOSE_LANES:
        text = path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "OWNED_DIRS:" in line]
        assert len(lines) == 1, path
        dirs = lines[0].split("OWNED_DIRS:", 1)[1].split()
        assert any(d.endswith("/_migrate") for d in dirs), (path, dirs)
        assert any(d.endswith("/_snapshots") for d in dirs), (path, dirs)
        assert [d for d in dirs if "workspaces/" in d] == [], (path, dirs)
        # 每一份 `E2B_WORKSPACE_BASE` 都必须与某个 `E2B_SHARED_*_ROOT` 同根；
        # 只有同根时"`_snapshots` 长在树根下"与"长在 export 根下"才是同一件事。
        roots = re.findall(r"E2B_SHARED_(?:VOLUME|WORKSPACE)_ROOT:\s*(\S+)", text)
        bases = re.findall(r"E2B_WORKSPACE_BASE:\s*(\S+)", text)
        assert roots, path
        for base in bases:
            assert base == roots[0], (path, base, roots)
