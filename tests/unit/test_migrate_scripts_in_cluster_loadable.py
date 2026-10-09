"""集群内 Job 只能加载它真正随 ConfigMap 装进去的东西。

2026-10-09 的生产尝试：`migrate-state-owner.sh --uid 65533 --apply` 的 Job 一启动就退 1 ——
ConfigMap `state-owner-migrate` 只装了 `migrate-state-owner.sh` 一个文件，而脚本顶层无条件下
`. "$SCRIPT_DIR/lib/cluster-guard.sh"`（N71 闸门 `1d0852b`，2026-10-03）⇒ 容器里
`/scripts/lib/cluster-guard.sh` 不存在，`set -e` 在第一个 `chown` 之前就收工。

既有的钉子只**渲染**Job（`test_state_owner_migrate.py` / `test_migrate_state_owner_uid.py`），
从不真的把渲染出来的那条路加载起来；`--root` 彩排也从不 source 那道闸门。所以这条路上
"脚本引用的东西" 与 "ConfigMap 装的东西" 从来没对过账 —— 这个文件补的就是这类钉子：

  1. Job 的 `command` 指向的 `/scripts/<key>` 必须恰好是 ConfigMap 用
     `--from-file=<key>=$SCRIPT_PATH` 装进去的那一个文件（并且真的只有它）；
  2. 把那个文件单独复制出去（没有 `lib/`）再 `bash -n`，检查的就是 ConfigMap 带着的原文；
  3. 把那棵"只有这一个文件"的树按 Job 的方式加载一次（`--in-cluster`）：它必须进到引擎、
     给出计划，而不是死在 `No such file or directory` 上；
  4. 静态兜底：闸门的 `source` 只在 `IN_CLUSTER != 1` 时到达 —— 运维路径与今天逐字相同。

RED（对 `1d0852b..ab98313` 的原文，把脚本单独复制出去跑 `--in-cluster`）：exit 1，
stderr 第一行 `<script>: line <N>: <dir>/lib/cluster-guard.sh: No such file or directory`。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "deploy" / "scripts"
JOB_MANIFESTS = REPO / "deploy" / "k8s-k0s"

#: (脚本, Job 清单, ConfigMap 名, 喂给 --in-cluster 的根目录里的那条路径, 计划成功的 SUMMARY 行)
#: 根目录里那一条是引擎判"这是本平台 export 根"的最小形状；`--in-cluster`（默认 dry-run）
#: 只跑计划，所以这三个 case 都不写盘。
CASES = (
    (
        "migrate-state-owner.sh",
        "state-owner-migrate.yaml",
        "state-owner-migrate",
        "state",
        "SUMMARY mode=plan targets=8 chowned=0 missing=7",
    ),
    (
        "migrate-state-base.sh",
        "state-base-migrate.yaml",
        "state-base-migrate",
        "_runtime",
        "SUMMARY mode=plan moves=1 dirs=3 todo=4 done=0 unknown=0",
    ),
)

_CASES = tuple(
    pytest.param(*case, id=case[0]) for case in CASES
)


def _clean_env() -> dict[str, str]:
    """一个不指向任何集群的环境：这些 case 全走 `--in-cluster`，不碰 kubectl。"""
    env = dict(os.environ)
    env.pop("KUBECONFIG", None)
    return env


def _from_file(script: Path) -> tuple[str, str]:
    """脚本自己那条 `--from-file="<key>=<path>"` —— ConfigMap 实际装的东西。"""
    matches = re.findall(r'--from-file="([^"=]+)=([^"=]+)"', script.read_text(encoding="utf-8"))
    assert len(matches) == 1, matches
    ((key, source),) = matches
    return key, source


def _loaded_script(manifest: Path) -> tuple[str, str]:
    """Job 清单里那条 `command: ["bash", "/scripts/<key>"]` 与它挂的 ConfigMap。"""
    job = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    (container,) = job["spec"]["template"]["spec"]["containers"]
    command, path = container["command"]
    assert command == "bash"
    mounts = {
        mount["name"]: mount
        for mount in container["volumeMounts"]
    }
    script_mount = mounts["script"]
    assert script_mount["mountPath"] == "/scripts"
    volumes = {
        volume["name"]: volume
        for volume in job["spec"]["template"]["spec"]["volumes"]
    }
    return path, volumes["script"]["configMap"]["name"]


@pytest.mark.parametrize("script_name, manifest_name, configmap, root_leaf, summary", _CASES)
def test_the_job_loads_exactly_the_file_the_configmap_ships(
    script_name: str,
    manifest_name: str,
    configmap: str,
    root_leaf: str,
    summary: str,
) -> None:
    """`/scripts/<key>` 就是 `--from-file=<key>=$SCRIPT_PATH` —— 装什么、load 什么，一个不多。"""
    script = SCRIPTS / script_name
    key, source = _from_file(script)
    loaded_path, mounted_configmap = _loaded_script(JOB_MANIFESTS / manifest_name)
    assert loaded_path == f"/scripts/{key}"
    assert mounted_configmap == configmap
    # ConfigMap 的 key 就是文件名，源就是脚本自己（Job 里那份逐字是仓库里这份）。
    assert source == "$SCRIPT_PATH"
    assert key == script.name


@pytest.mark.parametrize("script_name, manifest_name, configmap, root_leaf, summary", _CASES)
def test_the_configmap_text_parses_under_the_shell_the_job_uses(
    script_name: str,
    manifest_name: str,
    configmap: str,
    root_leaf: str,
    summary: str,
    tmp_path: Path,
) -> None:
    """`sh -n`（Job 用的是 `bash`，所以按 bash 解析）检查的是 ConfigMap 带着的原文。"""
    key, _ = _from_file(SCRIPTS / script_name)
    shipped = tmp_path / "scripts"
    shipped.mkdir()
    shutil.copyfile(SCRIPTS / script_name, shipped / key)
    proc = subprocess.run(
        ["bash", "-n", str(shipped / key)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


@pytest.mark.parametrize("script_name, manifest_name, configmap, root_leaf, summary", _CASES)
def test_the_in_cluster_path_loads_with_only_the_shipped_file(
    script_name: str,
    manifest_name: str,
    configmap: str,
    root_leaf: str,
    summary: str,
    tmp_path: Path,
) -> None:
    """把 ConfigMap 的那一个文件单独放出去，按 Job 的方式 `--in-cluster` 加载一次。

    这就是 2026-10-09 生产上跑的形状：`SCRIPT_DIR` 旁边**没有** `lib/`。脚本必须进到引擎、
    把只读计划打完（exit 0），而不是死在闸门那行 `source` 上（RED：exit 1，
    `.../lib/cluster-guard.sh: No such file or directory`）。
    """
    key, _ = _from_file(SCRIPTS / script_name)
    shipped = tmp_path / "scripts"
    shipped.mkdir()
    shutil.copyfile(SCRIPTS / script_name, shipped / key)
    assert not (shipped / "lib").exists()

    root = tmp_path / "export"
    root.mkdir()
    (root / root_leaf).mkdir()
    env = _clean_env()
    env["MIGRATE_ROOT"] = str(root)
    env["MIGRATE_WORKER_REPLICAS"] = "0"
    proc = subprocess.run(
        ["bash", str(shipped / key), "--in-cluster"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == ""
    assert summary in proc.stdout.splitlines()


@pytest.mark.parametrize("script_name, manifest_name, configmap, root_leaf, summary", _CASES)
def test_the_gate_is_loaded_only_on_the_local_path(
    script_name: str,
    manifest_name: str,
    configmap: str,
    root_leaf: str,
    summary: str,
) -> None:
    """静态兜底：闸门的 `source` 恰好夹在 `IN_CLUSTER != 1` 的 if 里，运维路径不变。"""
    lines = [line.strip() for line in (SCRIPTS / script_name).read_text(encoding="utf-8").splitlines()]
    assert lines.count('. "$SCRIPT_DIR/lib/cluster-guard.sh"') == 1
    index = lines.index('. "$SCRIPT_DIR/lib/cluster-guard.sh"')
    assert lines[index - 1] == 'if [ "$IN_CLUSTER" != "1" ]; then'
    assert lines[index + 1] == "fi"
