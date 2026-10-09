"""Task 4：控制面迁到自己的 uid 之后，属主迁移的 `--uid` 参数化与 `--rollback`。

Task 3 把控制面从 worker 的 65534 挪到 65533（`deploy/k8s/control-plane.yaml` 的
`runAsUser`/`runAsGroup`），于是共享卷上的平台态有了**两个**身份，属主迁移不能再是一把
`chown 65534:65534`（R12）：

* 控制面写的那些（`_builds`、`_images`（连同 `_oci`）、`_secrets`、`_templates`、
  `_snapshots`、`_migrate`）迁到 `--uid` 给的那一对；
* worker 自己的（共享 `state`：记录、命令日志、`.uid_pool.lock`；树根下那份 `workspaces/`
  拼写 —— 没有 `E2B_SHARED_VOLUME_ROOT` 时的快照 payload 根，以及 **N58 之前**的迁移暂存
  `workspaces/_migrate`）留在 65534 —— 把共享 `state` 交给控制面 uid 会让第一个建箱就写不动；
  控制面**今天**的迁移暂存是 `<export>/_migrate`（它 pod 的 subPath），那一条不在计划表里：
  `workspace-root-init` 会把它交给 65533，要让脚本在同一次跑里做就用 `--target _migrate`
  （回退时要再写一遍）；
* `_snapshots` 是唯一一条**两个身份共写**的：worker 写 `<id>/fs.tar` 与 `.complete`
  （`envd_service/agent.py` 的 `/agent/snapshots`），控制面写 `<id>/snapshot.json`，所以属主
  留在 worker、只把**组位**交给控制面 —— `65534:65533`（R13）。权限位（`2775` + setgid）由
  清单里 root 的 init 容器摆，不由这个脚本摆（它唯一的写操作还是 `chown -R`，没有别的写）。

这份文件钉住三个旋钮：不带 `--uid` 时**逐字**还是 C1 wave 2 的渲染；`--uid 65533` 时只有
控制面的路径动、且 `_snapshots` 那一条只动组位；`--rollback` 走同一张映射表反向（把正向动过
的槽位还给迁移前的 worker 那一对）。脚本的硬性质（dry-run 默认、唯一的写操作是 `chown -R`、
路径白名单、worker 停写观测、失败保留现场……）仍由
`tests/unit/test_state_owner_migrate.py` 逐条钉着，这里不重复。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "migrate-state-owner.sh"

WORKER_UID = 65534

#: 计划里那 8 个平台目标，顺序即脚本的打印顺序（与
#: `tests/unit/test_state_owner_migrate.py::PLATFORM_TARGETS` 逐字相同）。
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

#: 不带 `--uid` 时**逐字**要是 C1 wave 2 的渲染，也就是每一条都落到 worker 那一对。
C1_WAVE_2_OWNER = "owner=65534:65534"

#: `--uid 65533` 时每一条的目标对（R12 的拆分 + R13 的组位）。顺序与 `PLATFORM_TARGETS` 相同。
CONTROL_PLANE_PLAN = (
    "TARGET rel=state owner=65534:65534",
    "TARGET rel=workspaces/_migrate owner=65534:65534",
    "TARGET rel=workspaces/_snapshots owner=65534:65534",
    "TARGET rel=_images owner=65533:65533",
    "TARGET rel=_secrets owner=65533:65533",
    "TARGET rel=_snapshots owner=65534:65533",
    "TARGET rel=_templates owner=65533:65533",
    "TARGET rel=_builds owner=65533:65533",
)


def _offline_env(tmp_path: Path) -> dict[str, str]:
    """一个 `kubectl` 会记录自己被调用、然后失败的环境（同兄弟单测）。"""
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


def _print_plan(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """`--print-plan` 配一个**不在盘上**的根：输出与这台机器上有没有
    `/var/lib/e2b-sandboxes` 无关（只有计划、NOTE 与 MISSING 三段），于是可以逐字比对。"""
    return _run(
        ["--print-plan", "--root", str(tmp_path / "not-on-disk"), *extra],
        env=_offline_env(tmp_path),
    )


def _missing_line(name: str) -> str:
    return f"MISSING rel={name}（不在盘上：跳过 —— 这条对第二次跑就是幂等的）"


def _render_env(tmp_path: Path) -> dict[str, str]:
    env = _offline_env(tmp_path)
    # 版本文件是 gitignore 的（worktree 里没有），worker 副本数必须有人观测过是 0。
    env["VERSION"] = "test-version-4"
    env["MIGRATE_WORKER_REPLICAS"] = "0"
    return env


@pytest.fixture()
def export_root(tmp_path: Path) -> Path:
    """一棵 C1 wave 2 之后的 `<export>`：8 个平台目录都在盘上。"""
    root = tmp_path / "export"
    (root / "state" / "_runtime" / "sbx_aaa").mkdir(parents=True)
    (root / "state" / "_runtime" / "sbx_aaa" / "sandbox.json").write_text(
        '{"sandbox_id": "sbx_aaa"}\n', encoding="utf-8"
    )
    (root / "state" / ".uid_pool.lock").write_text("", encoding="utf-8")
    (root / "workspaces" / "_migrate").mkdir(parents=True)
    (root / "workspaces" / "_snapshots" / "sbx_aaa" / "fs").mkdir(parents=True)
    (root / "workspaces" / "_snapshots" / "sbx_aaa" / "fs" / "hello.txt").write_text(
        "hi\n", encoding="utf-8"
    )
    for name in ("_images", "_secrets", "_snapshots", "_templates", "_builds"):
        (root / name).mkdir()
    (root / "_builds" / "ctx.tar").write_bytes(b"build-context")
    # 池 uid 的沙箱树：迁移绝不许碰它。
    (root / "workspaces" / "sbx_aaa" / "workspace").mkdir(parents=True)
    (root / "workspaces" / "sbx_aaa" / "workspace" / "hello.txt").write_text(
        "hi\n", encoding="utf-8"
    )
    return root


# --- 不带 `--uid`：逐字还是 C1 wave 2 -------------------------------------


def test_without_uid_the_plan_is_byte_for_byte_the_c1_wave_2_rendering(tmp_path: Path) -> None:
    """默认那条路不能因为这次改动动过一个字节。

    期望的 stdout 是从**改动前的脚本**（`4e6101c:deploy/scripts/migrate-state-owner.sh`）
    上取回来的：同一个 `--print-plan --root`（根不在盘上，于是只有计划与 NOTE 两段）。
    """
    root = tmp_path / "not-on-disk"
    proc = _print_plan(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == (
        "\n".join(
            [
                "== state-owner-migration(C1 wave 2) ==",
                f"mode=plan root={root} dry_run=1 uid=65534 gid=65534",
                f"NOTE {root} 不在本机盘上：只打印路径计划（不 stat、不 chown）",
                *(f"TARGET rel={name} owner=65534:65534" for name in PLATFORM_TARGETS),
                *(_missing_line(name) for name in PLATFORM_TARGETS),
                "SUMMARY mode=plan targets=8 chowned=0 missing=8",
            ]
        )
        + "\n"
    )
    assert not (tmp_path / "kubectl-was-called.log").exists()


# --- `--uid 65533`：只有控制面的路径动 ------------------------------------


def test_uid_65533_moves_only_the_control_plane_paths(tmp_path: Path) -> None:
    proc = _print_plan(tmp_path, "--uid", "65533")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _targets(proc.stdout) == list(CONTROL_PLANE_PLAN)
    # 留在 worker 的恰是共享 `state` 与树根下的那两条（快照 payload 根、N58 之前的迁移暂存）
    # —— 共享 `state` 交给 65533 会让 worker 的建箱（记录、命令日志、`.uid_pool.lock`）当场写不动。
    assert [
        line.split("rel=")[1].split(" ")[0]
        for line in _targets(proc.stdout)
        if line.endswith("owner=65534:65534")
    ] == ["state", "workspaces/_migrate", "workspaces/_snapshots"]
    lines = proc.stdout.splitlines()
    root = tmp_path / "not-on-disk"
    assert f"mode=plan root={root} dry_run=1 uid=65533 gid=65533" in lines
    assert "direction=forward control=65533:65533" in lines
    assert "SUMMARY mode=plan targets=8 chowned=0 missing=8" in lines
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_the_uid_flag_spells_the_same_plan_with_an_equals_sign(tmp_path: Path) -> None:
    equals = _print_plan(tmp_path, "--uid=65533")
    assert equals.returncode == 0, equals.stdout + equals.stderr
    assert equals.stdout == _print_plan(tmp_path, "--uid", "65533").stdout
    assert _targets(equals.stdout) == list(CONTROL_PLANE_PLAN)


def test_a_uid_with_leading_zeros_is_canonicalised(tmp_path: Path) -> None:
    """手打的前导零不该漏进 Job 的 args（`10#` 强制十进制，去零后逐字 65533）。"""
    proc = _print_plan(tmp_path, "--uid", "0065533")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _targets(proc.stdout) == list(CONTROL_PLANE_PLAN)
    rendered = _run(["--render-job", "--uid", "0065533"], env=_render_env(tmp_path))
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    assert _rendered_args(rendered.stdout) == ["--in-cluster", "--apply", "--uid", "65533"]


def test_the_snapshot_store_moves_its_group_not_its_owner(tmp_path: Path) -> None:
    """R13：`<export>/_snapshots` 是两个身份共写的，所以属主留 worker、只改组位。"""
    proc = _print_plan(tmp_path, "--uid", "65533")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [
        line for line in _targets(proc.stdout) if line.startswith("TARGET rel=_snapshots ")
    ] == ["TARGET rel=_snapshots owner=65534:65533"]
    # 其余控制面条目的属主与组位都跟着 uid 走 —— 没有第二条只动组位的。
    assert [line for line in _targets(proc.stdout) if line.endswith("owner=65533:65533")] == [
        line for line in CONTROL_PLANE_PLAN if line.endswith("owner=65533:65533")
    ]


def test_an_extra_target_resolves_to_the_control_plane_pair(tmp_path: Path) -> None:
    """`--target` 追加的条目按控制面角色解析 —— 它是调用方点名要迁的那条路径。

    这也是那处拼写不一致的逃生口：控制面**今天**的迁移暂存是 `<export>/_migrate`（N58 之后它
    pod 挂的就是这条 subPath），而计划表里那条 `workspaces/_migrate` 是 N58 之前的拼写、属
    worker。想让本脚本在同一次跑里也把 `<export>/_migrate` 迁掉，就 `--target _migrate`。
    """
    proc = _print_plan(tmp_path, "--uid", "65533", "--target", "_migrate")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _targets(proc.stdout) == [
        *CONTROL_PLANE_PLAN,
        "TARGET rel=_migrate owner=65533:65533",
    ]
    assert "SUMMARY mode=plan targets=9 chowned=0 missing=9" in proc.stdout.splitlines()


def test_an_extra_target_that_duplicates_a_table_entry_keeps_the_tables_role(
    tmp_path: Path,
) -> None:
    """`--target state` 不许把共享 `state` 从 worker 那份账里撬走（表是唯一的真相源）。"""
    proc = _print_plan(tmp_path, "--uid", "65533", "--target", "state", "--target", "workspaces/_migrate")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _targets(proc.stdout) == list(CONTROL_PLANE_PLAN)
    assert "SUMMARY mode=plan targets=8 chowned=0 missing=8" in proc.stdout.splitlines()


def test_the_root_rehearsal_of_the_uid_run_keeps_the_shared_state_on_the_worker(
    export_root: Path, tmp_path: Path
) -> None:
    """`--apply` 的第一条就是 `state`，而 `--uid 65533` **不许**它变成 65533（R12）。

    这台开发机不是 root，chown 不会生效，所以这里用"记录 argv、不改属主"的假 chown：它同时
    把引擎的 fail-closed 后置检查（属主没变成目标就拒绝）变成断言 —— 8 条路径的全集在容器里
    用真的 root 彩排证明（见 task-4-report.md 的回退演练）。
    """
    env, chown_log = _chown_recorder_env(tmp_path)
    proc = _run(["--root", str(export_root), "--uid", "65533", "--apply"], env=env)
    assert proc.returncode == 4
    state = export_root / "state"
    st = os.lstat(state)
    assert proc.stderr.splitlines()[0] == (
        f"REFUSE(4): state chown 之后属主还是 {st.st_uid}:{st.st_gid}"
        "（存储把 chown 当成 no-op？）—— 停下来查"
    )
    assert _recorded_chown(chown_log) == [["-R", "65534:65534", str(state)]]


# --- `--rollback`：同一张表反向 -------------------------------------------


def test_rollback_walks_the_same_table_backwards(tmp_path: Path) -> None:
    back = _print_plan(tmp_path, "--uid", "65533", "--rollback")
    assert back.returncode == 0, back.stdout + back.stderr
    # 正向动过的每一条都还给迁移前的 worker 那一对；worker 自己那两条反向是恒等。
    assert _targets(back.stdout) == [
        f"TARGET rel={name} {C1_WAVE_2_OWNER}" for name in PLATFORM_TARGETS
    ]
    lines = back.stdout.splitlines()
    assert "direction=rollback control=65534:65534" in lines
    assert "SUMMARY mode=plan targets=8 chowned=0 missing=8" in lines


def test_rollback_needs_no_uid_because_the_pre_migration_owner_is_the_worker(tmp_path: Path) -> None:
    """回退的目标对是**迁移前**的属主（worker 那一对），不是 `--uid` 再算一次。"""
    plain = _print_plan(tmp_path, "--rollback")
    with_uid = _print_plan(tmp_path, "--rollback", "--uid", "65533")
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert _targets(plain.stdout) == _targets(with_uid.stdout)
    assert _targets(plain.stdout) == [
        f"TARGET rel={name} {C1_WAVE_2_OWNER}" for name in PLATFORM_TARGETS
    ]


def test_rollback_apply_is_the_reverse_write_on_the_first_entry(
    export_root: Path, tmp_path: Path
) -> None:
    env, chown_log = _chown_recorder_env(tmp_path)
    proc = _run(
        ["--root", str(export_root), "--rollback", "--uid", "65533", "--apply"], env=env
    )
    assert proc.returncode == 4
    assert _recorded_chown(chown_log) == [
        ["-R", "65534:65534", str(export_root / "state")]
    ]


# --- Job 渲染：两个旋钮随 Job 的 args 一起走 ------------------------------


def _rendered_args(stdout: str) -> list[str]:
    job = yaml.safe_load(stdout)
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return container["args"]


def test_render_job_carries_the_uid_and_the_rollback_flags(tmp_path: Path) -> None:
    env = _render_env(tmp_path)
    default = _run(["--render-job"], env=env)
    assert default.returncode == 0, default.stdout + default.stderr
    assert _rendered_args(default.stdout) == ["--in-cluster", "--apply"]
    uid = _run(["--render-job", "--uid", "65533"], env=env)
    assert uid.returncode == 0, uid.stdout + uid.stderr
    assert _rendered_args(uid.stdout) == ["--in-cluster", "--apply", "--uid", "65533"]
    back = _run(["--render-job", "--uid", "65533", "--rollback"], env=env)
    assert back.returncode == 0, back.stdout + back.stderr
    assert _rendered_args(back.stdout) == [
        "--in-cluster",
        "--apply",
        "--uid",
        "65533",
        "--rollback",
    ]


# --- `--uid` 的拒绝路径（在碰任何集群对象之前） ---------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ["--uid"],
            "REFUSE(2): --uid 需要一个十进制 uid：（1..4294967295，例如 65533）",
        ),
        (
            ["--uid", "abc"],
            "REFUSE(2): --uid 需要一个十进制 uid：abc（1..4294967295，例如 65533）",
        ),
        (
            ["--uid", "0"],
            "REFUSE(2): --uid 0 是 root：平台态不交给 root（控制面那一对是 65533，回退是 worker 的 65534）",
        ),
        (
            ["--uid", "00"],
            "REFUSE(2): --uid 0 是 root：平台态不交给 root（控制面那一对是 65533，回退是 worker 的 65534）",
        ),
        (
            ["--uid", "4294967296"],
            "REFUSE(2): --uid 超出 uid 范围（1..4294967295）：4294967296",
        ),
        (
            ["--uid", "00000000000"],
            "REFUSE(2): --uid 超出 uid 范围（1..4294967295）：00000000000",
        ),
    ],
)
def test_a_bad_uid_is_refused_before_any_cluster_object(
    args: list[str], message: str, tmp_path: Path
) -> None:
    proc = _run(["--apply", *args], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == message
    # fail-closed：拒绝发生在读集群之前 —— 一条 stdout 都没有，kubectl 一次没被调用。
    assert proc.stdout == ""
    assert not (tmp_path / "kubectl-was-called.log").exists()


# --- 静态：默认值没有被第二个旋钮顺手改掉 --------------------------------


def test_the_control_plane_pair_defaults_to_the_worker_pair_in_the_engine() -> None:
    """没有 `--uid` 时那一对必须是 worker 自己 —— 引擎里的默认值就是它。"""
    text = SCRIPT.read_text(encoding="utf-8")
    engine = text.split("<<'PY_ENGINE'", 1)[1].split("\nPY_ENGINE", 1)[0]
    engine_lines = [line.strip() for line in engine.splitlines()]
    assert [
        line for line in engine_lines if line.startswith('parser.add_argument("--uid"')
    ] == ['parser.add_argument("--uid", type=int, default=None)']
    assert [
        line for line in engine_lines if line.startswith('parser.add_argument("--rollback"')
    ] == ['parser.add_argument("--rollback", action="store_true")']
    # 唯一的写操作还是那一条 argv（默认那一对逐字保留 C1 wave 2 的拼法）。
    assert [
        line for line in engine_lines if line.startswith('return ["chown", "-R"')
    ] == [
        'return ["chown", "-R", "%d:%d" % (WORKER_UID, WORKER_GID), path]',
        'return ["chown", "-R", "%d:%d" % (uid, gid), path]',
    ]
    assert [token for token in ("chmod", "os.unlink", "os.rmdir", "os.remove", "shutil") if token in text] == []


def test_the_uid_and_the_rollback_flags_are_the_only_two_new_knobs() -> None:
    """它们必须只从显式参数来：默认值是"没给"，不是某个数字。

    `TARGET_UID` 一共只有两处赋值：初值（没给）与 `require_uid_shape` 里的去前导零。
    """
    lines = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()]
    assert '--uid) shift; TARGET_UID="${1:-}"; UID_SET=1; require_uid_shape ;;' in lines
    assert '--uid=*) TARGET_UID="${1#--uid=}"; UID_SET=1; require_uid_shape ;;' in lines
    assert "--rollback) ROLLBACK=1 ;;" in lines
    assert [line for line in lines if line.startswith("TARGET_UID=")] == [
        'TARGET_UID=""',
        'TARGET_UID="$(( 10#$TARGET_UID ))"',
    ]
    assert [line for line in lines if line.startswith("UID_SET=")] == ["UID_SET=0"]
    assert [line for line in lines if line.startswith("ROLLBACK=")] == ["ROLLBACK=0"]


def test_the_two_new_knobs_are_in_the_scripts_own_help() -> None:
    """`usage()` 的 `sed` 区间必须仍是头部注释块的真实范围 —— 新增的用法不能落在它外面。

    超出区间会把真命令打进帮助里，落在区间前则会把新增的用法藏掉。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    last_comment = 1  # 第 1 行是 shebang，注释块从第 2 行起
    for number, line in enumerate(lines, start=1):
        if number < 2:
            continue
        if not line.startswith("#"):
            break
        last_comment = number
    # `usage()` 里那一条 `sed` 必须**恰好**是它，且头部注释块的真实范围就是 2..last_comment。
    assert [line.strip() for line in lines if line.strip().startswith("sed -n '2,")] == [
        f"sed -n '2,{last_comment}p' \"$SCRIPT_PATH\""
    ], f"usage() should read lines 2..{last_comment}"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, cwd=str(REPO)
    )
    assert proc.returncode == 0
    assert proc.stdout == "\n".join(lines[1:last_comment]) + "\n"
    usage = proc.stdout.splitlines()
    assert [
        line for line in usage if line.startswith("#   deploy/scripts/migrate-state-owner.sh --uid")
    ] == [
        "#   deploy/scripts/migrate-state-owner.sh --uid 65533 [--apply] # 控制面迁到它自己的 uid"
    ]
    assert [
        line
        for line in usage
        if line.startswith("#   deploy/scripts/migrate-state-owner.sh --rollback")
    ] == ["#   deploy/scripts/migrate-state-owner.sh --rollback [--uid 65533] [--apply]"]


def test_the_documented_rollout_names_the_step_that_hands_over_the_live_migrate() -> None:
    """上线顺序必须点名"再 apply 清单"这一步 —— 否则读者会以为脚本跑完就算迁完。

    控制面**今天**的迁移暂存是 `<export>/_migrate`（`deploy/k8s/control-plane.yaml` 的
    `subPath: _migrate`），而计划表里那条 `workspaces/_migrate` 是 N58 之前的拼写、属 worker：
    真那条由 `deploy/k8s/c3-agent.yaml` 的 `workspace-root-init`
    （`mkdir -p "$shared/_migrate"` + `chown $CP_UID`，1777 兜底）交出去，所以脚本单独跑完
    Task 1 的探针读到的还是 65534。
    """
    lines = [line.strip() for line in SCRIPT.read_text(encoding="utf-8").splitlines()]
    start = next(index for index, line in enumerate(lines) if line.startswith("# 上线顺序："))
    rollout = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        rollout.append(line)
    assert rollout == [
        "# 上线顺序：worker 缩到 0 → 本脚本 --uid 65533 --apply → **apply 清单**"
        "（`workspace-root-init`",
        "#   把 <export>/_migrate 交给 65533，1777 兜底）→ 起 worker → 用 Task 1 的探针验证"
        "（它读的就是那条）",
        "#   （想让脚本在同一次跑里也迁它：`--uid 65533 --target _migrate --apply`；回退写同一条）",
        "# 回退顺序：worker 缩到 0 → 本脚本 --rollback --apply（正向写过 --target 的这里要写同一条）",
        "#   → 回退清单（否则下一次 agent rollout 时 init 容器会按新身份再交一遍）→ 起 worker → 验证",
    ]
