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
from pathlib import Path

import pytest

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
