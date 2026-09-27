# Task A7 报告：无 SYS_ADMIN 门禁固化 + backlog #25 收口

**状态：DONE_WITH_CONCERNS**（两项偏离 brief 原文，均已在下面「疑虑」逐条说明并留有证据）

| 项 | 值 |
|---|---|
| 主仓库提交 | `1d0bbbe test(deploy): pin the no-SYS_ADMIN worker shape and narrow the XFS deselects (A7)` |
| 分支/工作区 | `main`，`/Users/polus/project/ai/sandlock-e2b`（`git status`：仅既有 `?? target`） |
| fork 子模块 | 只读参考，**未改动**；指针仍 `71e9deb`（`upstream-pr/netns-free-clean`，未推送） |
| 测试镜像 | `e2b-sandlock-test:latest` = `sha256:2b796e1c11222c0e845f2d498ea9d4be0632babd4249b212ad209768bd11f42c`（A4 重建那枚，本次未重建） |
| 产品代码 | **零改动**（本 Task 只碰 1 个 deploy 脚本 + 3 个文档 + 计划勾选） |
| 远程推送 | 无（本地提交） |

---

## 1. `PROD_DROP_CAPS=SYS_ADMIN` 的实际计数与对照

**GREEN（本次）**：`PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh`

```
1075 passed, 3 skipped, 0 failed in 301.19s (0:05:01)
```

日志：`tmp/a7-nosa.log`（首行 `ENV-HEADER commit=f2af31e …` + `SCRIPT-SYNTAX … -> OK`
与两行 cap 探针）。**Fix round 1 更正**：这一版日志尾部没有 exit code 行，原文那句
`WRAPPER-EXIT=0` 不可复核（评审 Minor-4）。现在 `tmp/a7-run.sh` 把 `EXIT=<code>` 写进日志尾部，
当前 `tmp/a7-nosa.log`（fix round 1 在同树重跑）末行就是 `EXIT=0`，见 §F1。
注：ENV-HEADER 里的 `commit=`/`main=` 是**运行时**的 HEAD（`f2af31e`，A6 提交；A7 提交当时
还没生成）。门禁跑的就是提交里那份脚本：`deploy/scripts/test-prod-shaped.sh`
`sha256=f9e59e709eb94954981146d8b09afd6f41d0f333e1133d0f4b643f5e2ee19ac7`，
与 `git show 1d0bbbe:deploy/scripts/test-prod-shaped.sh` 逐字节相同 ⇒ 证据对当前提交有效。

**Fix round 1 在同一 lane 上重跑（当前树，含 A6 fix-1/fix-2 的新用例）**：
`1094 passed, 3 skipped, 0 failed`（361.69s），日志 `tmp/a7-nosa.log` 末行 `EXIT=0`
（首轮 fix-round 全量红过一条多节点 flake，留档 `tmp/a7-nosa-flake-multinode.log`，见 §F1.4）。
注意这条 lane 是 **chroot 形态**（`E2B_BASE_IMAGE=python-mcp:3.14`），所以 §F1.3 的形状红
（pure / macOS）不在这条 lane 上出现。

**RED（对照，改造前的同一 lane）**：`tmp/nosa-full.log` = `4 failed, 962 passed, 3 skipped`，
4 条 failed 逐条为：

- `tests/contract/test_migration.py::test_migrate_with_shared_volume`
- `tests/contract/test_uid_permissions.py::test_volume_shared_rw_across_distinct_uids`
- `tests/sdk/python/test_shared_volumes.py::test_shared_volume_mount_on_remote_node`
- `tests/sdk/python/test_shared_volumes.py::test_shared_volume_sandbox_cannot_reach_other_volumes`

⇒ 三条 skip、零 error；这 4 条正是 A4/A5 修掉的共享卷用例，**无红需要归因**（没有红）。

**cap 探针（证明门禁真的没有 SYS_ADMIN）**——探针 argv 由脚本自身导出（`tmp/a7-bin/docker`
是只回显 argv 的桩），所以不会与脚本漂移：

| 形态 | `CapEff` | SYS_ADMIN 位 `0x200000` |
|---|---|---|
| 默认 lane（不削 cap） | `00000000a02c35fb` | 在 |
| `PROD_DROP_CAPS=SYS_ADMIN` | `00000000a00c35fb` | 已清（差值正好 `0x200000`） |

**默认 lane 无漂移（本 Task 唯一触及的那条 lane，phase 1 + phase 2）**：
`./deploy/scripts/test-prod-shaped.sh`（不设 `PROD_DROP_CAPS`，`UNPRIVILEGED_PHASE=1`）

```
phase 1: 1075 passed, 3 skipped, 0 failed in 309.73s
phase 2: 48 passed, 1 skipped in 31.30s
```

日志 `tmp/a7-default-lane.log`。对照 A6 的 `tmp/a6-full-gate.log`（phase 1 `979 passed,
3 skipped`、phase 2 `48 passed, 1 skipped`）：**phase 2 逐字不变**，phase 1 只多出解禁的
96 条（见 §3）。

---

## 2. 改动清单

| 文件 | 改动 |
|---|---|
| `deploy/scripts/test-prod-shaped.sh` | ① 新增 `PROD_DROP_CAPS`（逗号分隔，可带空格）；② `XFS_DESELECTS` 7 → 2；③ 更正 strict-skips 注释口径；④ 头注释/Usage 补 `PROD_DROP_CAPS` 用法 |
| `docs/task-backlog.md` | #25 标 ✅：#25 标题与结论改成终态（无 `SYS_ADMIN` 可用 + 三处改动 A4/A5/A6 + 4 个证据日志名 + 门禁入口 + cap 探针）；保留「线上部署前置」为 ⬜（非代码缺口） |
| `docs/production-deployment-requirements.md` | §2.4.1 标题口径（A6 迁出 + A7 固化）与 `SYS_ADMIN` 行改成**只列 agent 侧用途**；§2.5 门禁形态段更正（deselect 2 个、`PROD_DROP_CAPS` 用法、`--cap-add` 压过 `--cap-drop` 的实测、strict-skips 口径） |
| `docs/HANDOFF.md` | 新增顶部 `## ⚡ 共享卷去 SYS_ADMIN（2026-09-11，A4–A7 收口 / backlog #25）`（探针 / RED-GREEN / 三处改动与 commit / wheel 指纹 / 门禁数字 / 遗留）；旧 F18 段那句「7 个配额文件」补一条 A7 更正；删掉旧块里已不成立的「backlog #25 未解决缺口」指向（由新块统一收口） |
| `docs/superpowers/plans/2026-09-10-shared-volume-cwd-and-backlog-closeout.md` | A0–A7 的 **38 个 `- [ ]` 全部置 `[x]`**（Track B 未动）；A7 Step 1 下补「`--cap-drop` 单独用是空操作」的控制器更正；Step 2 / Step 3 补实测数字 |

未纳入提交：`tmp/**`（gitignored 证据）、`.superpowers/**`（gitignored）、`target/`（既有未跟踪）。

---

## 3. `XFS_DESELECTS` 收窄前后差异

**前（7 条）**：

```
tests/contract/test_volume_quota.py
tests/contract/test_xfs_project_quota.py
tests/unit/test_volume_quota.py
tests/unit/test_xfs_project_quota_agent.py
tests/unit/test_quota_agent_client.py
tests/unit/test_quota_maintenance.py
tests/security/test_quota_enforcement.py   <- 该文件已不存在（stale）
```

**后（2 条，判据 = 真去建/报告 XFS prjquota 暂存盘）**：

```
tests/contract/test_volume_quota.py        # fixture 真 mount prjquota；无 XFS 时 skip 命中
                                           #   conftest 的 "XFS quota integration requires"
                                           #   ⇒ strict 下会变 error，必须留
tests/contract/test_xfs_project_quota.py   # 同上（真 xfs_quota report/limit 断言）
```

**移出 5 条的核对过程（都是「真跑一遍」而不是读代码猜）**：

- 4 个配额单测靠 `monkeypatch` 换掉 `xfs_project_supported` 与 `subprocess.run`，
  不碰真文件系统 ⇒ 在**无 SYS_ADMIN、无 XFS** 的 lane 下确实可跑。
- 第一次用 `tmp/lane.sh`（root、无 SYS_ADMIN、**无 SYS_PTRACE**）跑这 4 个文件：
  `91 passed, 5 failed`，5 条红全是
  `E2B_PER_SANDBOX_UID is enabled on a root worker without CAP_SYS_PTRACE …` 这条无关 WARNING
  落进 quota 用例的「精确日志列表」断言（不是 XFS 依赖，也不是产品缺陷）。
  ⇒ 定性与 A6 同源；在带 `SYS_PTRACE` 的 `test-prod-shaped.sh` 形态下 5 条全绿
  （最终全量 `1075 passed` 里已含）。
- `tests/security/test_quota_enforcement.py` 直接不存在（`rg --files` 找不到），
  留着只会让读者以为有覆盖 ⇒ 删除该条目。

**效果**：默认门禁多跑 **96** 条用例（`pytest … --collect-only` 实测这 4 个文件 = 96）；
对照 A6 的 `tmp/a6-full-gate.log`（`979 passed, 3 skipped`，收集 982）→ 现在（`1075 passed,
3 skipped`，收集 1078）。相对 A4 之前的基线 `tmp/nosa-full.log`（收集 969）共 +109，
其中 13 条是 A4–A6 自己新增、96 条是这次解禁。**A5/A6 的新用例（`via_agent` 形态、
agent URL 开关、project 管理）因此回到默认门禁。**

---

## 4. 台账收口改了哪些文件

- `docs/task-backlog.md`：#25 由「✅ 最小集实测 + ⬜ 退化缺口未解决 + ⬜ 线上前置」
  改为「✅ 已收口（无 `SYS_ADMIN` 可用 + 三处改动 A4/A5/A6 + 证据日志名 + 门禁入口）
  + ⬜ 线上部署前置（保留，属审计结论）」。
- `docs/production-deployment-requirements.md`：§2.4.1 的 `SYS_ADMIN` 行**终态化** ——
  只留 agent 侧用途（`profiles: ["quota"]` 的 quota-agent 执行 `xfs_quota -x`），
  worker 侧三处写成历史并标注 A4/A5/A6；另补 A7 的整份套件数字与 cap 探针。
  §2.5 更新 deselect 数量与 strict-skips 口径。
- `docs/HANDOFF.md`：新增 `## ⚡ 共享卷去 SYS_ADMIN（2026-09-11，A4–A7 收口 / backlog #25）`
  块（探针 → 踩坑 → 三处改动与 commit → wheel 指纹 → 门禁数字 → 遗留），
  并在旧 F18 段标注 A7 更正。
- `docs/superpowers/plans/2026-09-10-shared-volume-cwd-and-backlog-closeout.md`：
  A0–A7 步骤全部 `[x]` + A7 的实测数字与一条控制器更正。

---

## 5. 疑虑 / 与 brief 的偏离（逐条有证据）

1. **brief Step 1 给的 `--cap-drop` 片段单独用是空操作（已改为过滤 `--cap-add` 列表）。**
   本机 Docker 引擎 29.4.0 实测：`--cap-drop ALL --cap-add SYS_ADMIN --cap-drop SYS_ADMIN`
   → `CapEff=0x…a02c35fb`（**SYS_ADMIN 还在**）；不带 `--cap-add` 的
   `--cap-drop SYS_ADMIN` 才是 `0x…a00c35fb`。顺序无关、两种顺序都试过。若照抄原文，
   A7 会得到「自称无 SYS_ADMIN、实际带着它」的假证据。现实现：`PROD_DROP_CAPS` 同时
   ①把 cap 从 `--cap-add` 循环里摘掉、②保留 `--cap-drop` 兜底；探针数字见 §1。
   本 Task 未改 brief 文件（`.superpowers/sdd/task-A7-brief.md`），改动落在
   `test-prod-shaped.sh` 的注释与实现里。
2. **Step 3「常规三档无漂移」只跑了本改动唯一触及的 lane（生产形 phase 1 + phase 2），
   没重跑 gate A / gate B。** 理由：A7 不碰任何产品代码，gate A/B 也不用
   `test-prod-shaped.sh`（它们走 `tests --perf` 的 privileged 容器），数字不可能动；
   生产形两相则逐字对照了 A6 的 `tmp/a6-full-gate.log`（phase 2 完全相同，phase 1 只多
   96 条解禁用例）。若要求「六相全跑」的可复核证据，我可以补跑 `tmp/run-f31.sh`
   （已把它的 prod1 deselect 表同步收窄，约 25 min）。
3. **计划文件里带了控制器之前未提交的更正**（A5 那条「绝对路径同样 EACCES」、A2/A3 的
   count 更正）。它们是同一个 plan 文档的既有工作树改动，本次勾选必须落在这个文件上，
   所以一并提交（`git diff` 里可见）；如果希望它们单独成 commit，需要回退重排。
4. **背景运行被 OOM/kill 过两次**：`nohup` 起的 lane 在容器 `exit=137/1` 后日志截断
   （留档 `tmp/a7-nosa-aborted-bg.log`、`tmp/a7-nosa-run2.outer`），改用前台会话重跑；
   所以最终证据是 `tmp/a7-nosa.log`、`tmp/a7-default-lane.log` 两份完整日志
   （首行 ENV-HEADER）。另有一份 `tmp/a7-nosa-run1-redundant-args.log`：第一次调用多带了
   4 个文件参数（脚本本身已含 `tests`，pytest 去重），结果与最终一致（同为 1075/3/0），
   留作旁证。
5. **`tests/conftest.py` 未改**：Step 1c 只要求更正「口径」，实测结论是 strict-skips 只管
   6 个 runner 能力标记，因此改的是脚本注释 + 两份文档措辞，而不是放宽门禁。

---

## 6. 复现命令（都在 gitignored 的 `tmp/`）

```sh
# 无 SYS_ADMIN 全量（deliverable #2）
tmp/a7-run.sh                                  # = PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0

# 默认 lane（含 phase 2）漂移对照
A7_DROPS='' A7_UNPRIVILEGED_PHASE=1 A7_LOG=tmp/a7-default-lane.log tmp/a7-run.sh

# 只跑被移出 deselect 的 4 个单测文件（collect-only 实测 96 条）
tmp/a7-run.sh tests/unit/test_volume_quota.py tests/unit/test_xfs_project_quota_agent.py \
    tests/unit/test_quota_agent_client.py tests/unit/test_quota_maintenance.py
```

`tmp/a7-run.sh` 的 cap 探针依赖 `tmp/a7-bin/docker`（argv 回显桩），它保证探针用的是
**脚本当前**的 capset 而不是手抄的一份。

---

# Fix round 1（A7 评审）

**提交：`4cc6d95 docs(a7): record the breadth regression and the shape-scoped alias red (A7 fix round 1)`**
（只改 `docs/HANDOFF.md` §4b + `docs/superpowers/plans/…` 的 A7 Step 3；`1d0bbbe` 未 amend；
`tmp/**` 与 `.superpowers/**` 仍是 gitignored 证据。运行时 HEAD 是 `2936b20`；跑的过程中控制面
又落了 A6 fix-2 `a06e04c`，以及 fork 侧的 B1 `656bb31`——两者都不是本轮的改动。）

评审三条：① 补跑 gate A / gate B / macOS 的广度回归；② 报告里 `WRAPPER-EXIT=0` 不可复核；
③ A7 时点的三处过宽表述已由 A6 fix-1 在 HEAD 修正，报告要说清口径。**新增提交，没有 amend
`1d0bbbe`。** 结论：breadth regression **跑出两条红**，定性为「A4 新增用例的形状/平台口径
问题」，按评审要求**没有改任何测试或产品代码**，转 NEEDS_CONTEXT（详见 §F1.3）。

## F1.1 广度回归（gate A / gate B / macOS）

运行器 `tmp/a7-fix1-run.sh`（一相一容器、严格顺序、每份日志首行 ENV-HEADER、末行 `EXIT=<code>`；
phase 1 是 pinned 无 `SYS_ADMIN` lane，phase 2–4 是 gate A / gate B / macOS）。运行时 HEAD =
`2936b20`（`git rev-parse --short HEAD` 的实时值；注意**跑的过程中控制面又提交了 A6 fix-2
`a06e04c`**，所以证据日志的 commit 字段是运行时刻而非最终 HEAD，见 §F1.4）。

基线来源与 commit（**逐条写清**）：

| 相 | 最近一次全绿基线 | 基线 commit | 基线日志 |
|---|---|---|---|
| gate A（chroot） | `1069 passed / 4 skipped / 0 failed`（`GATE-A-EXIT=0`） | `569a70a`（2026-09-10 15:07，`docs: record the tier removal's measured end state, including phase 2`），即 `tmp/f31-gate-a.log` 写盘时刻之前的 HEAD | `tmp/f31-gate-a.log` |
| gate B（pure） | `1068 passed / 5 skipped / 0 failed`（`GATE-B-EXIT=0`） | 同上 `569a70a` | `tmp/f31-gate-b.log` |
| macOS | `989 passed / 84 skipped / 0 failed` | 同上 `569a70a` | `tmp/f31-macos.log` |

> 基线口径说明：这三份日志**早于 A4**（写于 2026-09-10 16:08/16:14/16:15），所以它们既不含
  A4–A7 新增的用例、也不含 A6 fix-1/fix-2。它们就是评审说的「A4 改了 `_view_cwd` / `mount_map`
  顺序 / `fs_mounts` 键集之后再没跑过」的那三档。

本次数字（运行器 `tmp/a7-fix1-run.sh`，日志尾部都有 `EXIT=`）：

| 相 | 本次结果 | 日志 | 漂移 |
|---|---|---|---|
| gate A（chroot） | `1104 passed, 4 skipped, 0 failed`（467.21s），`EXIT=0` | `tmp/fix1-gate-a.log` | **+35 passed，skip 不变**；见 §F1.2 的逐条账 |
| gate B（pure） | ⚠️ `1 failed, 1102 passed, 5 skipped`，`EXIT=1` | `tmp/fix1-gate-b.log` | 红 = `tests/contract/test_shared_volume_relative_cwd.py::test_volume_visible_from_both_workspace_aliases`（A4 新增） |
| macOS（宿主 venv，无 `--perf`） | ⚠️ `1 failed, 1023 passed, 80 skipped`，`EXIT=1` | `tmp/fix1-macos.log` | 同一条用例；`1023-989=+34` 是 base..HEAD 的净新增用例数（见 §F1.2），不是回归 |

## F1.2 gate A/B 的漂移逐条解释

**gate A `+35 passed`、skip 不变**（`1069 → 1104`）：

```
git diff --numstat 569a70a HEAD -- tests/      # 净新增 = 38 个 `def test_` - 3 个删除 = 35
  123/0   tests/contract/test_shared_volume_relative_cwd.py   (A4 新增)
  425/0   tests/unit/test_shared_volume_traversal.py          (A5 新增 13 条)
  235/0   tests/unit/test_upgrade_quota_agent_profile.py      (A6 fix-1/fix-2)
   95/0   tests/unit/test_xfs_project_quota_agent.py          (A6 新增)
   58/0   tests/unit/test_quota_agent_client.py               (A6 新增)
   39/0   tests/unit/test_policy_mapping.py                   (A4 新增)
   57/0   tests/unit/test_worker_manifest_permissions.py      (A6 fix-1)
    0/116 tests/unit/test_runtime_context_volumes.py          (A4 删除：bind 物化的单测)
  +小改动 test_executor_policy / test_sandlock_executor_route_b / test_volume_quota / test_quota_maintenance
```

对既有断言的改动**只有**评审预期的那一类（`/workspace` → `/home/user` 与双别名），实测
`git diff 569a70a HEAD -- tests/unit/test_executor_policy.py tests/unit/test_sandlock_executor_route_b.py
tests/unit/test_volume_quota.py tests/unit/test_quota_maintenance.py` 里的断言行只有：

```
  assert params["cwd"] == "/workspace"        ->  assert params["cwd"] == "/home/user"
  assert captured["fs_mounts"] == {"/workspace/mnt/data": str(subdir)}
    ->  {"/workspace/mnt/data": str(subdir), "/home/user/mnt/data": str(subdir)}
  + assert list(sb.fs_mount)[:2] == ["/home/user", "/workspace"]
```

没有格式/语义外的断言改写。**gate A 零 failed、skip 两条都与基线相同**（4 条 skip 是
`pure-shape workspace ownership` / `test_volume_quota.py:274 XFS 降级路径` /
无特权 worker 形态 / 非 root worker），逐条非新增。

## F1.3 两条红的定性：A4 新增用例的形状/平台口径（NEEDS_CONTEXT，未改任何测试）

**红是同一条用例**：`tests/contract/test_shared_volume_relative_cwd.py::test_volume_visible_from_both_workspace_aliases`
（A4 为「双别名」写的契约），只是失败原因在两个相里不同。

1. **gate B（pure 形态）= 形状口径，非 A4/A5 产品回归。** 复现（`tmp/a7-fix1-alias-shape-pair.log`）：
   ```
   pure-shape          1 failed, 1 passed   (EXIT=1)
   pure-shape-repeat   1 failed, 1 passed   (EXIT=1)   <- 确定性，不是 flake
   chroot-shape        2 passed             (EXIT=0)
   ```
   逐步探针 `tmp/a7-fix1-alias-probe.py`（`-p tests.conftest`，只打印不新断言语义；输出见
   `tmp/fix1-alias-probe.log`）在 pure 形态下给出：
   ```
   01 pwd                -> /var/lib/e2b-sandboxes/_test-runtime/<id>/sbx_<id>   (宿主路径，不是 /workspace)
   02 ls /home/user      -> ls: cannot access '/home/user': No such file or directory
   03 echo > mnt/data/a.txt; cat mnt/data/a.txt -> rc=0 / hello     <- pure 形态自己的工作区相对路径：成立
   04 cat /workspace/mnt/data/a.txt             -> ENOENT
   05 cd /home/user                             -> "can't cd to /home/user"
   ```
   即：`fs_mounts` 的 `/workspace/<rel>` + `/home/user/<rel>` 是 **chroot 形态**的虚拟别名
   （`_view_cwd` 只在有 base image 时把 cwd 映射到 `/home/user`；pure 形态 cwd 就是宿主
   workspace 路径，`/home/user` 在镜像里根本不存在）。所以这条契约的断言在 pure 形态**永远
   不可能成立**，而 pure 形态自己的契约（工作区相对路径读写）**在 A4 之后仍然成立**（步骤 03）。
   ⇒ **不是** A4/A5 引入的 pure 形态产品回归，而是 A4 的用例没有按形状收口。
2. **macOS = 形状 + 平台口径。** 同一条用例在 macOS 上还多一层：macOS 没有 Landlock
   （`E2B_EXECUTOR=sandlock requires Landlock ABI >= 6`），沙箱根本起不来 ⇒ 该用例在 macOS
   永远跑不了。macOS 相本来就靠 80 条「Linux/Landlock/root 不满足」的能力型 skip 维持，
   这条新用例没进那套口径，于是把 macOS 从 `0 failed` 拉成 `1 failed`。

**处置（按评审要求「真缺陷就停下报 NEEDS_CONTEXT，不要改断言糊过去」）**：我没有改这条用例、
没有加 skip/ignore、没有动产品代码。可选修法（需你拍板）：

1. 把该用例限定在 chroot 形态（形如 `pytest.mark.skipif(not E2B_BASE_IMAGE ...)` 或拆成
   「chroot 双别名契约」+「pure 工作区相对路径契约」两条）——最贴合事实，但要新增/改写
   A4 的契约用例；
2. 让 pure 形态也暴露 `/home/user`（产品/部署改动：镜像内建 `/home/user` 或在 worker 启动时
   建别名目录）——语义上要论证 pure 形态是否真需要这个别名；
3. 承认 pure 形态没有「双别名」契约，改写 A7 交付里那句「卷视图在绝对/相对、两个别名下都成立」
   的文档口径（当前 `docs/HANDOFF.md` 顶部块与 §2.4.2 的措辞是**无形状限定**的）。

## F1.4 其余两条

- **[Minor-4] `WRAPPER-EXIT=0` 已可复核。** `tmp/a7-run.sh` 与 `tmp/a7-fix1-run.sh` 现在都把
  `EXIT=<code>` 追加到日志尾部（评审建议的做法）。`tail -1 tmp/a7-nosa.log` = `EXIT=0`（本轮重跑），
  `tmp/a7-default-lane.log` 尾部同样有 `EXIT=`；`tmp/a7-nosa-flake-multinode.log`（本轮第一次
  全量）是 `EXIT=1`——那是**多节点用例的 flake**，见下。
- **pinned lane 的 flake（不是 A7 引入）**：第一次全量在
  `tests/sdk/python/test_multinode.py::test_create_routes_to_remote_worker` 红了一条：
  `instance exec failed: … instance is closed`。定性为负载型 flake：同一命令在该次跑了
  **647.98s**（安静时 301–310s；当时 `uptime` 负载 8.6→13.4），同一棵树的 2936b20 自带证据
  `tmp/a6fix1-nosa-gate.log` 是 `1088/3/0`，而**单文件重复 3 次全绿**（`6 passed` ×3，
  `tmp/a7-multinode-repeat.log`，首行 ENV-HEADER、每轮 `EXIT=0`）。
- **[口径归属] A7 时点 vs HEAD。** 我 `1d0bbbe` 里的三处措辞是按 A7 时点的事实写的，其中
  「k8s 形态是 root pod、`:53` 由 `NET_BIND_SERVICE` 覆盖」「全库只剩一处用途（无限定）」
  以及计划里 A6 Step 1 的 k8s 半边在当时就被提前勾上 —— 这三处**已在 HEAD 由 A6 fix-1
  `2936b20` 修正**（k8s worker 是镜像里的 uid 65534、必须声明 pod 级
  `securityContext.sysctls`；「不需要 `SYS_ADMIN`」限定为出厂镜像 + 清单形态，并点名两条
  非默认代码路径）。本报告只做归属说明，**没有改 `2936b20` 的任何内容**；`docs/HANDOFF.md`
  顶部块的措辞也由 `2936b20` 就地加了限定，本轮未再动它。

## F1.5 本轮新增证据文件

`tmp/fix1-gate-a.log` / `tmp/fix1-gate-b.log` / `tmp/fix1-macos.log`（各首行 ENV-HEADER、末行 `EXIT=`）、
`tmp/a7-fix1-alias-shape-pair.log`、`tmp/a7-fix1-alias-probe.py` + `tmp/fix1-alias-probe.log`
（逐步探针的真实输出，见下）、`tmp/a7-multinode-repeat.log`、`tmp/a7-nosa-flake-multinode.log`。

---

# Fix round 2（控制器裁定：形状限定，方案 ①）

**提交：`0898003 test(contract): scope the both-alias volume contract to the chroot shape (A7 fix round 2)`**
（`tests/contract/test_shared_volume_relative_cwd.py` + `docs/HANDOFF.md` §4b/§5 +
`docs/superpowers/plans/…` 的 A7 Step 3；未 amend 任何既有提交；无 `--ignore`、无产品代码改动）。

## F2.1 抄的是哪个 idiom（评审要求点名）

- **抄的对象**：`tests/contract/test_pure_shape_workspace_ownership.py` 第 **75-80 行**的
  `_NO_BASE_IMAGE = pytest.mark.skipif(bool(os.environ.get("E2B_BASE_IMAGE")), reason="pure-sandlock
  contract requires an empty E2B_BASE_IMAGE (gate B shape)")`，以及它在第 **113 行**的用法
  `@_NO_BASE_IMAGE`（marker 对象 + 装饰器，模块级只声明一次）。
- **同目录另一处同族惯用法**（读过、未采用，因为它要求 root + sandlock 而不是形状）：
  `tests/contract/test_uid_permissions.py:41` 的
  `pytestmark = pytest.mark.skipif(os.geteuid() != 0 or not sandlock_ready(), reason=…)`。
  另外 `tests/security/test_fork_network_features.py:281` 是在测试体里
  `pytest.skip("requires E2B_BASE_IMAGE (image-rootfs sandboxes)")` —— 同样是形状门控，
  但形式是函数内 skip，不是 marker；这里选的是与前两者一致、且与同目录文件名对的 marker 写法。
- **落点**：`tests/contract/test_shared_volume_relative_cwd.py` 新增
  ```python
  _IMAGE_ROOTFS_ONLY = pytest.mark.skipif(
      not os.environ.get("E2B_BASE_IMAGE"),
      reason=(
          "image-rootfs contract requires a non-empty E2B_BASE_IMAGE "
          "(chroot shape); the pure shape runs with the host workspace cwd "
          "and has no /home/user alias"
      ),
  )
  ```
  并把它装饰在 `test_volume_visible_from_both_workspace_aliases` 上（文件第 44 行起）。
  **断言一个字未改**（仍是 `assert code == 0` / `stdout == b"hello\nhello\nhello\n"` /
  `stderr == b""` 的精确比对），也没有加 `--ignore`。

## F2.2 重跑与对照（日志首行 ENV-HEADER、末行 `EXIT=`）

| 相 | fix round 2（最终） | fix round 1（门控前） | 基线 `569a70a` | 日志 |
|---|---|---|---|---|
| gate A（chroot） | `1104 passed / 4 skipped / 0 failed`，`EXIT=0`（300.83s） | `1104 / 4 / 0` | `1069 / 4 / 0` | `tmp/fix2-gate-a.log` |
| gate B（pure） | `1102 passed / 6 skipped / 0 failed`，`EXIT=0`（296.83s） | `1102 / 5 / 1 failed` | `1068 / 5 / 0` | `tmp/fix2-gate-b.log` |
| macOS（宿主 venv，无 `--perf`） | `1023 passed / 81 skipped / 0 failed`，`EXIT=0`（175.89s） | `1023 / 80 / 1 failed` | `989 / 84 / 0` | `tmp/fix2-macos.log` |

- **gate A 里该用例真的执行**：gate A 的 skip 表只有 4 条
  （`test_pure_shape_workspace_ownership.py:113` / `test_volume_quota.py:274` /
  `test_template_isolation.py:162` / `test_uid_pool.py:330`），**没有**
  `test_shared_volume_relative_cwd.py` 的条目；聚焦复跑该文件在 chroot 形态是 `2 passed`
  （`tmp/fix2-alias-shape-pair.log`，`EXIT=0`）。
- **skip 增量逐条对齐**：
  - gate B：`5 → 6`，新增的唯一一条 =
    `tests/contract/test_shared_volume_relative_cwd.py:44: image-rootfs contract requires a
    non-empty E2B_BASE_IMAGE (chroot shape); the pure shape runs with the host workspace cwd
    and has no /home/user alias`；其余 5 条与 fix1/基线逐字相同
    （`test_volume_quota.py:274` / `test_fork_network_features.py:281` /
    `test_template_isolation.py:44` / `test_template_isolation.py:162` / `test_uid_pool.py:330`）。
    passed 仍 1102、failed `1 → 0`。
  - macOS：`80 → 81`，增量同样只有这一条（同一 reason 文本，`…:44`）；passed 仍 1023、
    failed `1 → 0`；其余 skip 全是既有的 Linux/Landlock/chown/root 能力类。
  - 若与**基线**（`tmp/f31-*`）比：gate B `5 → 6`（+1）、macOS `84 → 81`（−3，因为 A4–A6 期间
    有 3 条 macOS 能力类 skip 变成实际执行/新增用例，与本次门控无关；本次只加 1 条）。

## F2.3 双别名契约的形状无关那半（评审要求 3）

两条都**在**、都是精确断言、都不需要 Linux/root/形状，macOS 上也跑：

- `tests/unit/test_policy_mapping.py::test_volume_views_map_under_both_workspace_aliases`
  （第 84 行起）：`assert sandbox.fs_mount["/workspace/mnt/data"] == volume` 与
  `assert sandbox.fs_mount["/home/user/mnt/data"] == volume`（第 116-117 行），另加
  `list(sandbox.fs_mount)[:2] == ["/home/user", "/workspace"]` 的声明顺序断言。
- `tests/contract/test_shared_volume_relative_cwd.py::test_runtime_context_registers_both_volume_aliases`
  （同文件内、非沙箱/非平台依赖）：`assert captured["fs_mounts"] == {"/workspace/mnt/data": str(vol),
  "/home/user/mnt/data": str(vol)}`。

实测（macOS 宿主 venv，`tmp/fix2-shape-independent-alias-assertions.log`，首行 ENV-HEADER、
末行 `EXIT=0`）：
```
tmp/testenv/bin/python -m pytest tests/unit/test_policy_mapping.py::test_volume_views_map_under_both_workspace_aliases \
  tests/contract/test_shared_volume_relative_cwd.py::test_runtime_context_registers_both_volume_aliases -q
2 passed in 0.05s
```
⇒ 端到端那条按形状门控之后，「双别名」这条契约**唯一证据没有丢**：形状无关的半由这两条单测守，
形状相关的端到端由 gate A（chroot）守。

## F2.4 本轮新增证据文件

`tmp/fix2-gate-a.log` / `tmp/fix2-gate-b.log` / `tmp/fix2-macos.log`、
`tmp/fix2-alias-shape-pair.log`（pure `1 passed, 1 skipped` / chroot `2 passed`）、
`tmp/fix2-shape-independent-alias-assertions.log`。fix round 1 的日志一律保留供对照。

## F2.5 证据与提交的对应关系

fix round 2 的三相在 06:27–06:41Z 跑，日志首行 `ENV-HEADER commit=4cc6d95 …`——那时的 HEAD 是
fix round 1 的文档提交，**门控这次改动当时还在工作树里**（14:27:17 写盘，早于第一次运行）。
提交后核对：`tests/contract/test_shared_volume_relative_cwd.py` 的 blob
`sha256=b95e9b0140f905e07873df405d1cfed4add4213f831c74c0146fecd6d8d7b67a`，与
`git show 0898003:…` 逐字节相同 ⇒ 三相跑的确实是提交里那份门控。
