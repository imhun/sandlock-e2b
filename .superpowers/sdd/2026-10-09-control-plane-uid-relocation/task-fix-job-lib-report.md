# 修 release blocker：集群内 Job 加载不到 `lib/cluster-guard.sh`

**Status: DONE**（不含生产上线；本轮**没有**碰 k0s —— 无隧道、无 kubectl、无 apply）

基准 `main` = `ab98313`。改动落在 6 个文件：两个迁移脚本、两个文档片段、一条既有钉子、
一个新增的钉子文件。所有测试命令在 macOS 开发机上跑（`tmp/venv/bin/python -m pytest`）。

---

## 1. 复现（生产现场 + 本机）

`deploy/scripts/migrate-state-owner.sh --uid 65533 --apply` 渲染出的 ConfigMap
`state-owner-migrate` 只装 `migrate-state-owner.sh` 一个文件（`kubectl create configmap
--from-file=migrate-state-owner.sh=$SCRIPT_PATH`），而脚本顶层无条件
`. "$SCRIPT_DIR/lib/cluster-guard.sh"`（N71 闸门 `1d0852b`，2026-10-03）⇒ 容器里
`/scripts/lib/cluster-guard.sh` 不存在，`set -e` 退出 1，**一个 `chown` 都没做**。
现场证据在 `task-5-prod-report.md` §1.4（`tmp/cpuid-prod/rollout.log`）。

本机把它按同形状复现（只复制那一个文件、不放 `lib/`）：原文落
`tmp/cpuid-fixjoblib/red-in-cluster-load.txt`，逐字：

```
tmp/cpuid-fixjoblib/head-migrate-state-owner.sh/migrate-state-owner.sh: line 563: /Users/polus/project/ai/sandlock-e2b/tmp/cpuid-fixjoblib/head-migrate-state-owner.sh/lib/cluster-guard.sh: No such file or directory
EXIT=1

tmp/cpuid-fixjoblib/head-migrate-state-base.sh/migrate-state-base.sh: line 1015: /Users/polus/project/ai/sandlock-e2b/tmp/cpuid-fixjoblib/head-migrate-state-base.sh/lib/cluster-guard.sh: No such file or directory
EXIT=1
```

⇒ **`migrate-state-base.sh` 同形**（顶层 source 在 `:1015`，同样 `--from-file=` 只装脚本本身），
按第 2 条要求一并修。这也是潜伏回归：C1 的属主迁移跑在 `1d0852b` 之前，闸门加上后这条
Job 路径再没被真跑过 —— Task 4 的演练走的是离线 `--root`，从不 source 闸门。

## 2. 修法：闸门只在**本地（运维）路径**加载，集群内路径不引任何 ConfigMap 没装的文件

闸门是**本地**的顾虑：Job 里没有 `KUBECONFIG`（service account + in-cluster API），
`require_target_cluster` 的判据（`KUBECONFIG` 显式设置 + `kubectl version -o json` 的形状）
在那条路上本来就不适用；集群内"worker 真的停写了吗"由 Job 的 `MIGRATE_WORKER_REPLICAS=0`
闸门与脚本自己的 `--in-cluster` 检查承担。所以把 `source` 收进 `IN_CLUSTER != 1` 条件里。

### 2.1 `deploy/scripts/migrate-state-owner.sh`（与 `migrate-state-base.sh` 同形）

旧（`owner` `:563` / `base` `:1015`，**逐字一样**，只差前后那段注释）：

```bash
. "$SCRIPT_DIR/lib/cluster-guard.sh"
```

新：

```bash
if [ "$IN_CLUSTER" != "1" ]; then
    . "$SCRIPT_DIR/lib/cluster-guard.sh"
fi
```

（上面带一段中文注释说明"为什么"：`owner` 在 `:566-577`、`base` 在 `:1018-1029`。）

### 2.2 运维路径与今天**逐字相同**

`IN_CLUSTER` 默认 `0`（只有 `--in-cluster` 置 1），所以运维路径（`--print-plan` /
`--render-job` / `--root` 彩排 / `--apply`）仍然加载闸门，`check_kubeconfig` /
`check_cluster_identity` / `check_worker_stopped` 与旧代码完全一致 —— 没有加宽任何条件、
没有把闸门换成"可选"。实测（无 `KUBECONFIG`）：

```
$ env -u KUBECONFIG bash deploy/scripts/migrate-state-owner.sh --apply
REFUSE(2): KUBECONFIG 没设：export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"（本机默认 context 指的是另一套 ACK 集群，见 docs/deploy-clusters.md §1）
exit=2
$ env -u KUBECONFIG bash deploy/scripts/migrate-state-base.sh --apply
（同一条）
```

闸门常量仍由 lib 提供（`nodes=2 arch=arm64 kubelet=+k0s`）；既有钉子
`test_cluster_guard.py` / `test_secret_scripts_require_cluster_guard.py` 与两个脚本自己的
运维路径钉子全绿。

### 2.3 集群内路径现在从"ConfigMap 真装了什么"加载

只复制那一个文件（没有 `lib/`）、按 Job 的方式 `--in-cluster`（默认 dry-run）：两个脚本都
进到引擎并给出计划，`EXIT=0`。原文 `tmp/cpuid-fixjoblib/green-in-cluster-load.txt`：

```
$ bash <shipped>/migrate-state-owner.sh --in-cluster   # MIGRATE_ROOT=<dir with state/>
SUMMARY mode=plan targets=8 chowned=0 missing=7
EXIT=0
$ bash <shipped>/migrate-state-base.sh --in-cluster    # MIGRATE_ROOT=<dir with _runtime/>
SUMMARY mode=plan moves=1 dirs=3 todo=4 done=0 unknown=0
EXIT=0
```

## 3. 那条缺失的测试（RED → GREEN）

新钉子 `tests/unit/test_migrate_scripts_in_cluster_loadable.py`，8 个 case（两脚本 × 4 条），
钉的是**这一类**而不是"字符串在不在"：

1. `test_the_job_loads_exactly_the_file_the_configmap_ships` —— 从脚本自己的
   `--from-file=<key>=$SCRIPT_PATH` 取出 ConfigMap 的 key/源，与 Job 清单里的
   `command: ["bash", "/scripts/<key>"]` + `configMap.name` 逐字对齐；`/scripts/<key>`
   恰好是那个文件，不多不少。
2. `test_the_configmap_text_parses_under_the_shell_the_job_uses` —— 把那个文件单独复制出去
   （ConfigMap 的原文），`bash -n`（Job 的 `command` 用的就是 `bash`）必须无输出、退出 0。
3. `test_the_in_cluster_path_loads_with_only_the_shipped_file` —— **真的把渲染出来的那条
   形状跑一遍**：`SCRIPT_DIR` 旁边没有 `lib/`，`--in-cluster` 必须进引擎、exit 0、stderr 为空。
4. `test_the_gate_is_loaded_only_on_the_local_path` —— 静态兜底：那条 `source` 恰好夹在
   `if [ "$IN_CLUSTER" != "1" ]; then` / `fi` 之间。

**RED**（在 `HEAD=ab98313` 的 worktree 里跑同一个钉子文件，只补这一个文件）：
`tmp/cpuid-fixjoblib/red-nail.txt` = **4 failed, 4 passed**，失败逐字：

```
FAILED tests/unit/test_migrate_scripts_in_cluster_loadable.py::test_the_in_cluster_path_loads_with_only_the_shipped_file[migrate-state-owner.sh]
FAILED tests/unit/test_migrate_scripts_in_cluster_loadable.py::test_the_in_cluster_path_loads_with_only_the_shipped_file[migrate-state-base.sh]
FAILED tests/unit/test_migrate_scripts_in_cluster_loadable.py::test_the_gate_is_loaded_only_on_the_local_path[migrate-state-owner.sh]
FAILED tests/unit/test_migrate_scripts_in_cluster_loadable.py::test_the_gate_is_loaded_only_on_the_local_path[migrate-state-base.sh]
```

失败正文里出现生产现场那一行的本机版本（`.../scripts/lib/cluster-guard.sh: No such file or
directory`、`assert 1 == 0`），原文见 `tmp/cpuid-fixjoblib/red-nail.txt`。

**GREEN**（当前工作树）：`tests/unit/test_migrate_scripts_in_cluster_loadable.py` **8 passed**；
整档 focused（两迁移脚本 + uid 那两条 + 闸门那两条 + base 脚本）= **121 passed**（原文
`tmp/cpuid-fixjoblib/green-focused.txt`）。

## 4. 既有硬性质没有被动到

* dry-run 默认、只有 `chown -R` 写、没有 `chmod`/删除/`shutil`/`rm -rf`、没有 `trap`、
  失败保留现场、worker 必须在 0 副本、白名单拒绝沙箱树、引擎 heredoc 仍在函数里、
  不带 `--uid` 的渲染逐字等于今天 —— 这些钉子（`test_state_owner_migrate.py` /
  `test_migrate_state_owner_uid.py` / `test_migrate_state_base_script.py`）全绿，没改断言。
* 唯一的钉子文本改动是新增了两条"先关自动扩缩器"的口径（见下）。`usage()` 的 `sed` 区间随
  头部注释块的真实长度一起**有意**改了两处：
  * `migrate-state-owner.sh`：`sed -n '2,79p'` → `sed -n '2,82p'`（头部 +3 行）；
  * `migrate-state-base.sh`：`sed -n '2,55p'` → `sed -n '2,58p'`（头部 +3 行）。
  两条钉子是"`sed` 区间 == 头部注释块真实范围"（owner 那条还额外逐字比对 `--help` 全文），
  改的是**值**而不是把钉子放松。

## 5. Docs 口径（同一类，2026-10-09 上线尝试撞到的）

`E2B_AS_MIN_REPLICAS=2` 的 autoscaler（N58 起在控制面里）会在 ~1 秒内把刚缩到 0 的 worker
拉回来 —— §7.29 写了，但 operator 会照做的"缩 worker"片段没写。改了三处（都是文档/注释）：

1. `docs/k8s-deployment.md` §**24.1 执行顺序**（这是仓库里**唯一**的 §24.1；简报里写的
   `docs/deploy-clusters.md` §24.1 实际不存在 —— §24.x 在 `docs/k8s-deployment.md`，
   `deploy-clusters.md` 只有 §7.29 那条正确口径）：第 1 步改成"先关自动扩缩器 →
   `kubectl -n sandlock set env deploy/control-plane E2B_AS_ENABLED=false` + `rollout status`
   → 再缩 worker"；第 4 步补"先把 `E2B_AS_ENABLED=true` 写回去"。
2. `deploy/k8s-k0s/README.md`「平台态属主迁移」的同一段调用片段：同样的两步。
3. 两个脚本 `--help` 的 rollout 行：`上线顺序：` 冠上"先关自动扩缩器"（带命令、`§7.29`
   指向与"~1 秒拉回来"的事实），`回退顺序：` 同上；`migrate-state-base.sh` 的
   `上线顺序` 也补了同一段。

只动文档口径，不改任何清单/行为。

## 6. 测试命令与读数

```bash
# focused（RED 见 §3；这里是 GREEN）
tmp/venv/bin/python -m pytest tests/unit/test_migrate_scripts_in_cluster_loadable.py \
  tests/unit/test_migrate_state_owner_uid.py tests/unit/test_state_owner_migrate.py \
  tests/unit/test_migrate_state_base_script.py tests/unit/test_cluster_guard.py \
  tests/unit/test_secret_scripts_require_cluster_guard.py -q
# -> 121 passed in 31.38s

# 整档
tmp/venv/bin/python -m pytest tests/unit -q
# -> 4 failed, 2805 passed, 12 skipped in 232.75s
```

整档那 4 条正是简报点名的既有项，**没有新增**：
`test_own_identity_naming.py::test_the_live_tree_stopped_spelling_the_letters`（既有、与本改无关）、
`test_real_root_gate.py::test_the_probe_asks_for_the_pivot_root_this_architecture_has`、
`test_xfs_quotactl_backend.py::test_probe_reports_unsupported_on_einval`、
`test_xfs_quotactl_backend.py::test_probe_reports_unknown_on_other_errors`（后三条 macOS 平台项）。

另跑：两个脚本 `bash -n` 均 OK；`--help` 输出仍逐字等于头部注释块（钉子过的）。

## 7. 改了哪些文件

| 文件 | 改了什么 |
|---|---|
| `deploy/scripts/migrate-state-owner.sh` | 集群内不 source 闸门（`if IN_CLUSTER != 1`）+ 为什么的注释；rollout 行补"先关自动扩缩器"；`usage()` `sed` `2,79`→`2,82` |
| `deploy/scripts/migrate-state-base.sh` | 同上（`source` 在 `:1015`）；rollout 行补同一段；`usage()` `sed` `2,55`→`2,58` |
| `tests/unit/test_migrate_scripts_in_cluster_loadable.py` | **新增**：这类缺失的钉子（8 case） |
| `tests/unit/test_migrate_state_owner_uid.py` | rollout 钉子的期望块随脚本 +3 行（先关扩缩器） |
| `docs/k8s-deployment.md` | §24.1 执行顺序：先关扩缩器、起回时写回 `true` |
| `deploy/k8s-k0s/README.md` | 同一段 k0s 调用片段：同样两步 |
| `.superpowers/sdd/.../task-fix-job-lib-report.md` | 本报告 |

## 8. 关注点 / 留给下一步

1. **没有上生产**（按交办）。修好之后的重跑顺序不变：先本地车道、再按 `task-5-prod-report.md`
   §5 的 1–6 步走；卷仍是 `65534:65534`、集群仍是改动前的形状，第 2 步可以从干净状态重跑。
   现场里保留的 `job/state-owner-migrate`（Failed）与 `configmap/state-owner-migrate` 是
   上一次的证据，清理命令在 `task-5-prod-report.md` §4。
2. **手册的文件名对不上**：简报写 `docs/deploy-clusters.md` §24.1，但 §24.1 只存在于
   `docs/k8s-deployment.md`（`grep -rn '24\.1' docs deploy` 只有这一处）；`deploy-clusters.md`
   没有 §24.1，它的 §7.29 是那条正确口径。我改的是真的那一处 §24.1，并把同一段调用片段
   （`deploy/k8s-k0s/README.md`）一并收口 —— 与 `task-5-prod-report.md` §7.3 的
   "两处调用片段"一致。
3. `deploy/scripts/migrate-state-base.sh` 的 `--in-cluster` 只在 `--apply` 时写；本轮没有
   在生产上跑它，它的 Job 路径同样只被新增的钉子覆盖（同样的"只装一个文件"形状）。
4. 两个文档片段里第 4 步写回 `E2B_AS_ENABLED=true` 用的是 `set env`（不是
   `rollout restart`）；`apply.sh` 会把清单里的 `"true"` 再写回去，两条路等价。
