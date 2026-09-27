"""C1 wave 2 Task 6: 平台态属主的一次性迁移被它的契约钉住。

wave 1/2 之后 worker 是 uid 65534，而今天这台 NAS 上的平台态文件（`state/**`、
`_runtime/**`、`.uid_pool.lock`、`_images/**`、`_secrets/**` …）还是 root worker 写下的
0600/0700。这一步是硬前置，所以它被钉两遍：

* **静态**：钉脚本与 Job 的文本 —— dry-run 是默认、唯一的写操作是 `chown`（没有 chmod、
  没有删除、没有 `trap` 清理现场）、worker 不在 0 副本就拒绝、Job 以 root 跑在同一条
  PVC 上且不自动重试、三处占位符让"直接 apply 原文件"什么也做不了；
* **行为**：在没有集群的情况下跑 `--print-plan` 与 `--root` 彩排 —— 计划恰是 brief 的
  那 7 个平台目录；路径计划里任何落在 `workspaces/` 之下的拼写（含 `..` 与符号链接）都
  被点名拒绝。毒化过的 `kubectl`（记录自己被调用过、然后失败）把"这些路径不连集群"
  变成断言而不是承诺。

脚本行按 `strip()` 后精确比对（同 `test_migrate_state_base_script.py`）：重写过的行要在
这里重新对一遍，而不是靠旧行的子串蒙过去。
"""

from __future__ import annotations

import os
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

#: 计划里那 7 个平台目录，**顺序即脚本的打印顺序**（Task 6 brief 的顺序）。
PLATFORM_TARGETS = (
    "state",
    "_migrate",
    "_images",
    "_secrets",
    "_snapshots",
    "_templates",
    "_builds",
)

#: 沙箱树。池 uid 的树，不是 worker 的 —— 这个前缀下的任何路径都必须被拒。
SANDBOX_TREES = "workspaces"


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
    for name in ("_migrate", "_images", "_secrets", "_snapshots", "_templates", "_builds"):
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
    assert (
        'replicas="$(kubectl -n "$NAMESPACE" get statefulset/e2b-worker '
        "-o jsonpath='{.spec.replicas}')\""
    ) in lines
    # 缩容还不够：正在终止的 pod 还占着卷。闸门也读 pod 列表，并写出怎么修。
    assert 'pods="$(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker -o name)"' in lines
    assert "scale statefulset/e2b-worker --replicas=0" in SCRIPT.read_text(encoding="utf-8")


def test_the_engine_heredoc_is_inside_a_function_not_a_command_substitution() -> None:
    """macOS `/bin/bash` 是 3.2，它会把 `x="$(cat <<'PY')"` 解析坏。

    扫 `$( )` 时它会跟着 heredoc 正文里的单引号走 —— 而 python 正文全是单引号。
    开发机的默认 bash 就是那一份，所以引擎正文写在函数的 heredoc 里再管道出去。
    """
    lines = _lines(SCRIPT)
    assert "py_engine() {" in lines
    assert [line for line in lines if "$(cat <<'PY_ENGINE'" in line] == []


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
    assert "state-owner-migrate.yaml" not in text
    kustomization = yaml.safe_load(text)
    assert kustomization["resources"] == [
        "../k8s",
        "storage-nas.yaml",
        "gateway-nodeport.yaml",
    ]


# --- 行为：路径计划（不连集群） -------------------------------------------


def test_print_plan_lists_exactly_the_seven_platform_directories(tmp_path: Path) -> None:
    proc = _run(["--print-plan"], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    targets = _targets(proc.stdout)
    assert targets == [
        f"TARGET rel={name} owner={WORKER_UID}:{WORKER_GID}" for name in PLATFORM_TARGETS
    ]
    # 计划里的每一条都不是沙箱树（`workspaces` 本身也不行）。
    rels = [line.split("rel=")[1].split(" ")[0] for line in targets]
    assert [
        rel for rel in rels if rel == SANDBOX_TREES or rel.startswith(SANDBOX_TREES + "/")
    ] == []
    assert not (tmp_path / "kubectl-was-called.log").exists()


@pytest.mark.parametrize(
    "declared",
    [
        "workspaces",
        "workspaces/sbx_1",
        f"{EXPORT_IN_CLUSTER}/workspaces/../workspaces/sbx_1",
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
        "（沙箱树属于池 uid，不是 worker 的）"
    )
    # fail-closed：拒绝发生在打印计划之前，stdout 里一条 TARGET 都没有。
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
    assert "SUMMARY mode=plan targets=7 chowned=0 missing=0" in lines
    # dry-run 什么也没写：条目、inode、权限位、mtime 逐条不变。
    assert _inventory(export_root) == before
    assert not (tmp_path / "kubectl-was-called.log").exists()


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
