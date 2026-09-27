"""C1 wave 2 Task 6: 平台态属主的一次性迁移被它的契约钉住。

wave 1/2 之后 worker 是 uid 65534，而今天这台 NAS 上的平台态文件（`state/**`、
`_runtime/**`、`.uid_pool.lock`、`_images/**`、`_secrets/**` …）还是 root worker 写下的
0600/0700。这一步是硬前置，所以它被钉两遍：

* **静态**：钉脚本与 Job 的文本 —— dry-run 是默认、唯一的写操作是 `chown`（没有 chmod、
  没有删除、没有 `trap` 清理现场）、worker 不在 0 副本就拒绝、Job 以 root 跑在同一条
  PVC 上且不自动重试、三处占位符让"直接 apply 原文件"什么也做不了；
* **行为**：在没有集群的情况下跑 `--print-plan` 与 `--root` 彩排 —— 计划恰是 8 个平台
  目标；树根下**恰允许 `workspaces/_migrate` 与 `workspaces/_snapshots` 这两条确切条目**
  （前者是控制面唯一可写的 subPath、也是 `workspace-root-init` 建的那个；后者是 **worker
  的快照 payload 根** —— `envd_service/agent.py` 硬编码为 `<workspace_base>/_snapshots`，
  C1 之后 worker 以 uid 65534 往它里面写，属主必须是 worker；控制面的记录根是另一条
  `<export>/_snapshots`），其余任何落在 `workspaces/` 之下的拼写（含
  `workspaces` 本身、它的兄弟、那两条下面的东西、`..` 与符号链接的变体）都被点名拒绝。
  毒化过的 `kubectl`（记录自己被调用过、然后失败）把"这些路径不连集群"变成断言而不是
  承诺。

脚本行按 `strip()` 后精确比对（同 `test_migrate_state_base_script.py`）：重写过的行要在
这里重新对一遍，而不是靠旧行的子串蒙过去。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "migrate-state-owner.sh"
JOB = REPO / "deploy" / "k8s-k0s" / "state-owner-migrate.yaml"
KUSTOMIZATION = REPO / "deploy" / "k8s-k0s" / "kustomization.yaml"

WORKER_UID = 65534
WORKER_GID = 65534

#: `<export>`：集群里那块 PVC（`sandbox-shared`）的挂载点，也是 Job 挂它的路径。
EXPORT_IN_CLUSTER = "/var/lib/e2b-sandboxes"

#: 计划里那 8 个平台目标，**顺序即脚本的打印顺序**（Task 6 brief 的顺序）。
#: `_migrate` 与 `_snapshots` 在 N27 之后落在**树根之下**（前者是控制面在那里的 subPath；
#: 后者是 worker 的快照 payload 根，`envd_service/agent.py` 硬编码
#: `<workspace_base>/_snapshots`），不在 export 根上 —— 迁移工具的目标路径必须与清单和
#: worker 端点同名，否则真机上它们恒 MISSING（`_migrate` 那条就是这个 bug，本轮修的就是它；
#: `_snapshots` 是 Task 8 真机预检新发现的补丁）。控制面的快照**记录**根是另一条
#: `<export>/_snapshots`（`SnapshotRegistry` 建在共享 export 根上），也在计划里。
PLATFORM_TARGETS = (
    "state",
    "workspaces/_migrate",
    "workspaces/_snapshots",
    "_images",
    "_secrets",
    "_snapshots",
    "_templates",
    "_builds",
)

#: 沙箱树。池 uid 的树，不是 worker 的 —— 这个前缀下除 `TREE_ROOT_ALLOWED` 外一律拒绝。
SANDBOX_TREES = "workspaces"

#: 树根下**恰好**放行的两条：控制面的迁移暂存（worker 自己也写它下面那份）与 **worker 的
#: 快照 payload 根**（`<workspace_base>/_snapshots/<id>`；C1 之后 worker 以 uid 65534 往它
#: 里面写 payload，属主必须是 worker。控制面的记录根是另一条 `<export>/_snapshots`）。
MIGRATE_STAGING = "workspaces/_migrate"
SNAPSHOT_STORE = "workspaces/_snapshots"
TREE_ROOT_ALLOWED = (MIGRATE_STAGING, SNAPSHOT_STORE)


def _lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def _embedded(marker: str, path: Path) -> list[str]:
    """`path` 里 `<<'MARKER'` heredoc 的正文（strip 过的行）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip().endswith(f"<<'{marker}'")
    )
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == marker)
    return [line.strip() for line in lines[start + 1 : end]]


def _offline_env(tmp_path: Path) -> dict[str, str]:
    """一个 `kubectl` 会记录自己被调用、然后失败的环境。"""
    bindir = tmp_path / "poison-bin"
    bindir.mkdir(exist_ok=True)
    poison = bindir / "kubectl"
    poison.write_text(
        "#!/usr/bin/env bash\n"
        'printf "kubectl %s\\n" "$*" >> "$POISON_LOG"\n'
        "exit 99\n",
        encoding="utf-8",
    )
    poison.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["POISON_LOG"] = str(tmp_path / "kubectl-was-called.log")
    env.pop("KUBECONFIG", None)
    return env


def _run(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )


def _chown_recorder_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """`_offline_env` + 一个"记录 argv、不改属主"的假 `chown`（非 root 的开发机上用）。"""
    env = _offline_env(tmp_path)
    recorder = tmp_path / "poison-bin" / "chown"
    recorder.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        'with open(os.environ["CHOWN_LOG"], "a", encoding="utf-8") as fh:\n'
        '    fh.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)
    log = tmp_path / "chown-argv.jsonl"
    env["CHOWN_LOG"] = str(log)
    return env, log


def _recorded_chown(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _targets(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("TARGET rel=")]


def _counts(path: Path) -> tuple[int, int]:
    files = dirs = 0
    for _cur, subdirs, names in os.walk(path):
        dirs += len(subdirs)
        files += len(names)
    return files, dirs


def _inventory(root: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """relpath -> (dev, ino, size, mode, mtime_ns)：dry-run 写过一个字节就会不等。"""
    out: dict[str, tuple[int, int, int, int, int]] = {}
    for cur, subdirs, names in os.walk(root):
        subdirs.sort()
        names.sort()
        for name in subdirs + names:
            target = Path(cur) / name
            st = os.lstat(target)
            out[str(target.relative_to(root))] = (
                st.st_dev,
                st.st_ino,
                st.st_size,
                stat.S_IMODE(st.st_mode),
                st.st_mtime_ns,
            )
    return out


@pytest.fixture()
def export_root(tmp_path: Path) -> Path:
    """一个 wave-1 之后的 `<export>`：平台目录是 root 写的，树是池 uid 的。"""
    root = tmp_path / "export"
    (root / "state" / "_runtime" / "sbx_aaa").mkdir(parents=True)
    (root / "state" / "_runtime" / "sbx_aaa" / "sandbox.json").write_text(
        '{"sandbox_id": "sbx_aaa"}\n', encoding="utf-8"
    )
    (root / "state" / ".route-b" / "10000").mkdir(parents=True)
    (root / "state" / ".uid_pool.lock").write_text("", encoding="utf-8")
    (root / SANDBOX_TREES / "_migrate").mkdir(parents=True)
    # worker 的快照 payload 根：树根下的第二条平台目录（`envd_service/agent.py` 硬编码
    # `<workspace_base>/_snapshots`）。它必须**真的在盘上**，否则"放行
    # workspaces/_snapshots"可以在一棵根本没有它的树上恒 MISSING 地通过 —— 那正是这条修复
    # 要防的静默类型。
    (root / SANDBOX_TREES / "_snapshots" / "sbx_aaa").mkdir(parents=True)
    (root / SANDBOX_TREES / "_snapshots" / "sbx_aaa" / "fs").mkdir(parents=True)
    for name in ("_images", "_secrets", "_snapshots", "_templates", "_builds"):
        (root / name).mkdir()
    (root / "_builds" / "ctx.tar").write_bytes(b"build-context")
    (root / SANDBOX_TREES / "sbx_aaa" / "workspace").mkdir(parents=True)
    (root / SANDBOX_TREES / "sbx_aaa" / "workspace" / "hello.txt").write_text(
        "hi\n", encoding="utf-8"
    )
    return root


# --- 静态契约：脚本 -------------------------------------------------------


def test_the_script_defaults_to_dry_run() -> None:
    lines = _lines(SCRIPT)
    # 默认值，也是唯一一处无条件赋值；能翻转它的只有参数循环里的 `--apply`。
    assert [line for line in lines if line.startswith("DRY_RUN=")] == ["DRY_RUN=1"]
    assert "--apply) DRY_RUN=0 ;;" in lines


def test_the_script_only_chowns_and_never_chmods_or_deletes() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    # 迁移改的只有属主：不 chmod（权限位是策略，不是这一步的事）、不删任何东西
    #（没有 unlink/rmdir/rmtree），也不拷贝内容。
    assert [token for token in ("chmod", "os.unlink", "os.rmdir", "os.remove", "shutil") if token in text] == []
    assert "rm -rf" not in text
    engine = "\n".join(_embedded("PY_ENGINE", SCRIPT))
    assert '"chown", "-R", "%d:%d" % (WORKER_UID, WORKER_GID)' in engine


def test_the_script_keeps_the_scene_when_a_step_fails() -> None:
    """半途失败要留下证据：没有 `trap … EXIT` 清理、也没有 rollback 分支。"""
    lines = _lines(SCRIPT)
    assert [line for line in lines if line.startswith("trap ")] == []
    # 唯一的善意清理是成功跑完之后删掉自己建的 Job/ConfigMap（--keep-job 连这个都留着）。
    assert "--keep-job) KEEP_JOB=1 ;;" in lines


def test_the_script_requires_the_worker_to_be_scaled_to_zero() -> None:
    lines = _lines(SCRIPT)
    assert "want_replicas=0" in lines
    # `cmd ||` + 下一行的 `refuse`：与兄弟脚本同一形状 —— `set -e` 不会因为
    # `local x="$(cmd)"` 把失败吞掉，读不到副本数就等于"停写没有被验证过"。
    assert (
        'replicas="$(kubectl -n "$NAMESPACE" get statefulset/e2b-worker '
        "-o jsonpath='{.spec.replicas}')\" ||"
    ) in lines
    # 缩容还不够：正在终止的 pod 还占着卷。闸门也读 pod 列表，并写出怎么修。
    assert 'pods="$(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker -o name)" ||' in lines
    assert (
        'refuse 2 "kubectl get statefulset/e2b-worker 失败 —— 停写没有被验证过，拒绝继续'
        '（通道断了？RBAC？）"'
    ) in lines
    assert "scale statefulset/e2b-worker --replicas=0" in SCRIPT.read_text(encoding="utf-8")


def test_the_engine_heredoc_is_inside_a_function_not_a_command_substitution() -> None:
    """macOS `/bin/bash` 是 3.2，它会把 `x="$(cat <<'PY')"` 解析坏。

    扫 `$( )` 时它会跟着 heredoc 正文里的单引号走 —— 而 python 正文全是单引号。
    开发机的默认 bash 就是那一份，所以引擎正文写在函数的 heredoc 里再管道出去。
    """
    lines = _lines(SCRIPT)
    assert "py_engine() {" in lines
    assert [line for line in lines if "$(cat <<'PY_ENGINE'" in line] == []


def test_no_shell_variable_is_left_adjacent_to_a_non_ascii_character() -> None:
    """`$JOB（` 这种写法在 macOS 自带的 bash 3.2 上会炸：

    `bash: line N: JOB\\xef: unbound variable` —— 3.2 不是多字节感知的，它会把全角字符的
    首字节吞进变量名，`set -u` 下直接中止（实测：`job_run` 的 `warn` 一跑就退出）。所以
    变量紧挨全角标点时必须写 `${JOB}`。兄弟脚本里有同样的四处（不在本次写集内）。
    """
    offenders = []
    for number, line in enumerate(_lines(SCRIPT), start=1):
        for match in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line):
            offenders.append((number, match.group(0)))
    assert offenders == []


def test_the_print_plan_and_offline_paths_never_touch_the_cluster() -> None:
    """`--print-plan` / `--root` 都不带 KUBECONFIG，也必须干干净净退出 0。"""
    lines = _lines(SCRIPT)
    assert "--print-plan) PRINT_PLAN=1 ;;" in lines
    assert "--root) shift; OFFLINE_ROOT=\"${1:-}\" ;;" in lines


# --- 静态契约：Job --------------------------------------------------------


def test_the_job_runs_as_root_on_the_shared_volume_without_retries() -> None:
    job = yaml.safe_load(JOB.read_text(encoding="utf-8"))
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == "state-owner-migrate"
    assert job["metadata"]["namespace"] == "sandlock"
    # 一次性迁移：半途失败的容器**不**自动重试（重试会把刚 chown 的一半再来一遍）。
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    # NFS 只认凭据里的 uid：读 root worker 写下的 0600 文件、把属主让给池/worker uid，
    # 都只有 0 能做。组与 worker 的 65534 对齐，chown 之后属组不出现第三个数字。
    assert pod["securityContext"]["runAsUser"] == 0
    assert pod["securityContext"]["runAsGroup"] == WORKER_GID
    (container,) = pod["containers"]
    assert container["image"] == (
        "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:__IMAGE_VERSION__"
    )
    assert container["command"] == ["bash", "/scripts/migrate-state-owner.sh"]
    # 由操作脚本渲染后 apply。直接 `kubectl apply -f` 原文件时它是个未知参数，
    # 脚本立刻非零退出 —— 不会 chown 任何东西。
    assert container["args"] == ["__ENGINE_FLAGS__"]
    variables = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert variables == {
        "MIGRATE_ROOT": EXPORT_IN_CLUSTER,
        "MIGRATE_WORKER_REPLICAS": "__WORKER_REPLICAS__",
    }
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts[EXPORT_IN_CLUSTER] == {"name": "shared", "mountPath": EXPORT_IN_CLUSTER}
    assert mounts["/scripts"] == {
        "name": "script",
        "mountPath": "/scripts",
        "readOnly": True,
    }
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["shared"]["persistentVolumeClaim"] == {"claimName": "sandbox-shared"}
    assert volumes["script"]["configMap"] == {
        "name": "state-owner-migrate",
        "defaultMode": 0o444,
    }


def test_the_job_is_not_part_of_the_rendered_overlay() -> None:
    """它是一次性、由人手 apply 的对象，绝不是 `apply.sh` 的一员。"""
    text = KUSTOMIZATION.read_text(encoding="utf-8")
    # 只断言与本次相关的那一项：同波次的 Task 4 正在往 resources 里加 `priv-broker.yaml`，
    # 整份列表相等的断言会因为别人的正当改动变红。
    assert "state-owner-migrate" not in text
    resources = yaml.safe_load(text)["resources"]
    assert [entry for entry in resources if "state-owner-migrate" in entry] == []


# --- 行为：路径计划（不连集群） -------------------------------------------


def test_print_plan_lists_exactly_the_eight_platform_targets(tmp_path: Path) -> None:
    proc = _run(["--print-plan"], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    targets = _targets(proc.stdout)
    assert targets == [
        f"TARGET rel={name} owner={WORKER_UID}:{WORKER_GID}" for name in PLATFORM_TARGETS
    ]
    assert len(targets) == 8
    # 树根下恰允许那两条：控制面的迁移暂存与 worker 的快照 payload 根；`workspaces` 本身、它的兄弟、
    # 别的目录都拒绝 —— 这条断言钉的就是"放行集恰是那两条"。
    rels = [line.split("rel=")[1].split(" ")[0] for line in targets]
    assert [
        rel for rel in rels if rel == SANDBOX_TREES or rel.startswith(SANDBOX_TREES + "/")
    ] == list(TREE_ROOT_ALLOWED)
    assert not (tmp_path / "kubectl-was-called.log").exists()


@pytest.mark.parametrize(
    "declared",
    [
        "workspaces",
        "workspaces/sbx_aaa",
        # 树根下放行的那两条**不**给它们开侧门：树根下的别的目录、它们下面的东西、或经
        # `..`/额外层级绕回树里，都还是"落在 workspaces/ 之下"。
        "workspaces/_migrate/sbx_1",
        "workspaces/_migrate/../sbx_1",
        "workspaces/_snapshots/sbx_aaa",
        "workspaces/_snapshots/../sbx_aaa",
        f"{EXPORT_IN_CLUSTER}/workspaces/../workspaces/sbx_aaa",
        f"{EXPORT_IN_CLUSTER}/workspaces/_migrate/../workspaces/sbx_1",
        f"{EXPORT_IN_CLUSTER}/workspaces/_snapshots/../workspaces/sbx_aaa",
    ],
)
def test_a_declared_path_under_the_sandbox_trees_is_refused_by_name(
    declared: str, tmp_path: Path
) -> None:
    proc = _run(["--print-plan", "--target", declared], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    # 点名：拒绝文案里就是调用方写的那条路径。
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(2): 拒绝：{declared} 落在 {SANDBOX_TREES}/ 之下"
        f"（沙箱树属于池 uid，不是 worker 的；树根下只放行 {'、'.join(TREE_ROOT_ALLOWED)} 两条）"
    )
    # fail-closed：拒绝发生在打印计划之前，stdout 里一条 TARGET 都没有。
    assert proc.stdout == ""


def test_the_two_tree_root_platform_dirs_are_the_entries_the_tree_root_allows(
    export_root: Path, tmp_path: Path
) -> None:
    """树根下恰放行那两条 —— 而且它们都是那两条**真的路径**（fixture 里确实存在）。

    `workspaces/_migrate` 是 N27 之后控制面唯一可写的 subPath（`workspace-root-init` 建的就是
    它）；`workspaces/_snapshots` 是 worker 的快照 payload 根（C1 之后 worker 把 payload 写进
    它）。两者都坐在
    树根下，所以迁移工具必须去 chown 它们。`--target` 再声明一遍是幂等的：计划去重，打印出来
    仍是那 8 条；相对与绝对拼写都 rc=0。
    """
    for rel in TREE_ROOT_ALLOWED:
        assert (export_root / rel).is_dir(), rel
    # 相对拼写：就是计划里那一条确切条目，`--target` 再声明一遍是幂等的（去重后仍是那 8 条）。
    for declared in TREE_ROOT_ALLOWED:
        proc = _run(
            ["--root", str(export_root), "--print-plan", "--target", declared],
            env=_offline_env(tmp_path),
        )
        assert proc.returncode == 0, declared + proc.stdout + proc.stderr
        assert _targets(proc.stdout) == [
            f"TARGET rel={name} owner={WORKER_UID}:{WORKER_GID}" for name in PLATFORM_TARGETS
        ], declared
    # 绝对拼写：同一条路径换个写法也必须放行（rc=0，且该 rel 在计划里）。
    for rel in TREE_ROOT_ALLOWED:
        proc = _run(
            ["--root", str(export_root), "--print-plan", "--target", f"{export_root}/{rel}"],
            env=_offline_env(tmp_path),
        )
        assert proc.returncode == 0, rel + proc.stdout + proc.stderr
        rels = [line.split("rel=")[1].split(" ")[0] for line in _targets(proc.stdout)]
        assert set(rels) == set(PLATFORM_TARGETS)
        assert rel in rels
    # 两条放行条目**真的在盘上**、被 stat 到 —— 不是恒 MISSING 的摆设。
    planned = _run(["--root", str(export_root)], env=_offline_env(tmp_path))
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert [line for line in planned.stdout.splitlines() if line.startswith("MISSING rel=")] == []
    stat_rels = [
        line.split("rel=")[1].split(" ")[0]
        for line in planned.stdout.splitlines()
        if line.startswith("STAT rel=")
    ]
    assert [rel for rel in TREE_ROOT_ALLOWED if rel in stat_rels] == list(TREE_ROOT_ALLOWED)


@pytest.mark.parametrize("rel", TREE_ROOT_ALLOWED)
def test_each_tree_root_entry_must_be_its_own_real_directory(
    rel: str, export_root: Path, tmp_path: Path
) -> None:
    """放行一条树根下的平台目录的前提是它就是那个真实目录，不是一条符号链接。"""
    shutil.rmtree(export_root / rel)
    (export_root / rel).symlink_to("sbx_aaa")
    real = os.path.realpath(export_root / rel)
    proc = _run(["--root", str(export_root), "--print-plan"], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(2): 拒绝：{rel} 经符号链接落到 {real}"
        " —— 迁移只碰真实目录，且树根下放行的就是它们自己那两个（停下来人看）"
    )
    assert proc.stdout == ""


@pytest.mark.parametrize("rel", TREE_ROOT_ALLOWED)
def test_a_symlink_alias_of_a_tree_root_entry_is_refused(
    rel: str, export_root: Path, tmp_path: Path
) -> None:
    """换个名字走同一条路也不行：白名单是那**两条**确切条目，不是"解析到它就算"。"""
    alias = "platform-alias"
    (export_root / alias).symlink_to(rel)
    real = os.path.realpath(export_root / alias)
    proc = _run(
        ["--root", str(export_root), "--print-plan", "--target", alias],
        env=_offline_env(tmp_path),
    )
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(2): 拒绝：{alias} 经符号链接落在 {SANDBOX_TREES}/ 之下（{real}）"
        "—— 沙箱树属于池 uid，不是 worker 的"
    )
    assert proc.stdout == ""


def test_a_path_outside_the_export_root_is_refused(tmp_path: Path) -> None:
    proc = _run(["--print-plan", "--target", "/etc/passwd"], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(2): 拒绝：/etc/passwd 不在 export 根 {EXPORT_IN_CLUSTER} 之下"
    )
    assert proc.stdout == ""


def test_a_symlink_into_the_sandbox_trees_is_refused(export_root: Path, tmp_path: Path) -> None:
    """`..` 用词法归一化挡住；符号链接要用 `realpath` 才看得见。"""
    (export_root / "esc").symlink_to(SANDBOX_TREES)
    real = os.path.realpath(export_root / "esc" / "sbx_1")
    proc = _run(
        ["--root", str(export_root), "--print-plan", "--target", "esc/sbx_1"],
        env=_offline_env(tmp_path),
    )
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(2): 拒绝：esc/sbx_1 经符号链接落在 {SANDBOX_TREES}/ 之下（{real}）"
        "—— 沙箱树属于池 uid，不是 worker 的"
    )
    assert proc.stdout == ""


# --- 行为：离线彩排 -------------------------------------------------------


def test_the_offline_dry_run_plans_every_target_and_writes_nothing(
    export_root: Path, tmp_path: Path
) -> None:
    before = _inventory(export_root)
    proc = _run(["--root", str(export_root)], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert [line for line in lines if line.startswith("TARGET rel=")] == [
        f"TARGET rel={name} owner={WORKER_UID}:{WORKER_GID}" for name in PLATFORM_TARGETS
    ]
    # 每个目标目录的 stat 与条目数都打出来（跑完便于和迁移前对比）。
    expected = []
    for name in PLATFORM_TARGETS:
        target = export_root / name
        st = os.lstat(target)
        files, dirs = _counts(target)
        expected.append(
            "STAT rel=%s uid=%d gid=%d mode=0%o files=%d dirs=%d"
            % (name, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), files, dirs)
        )
    assert [line for line in lines if line.startswith("STAT rel=")] == expected
    assert "SUMMARY mode=plan targets=8 chowned=0 missing=0" in lines
    # dry-run 什么也没写：条目、inode、权限位、mtime 逐条不变。
    assert _inventory(export_root) == before
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_an_empty_root_refuses_in_apply_mode(tmp_path: Path) -> None:
    """卷没挂上（空目录）时**不能**以成功收尾。

    挂在空目录上的 PVC 实测就是"8 条全 MISSING、chowned=0、RC=0"——运维会以为迁完了，
    起来 worker 才发现平台态还是 root 的 0600。所以两个闸门都必须在写路径上：形状闸门
    （`state` 与 `state/_runtime` 都不在 ⇒ 这不是本平台的 export 根）先响，`chowned == 0`
    那条兜住其它"计划与实物对不上"的样子。兄弟工具在同样情形用的是 `EXIT_SHAPE`(=3)。
    """
    root = tmp_path / "empty"
    root.mkdir()
    env, chown_log = _chown_recorder_env(tmp_path)
    proc = _run(["--root", str(root), "--apply"], env=env)
    assert proc.returncode == 3
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(3): 没有任何目标被 chown：{root} 下既没有 state 也没有 state/_runtime"
        " —— 这不像是本平台的 export 根（卷没挂上？--root/MIGRATE_ROOT 给错了？），停下来人看"
    )
    # fail-closed：拒绝发生在打印计划与动手之前 —— 一条 TARGET/STAT/AFTER/SUMMARY 都没有，
    # 也没有任何 chown 被调用。
    assert [
        line
        for line in proc.stdout.splitlines()
        if line.startswith(("TARGET ", "STAT ", "AFTER ", "SUMMARY "))
    ] == []
    assert not chown_log.exists()
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_a_root_with_state_but_no_runtime_is_not_a_shape_error(tmp_path: Path) -> None:
    """判据（与引擎 `assert_export_shape` 逐字一致）：只有 `state` 与 `state/_runtime`
    **都不在**才算"这不是本平台的 export 根"。`state` 在、`state/_runtime` 不在 = 这个集群
    还没有任何沙箱记录 —— 那是合法状态，不拦（否则一个刚建好的部署会被自己的迁移工具拒绝）。
    """
    root = tmp_path / "export"
    (root / "state").mkdir(parents=True)
    (root / "state" / ".uid_pool.lock").write_text("", encoding="utf-8")
    proc = _run(["--root", str(root)], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert [line for line in lines if line.startswith("TARGET rel=")] == [
        f"TARGET rel={name} owner={WORKER_UID}:{WORKER_GID}" for name in PLATFORM_TARGETS
    ]
    st = os.lstat(root / "state")
    assert [line for line in lines if line.startswith("STAT rel=")] == [
        "STAT rel=state uid=%d gid=%d mode=0%o files=1 dirs=0"
        % (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
    ]
    assert "SUMMARY mode=plan targets=8 chowned=0 missing=7" in lines


def test_the_engine_asks_chown_to_re_own_the_planned_path(
    export_root: Path, tmp_path: Path
) -> None:
    """`--apply` 真的去调 `chown -R 65534:65534 <计划里的路径>`，第一条是 `state`。

    这台开发机不是 root，chown 不会生效，所以这里用一个"记录 argv、不改属主"的假 chown：
    它同时把引擎的 fail-closed 后置检查（属主没变成 worker 就拒绝）变成断言。8 条路径的
    全集在容器里用"记录 + 转发给真 chown"的包装脚本证明（见 task-6-report.md）。
    """
    env, chown_log = _chown_recorder_env(tmp_path)
    proc = _run(["--root", str(export_root), "--apply"], env=env)
    assert proc.returncode == 4
    state = export_root / "state"
    st = os.lstat(state)
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(4): state chown 之后属主还是 {st.st_uid}:{st.st_gid}"
        "（存储把 chown 当成 no-op？）—— 停下来查"
    )
    calls = _recorded_chown(chown_log)
    assert calls == [["-R", f"{WORKER_UID}:{WORKER_GID}", str(state)]]
    trees = str(export_root / SANDBOX_TREES)
    assert [path for _flag, _pair, path in calls if path == trees or path.startswith(trees + os.sep)] == []


# --- 行为：Job 渲染（不连集群） -------------------------------------------


def _render_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = _offline_env(tmp_path)
    # 版本文件是 gitignore 的（worktree 里没有），所以显式给一个。
    env["VERSION"] = "test-version-6"
    env.update(extra)
    return env


def test_the_job_renders_as_root_on_the_shared_volume_without_retries(tmp_path: Path) -> None:
    proc = _run(
        ["--render-job"], env=_render_env(tmp_path, MIGRATE_WORKER_REPLICAS="0")
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (tmp_path / "kubectl-was-called.log").exists()
    job = yaml.safe_load(proc.stdout)
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"] == {"runAsUser": 0, "runAsGroup": WORKER_GID}
    (container,) = pod["containers"]
    assert container["image"] == (
        "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:test-version-6"
    )
    assert container["command"] == ["bash", "/scripts/migrate-state-owner.sh"]
    assert container["args"] == ["--in-cluster", "--apply"]
    variables = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert variables == {
        "MIGRATE_ROOT": EXPORT_IN_CLUSTER,
        "MIGRATE_WORKER_REPLICAS": "0",
    }
    # 渲染之后不留任何占位符。
    assert [token for token in ("__WORKER_REPLICAS__", "__ENGINE_FLAGS__", "__IMAGE_VERSION__") if token in proc.stdout] == []


def test_render_job_refuses_a_worker_that_is_not_observed_at_zero(tmp_path: Path) -> None:
    proc = _run(["--render-job"], env=_render_env(tmp_path, MIGRATE_WORKER_REPLICAS="2"))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        "REFUSE(2): MIGRATE_WORKER_REPLICAS=2 不是 0：worker 停写没有被验证过"
        "（scale statefulset/e2b-worker --replicas=0 再确认没有 worker pod）。"
        "这份 Job 必须由 deploy/scripts/migrate-state-owner.sh 渲染后 apply，"
        "直接 apply 清单是不会 chown 任何东西的"
    )
    assert proc.stdout == ""


def test_the_in_cluster_gate_refuses_the_unrendered_placeholder(tmp_path: Path) -> None:
    """直接 `kubectl apply -f state-owner-migrate.yaml` 的那条路：不给它任何机会。"""
    env = _offline_env(tmp_path)
    env["MIGRATE_ROOT"] = str(tmp_path)
    env["MIGRATE_WORKER_REPLICAS"] = "__WORKER_REPLICAS__"
    proc = _run(["--in-cluster", "--apply"], env=env)
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        "REFUSE(2): MIGRATE_WORKER_REPLICAS=__WORKER_REPLICAS__ 不是 0：worker 停写没有被验证过"
        "（scale statefulset/e2b-worker --replicas=0 再确认没有 worker pod）。"
        "这份 Job 必须由 deploy/scripts/migrate-state-owner.sh 渲染后 apply，"
        "直接 apply 清单是不会 chown 任何东西的"
    )


def test_an_unknown_argument_is_refused(tmp_path: Path) -> None:
    """`__ENGINE_FLAGS__` 直接 apply 时就是这条：未知参数，立刻非零退出。"""
    proc = _run(["__ENGINE_FLAGS__"], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == "REFUSE(2): 未知参数：__ENGINE_FLAGS__（--help 看用法）"


# --- 行为：操作脚本（stubbed kubectl） -------------------------------------
#
# `check_worker_stopped` 是"worker 真的停写了吗"的唯一判据，所以它必须被**行为地**钉住：
# 静态断言只能证明那两行还在，证明不了它们真的被走到、拒绝了、并且没往下走。

STUB_KUBECTL = '''#!/usr/bin/env python3
"""A kubectl that records its argv and answers the calls the driver makes."""
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
if argv[:2] == ["get", "nodes"]:
    # Cluster-scoped, so deliberately no `-n`: the driver has to be able to
    # check *which* cluster it is talking to before anything else.
    nodes = [
        {"metadata": {"name": "izuf697v12g31dyz4uvsjlz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
        {"metadata": {"name": "izuf6d1usviqv6x9qk1hpcz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
    ]
    print(json.dumps({"items": nodes}))
    raise SystemExit(0)
assert argv[:2] == ["-n", "sandlock"], argv
rest = argv[2:]
if rest[:2] == ["get", "statefulset/e2b-worker"]:
    print(os.environ.get("STUB_REPLICAS", "0"))
elif rest[:2] == ["get", "pods"]:
    print(os.environ.get("STUB_PODS", ""))
elif rest[:1] == ["exec"]:
    assert stdin.startswith("#!/usr/bin/env python3"), stdin[:60]
    print("SUMMARY mode=plan targets=8 chowned=0 missing=0")
elif rest[:2] == ["create", "configmap"]:
    print("apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: state-owner-migrate\\n")
elif rest[:1] == ["apply"]:
    with open(os.environ["STUB_APPLIED"], "a", encoding="utf-8") as fh:
        fh.write(stdin)
elif rest[:3] == ["get", "job", "state-owner-migrate"]:
    print("1" if argv[-1].endswith(".status.succeeded}") else "0")
elif rest[:1] == ["logs"]:
    print("JOB LOG LINE")
elif rest[:1] == ["delete"]:
    pass
else:
    raise SystemExit("unhandled argv: %r" % (argv,))
'''


def _stub_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "kubectl"
    stub.write_text(STUB_KUBECTL, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["KUBECONFIG"] = str(REPO / "tmp" / "k0s" / "kubeconfig")
    env["VERSION"] = "test-version-6"
    env["STUB_LOG"] = str(tmp_path / "kubectl-argv.jsonl")
    env["STUB_APPLIED"] = str(tmp_path / "applied.yaml")
    return env, tmp_path / "kubectl-argv.jsonl", tmp_path / "applied.yaml"


def _recorded(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_the_operator_path_refuses_when_the_worker_is_still_up(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    env["STUB_REPLICAS"] = "2"
    proc = _run(["--apply"], env=env)
    assert proc.returncode == 2
    # 集群身份自检先把节点清单打到 stderr，所以这条按"其中一行"精确匹配。
    assert (
        "REFUSE(2): statefulset/e2b-worker 有 2 个副本（要 want_replicas=0）——先停写："
        "kubectl -n sandlock scale statefulset/e2b-worker --replicas=0 && "
        "kubectl -n sandlock wait --for=delete pod -l app=e2b-worker --timeout=300s"
    ) in proc.stderr.splitlines()
    # 停在闸门上：没有读计划、没有 apply、没有 Job。
    assert not applied.exists()
    assert [argv for argv in _recorded(log) if argv[2:3] == ["apply"]] == []
    assert [argv for argv in _recorded(log) if argv[2:3] == ["exec"]] == []


def test_the_operator_path_refuses_a_lingering_worker_pod(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    env["STUB_REPLICAS"] = "0"
    env["STUB_PODS"] = "pod/e2b-worker-0\npod/e2b-worker-1"
    proc = _run(["--apply"], env=env)
    assert proc.returncode == 2
    assert (
        "REFUSE(2): app=e2b-worker 还有 pod 在跑（停写没完成）："
        "pod/e2b-worker-0 pod/e2b-worker-1"
    ) in proc.stderr.splitlines()
    assert not applied.exists()
    assert [argv for argv in _recorded(log) if argv[2:3] == ["apply"]] == []


def test_the_operator_path_renders_and_runs_the_job(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    proc = _run(["--apply"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "JOB LOG LINE" in proc.stdout.splitlines()

    rendered = applied.read_text(encoding="utf-8")
    assert [
        token
        for token in ("__WORKER_REPLICAS__", "__ENGINE_FLAGS__", "__IMAGE_VERSION__")
        if token in rendered
    ] == []
    job = list(yaml.safe_load_all(rendered))[-1]
    assert job["kind"] == "Job"
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["bash", "/scripts/migrate-state-owner.sh"]
    assert container["args"] == ["--in-cluster", "--apply"]
    assert container["image"] == (
        "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:test-version-6"
    )
    # 观测值（0）真的从 `check_worker_stopped` 一路走到 Job 的 env 里 —— 这条线是纯行为的。
    variables = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert variables == {
        "MIGRATE_ROOT": EXPORT_IN_CLUSTER,
        "MIGRATE_WORKER_REPLICAS": "0",
    }

    recorded = _recorded(log)

    def index_of(*prefix: str) -> int:
        return next(
            index for index, argv in enumerate(recorded) if argv[: len(prefix)] == list(prefix)
        )

    stop_write = index_of("-n", "sandlock", "get", "statefulset/e2b-worker")
    pods = index_of("-n", "sandlock", "get", "pods", "-l", "app=e2b-worker")
    plan = index_of("-n", "sandlock", "exec")
    job_apply = max(index for index, argv in enumerate(recorded) if argv[2:3] == ["apply"])
    logs = index_of("-n", "sandlock", "logs", "job/state-owner-migrate")
    delete = index_of("-n", "sandlock", "delete", "job", "state-owner-migrate")
    # 先认集群 → 观测停写 → 读一遍只读计划 → 才 apply Job → 收日志 → 清理。
    assert index_of("get", "nodes", "-o", "json") < stop_write < pods < plan
    assert plan < job_apply < logs < delete
    assert [
        "-n",
        "sandlock",
        "exec",
        "-i",
        "deploy/control-plane",
        "-c",
        "control-plane",
        "--",
        "python3",
        "-",
        "--root",
        EXPORT_IN_CLUSTER,
        "--mode",
        "plan",
    ] in recorded
    assert [
        "-n",
        "sandlock",
        "create",
        "configmap",
        "state-owner-migrate",
        f"--from-file=migrate-state-owner.sh={SCRIPT}",
        "--dry-run=client",
        "-o",
        "yaml",
    ] in recorded
    assert ["-n", "sandlock", "delete", "configmap", "state-owner-migrate", "--ignore-not-found"] in recorded
