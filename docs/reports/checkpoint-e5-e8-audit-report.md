# checkpoint/restore 计划尾部 E5–E8 收口审计报告（2026-09-27）

工作树共享（另有 N41/N27/N37/N39 在并行改别的文件）。本单**只读证据 + 只补"决定已经拍过"
的那部分文档**，没有实现任何计划里没写形状的东西；未碰集群、未起资源。

## 0. 结论先行

* **计划正文从未定义 `Task E5`–`E8`**：正文写到 `Task E4`（`:1673` 起）就结束，全文 1868 行。
  E5–E8 只出现在**依赖表**（`:53-59`）、**决策点表**（`:96-108`）、**验收矩阵**与正文里的
  零散引用。→ 本轮给每一处引用定处置，并为"定义缺失"本身登记。
* **已悄悄做完的**：决策 5（公开只读端点）、决策 6（验收脚本转正）、决策 3 的"数字可见"那半、
  决策 7 的清单落地、F3/F4 两条决定门的**事实核对**、以及 `test_checkpoint_restore_unused.py`
  的 docstring（**已与事实一致**，不需要按计划原话改）。
* **没做的**（本轮只补文档/登记，不实现）：决策 2 的对外语义（本轮补写）、决策 3 的**告警**、
  决策 4（`E2B_PAUSED_TTL_S`，待决策）、E5 的定义、E8 里"孤儿图会回收"这句话。
* **发现一个真缺陷并修掉**：守卫用例 `test_checkpoint_restore_unused.py` 会**空转** ——
  `offenders == []` 也是"什么都没扫"的返回值。已按 TDD 补两条用例（RED→GREEN）。

## 1. 审计表

（同一张表已落进
`docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md` 尾部的
《E5–E8 收口审计（2026-09-27）》，本节给证据原文摘录与判理由。）

### 1.1 决策点表 7 行

| 条目 | 现状证据（文件:行） | 处置 |
|---|---|---|
| 1 余项：OCI / `--restore-from` 是否也支持 exec | `rg -n "restore-from\|restore_from" envd_service control_plane deploy tests` = **0 命中**（2026-09-27 复核）；结论此前只在 gitignored `.superpowers/sdd/progress.md:2319`（"F4 已裁定不做"） | 随裁定取消（不做）；**本轮补写**到 `docs/checkpoint-restore-e2b-half.md` §2 D9 行 + §6(k)⑤ |
| 2 恢复后 stdout 是否接平台日志（今天 `/dev/null`） | 机制有据：`envd_service/route_b.py:332`、`deploy/k8s/worker.yaml:490-492`、`docs/checkpoint-restore-e2b-half.md` §6(j)；**对外语义全库无** | **本轮补写** §6(k)① |
| 3 软账 + 并发可超；暴露 `used/budget` + 告警 | 见 §1.3 | 数字 ✔ / 口径**本轮补写** / **告警 → 登记 open-issues** |
| 4 `E2B_PAUSED_TTL_S`（默认 0） | 见 §1.4 | **未做 + 待决策 → 登记 open-issues** |
| 5 公开端点 `GET /sandboxes/{id}/checkpoint` | `control_plane/api/sandboxes.py:2764-2832`；契约 `tests/contract/test_checkpoint_status_api.py` | 已做 ✔ |
| 6 转正 `checkpoint_acceptance.py` | `deploy/scripts/checkpoint_acceptance.py`（前置断言 `:379-380`） | 已做 ✔ |
| 7 `E2B_PAUSE_CHECKPOINT` 长期默认 `"1"` + 代价写进文档 | `deploy/k8s/worker.yaml:483-484`（`"1"`）、代价注释 `:455-492`；代码默认仍关 `envd_service/config.py:378` | 已做 ✔（清单注释即代价）；结论**本轮补写** §6(k)④ |

### 1.2 依赖表里 E5–E8 的引用

| 引用 | 证据 | 处置 |
|---|---|---|
| `E4 → E8`（文档写"孤儿会回收"） | 代码：`envd_service/runtime/checkpoint_store.py:640/681/714`、`envd_service/agent.py:2345`/`:2423`、用例 `tests/unit/test_quota_maintenance.py:1686`；**文档 0 命中** | 代码 ✔ / 文档**本轮补写** §6(k)③ |
| `E5 → E8` | `rg -n 'E5'` 全计划仅 `:38`/`:44`/`:56`/`:59`，无正文定义 | **计划缺口 → 登记 open-issues** |
| `E6 → E8` | 同决策 4 | 未做 + 待决策 |
| `E7 → E8` | 同决策 3（"暴露数字"已在 S2 落地，§6(f)）；端到端由 Task 2 转正 | 部分 |
| `E8 → 全部` | 见 §1.5 | 本轮收口 |

### 1.3 决策 3 的展开（`used/budget` 可见性与告警）

* 上报到节点视图：`envd_service/agent.py:265-266`（`_platform_disk_report`）→
  `control_plane/api/internal.py:106-107`（心跳摄取）→
  `control_plane/registry/nodes.py:206-207`（`NodeRecord.as_dict` 的 `platformDiskUsedMB`/`BudgetMB`）。
* worker 的 checkpoint 回复也带：`envd_service/runtime/checkpoint_store.py:473-474`。
* **公开只读端点不带**：`control_plane/api/sandboxes.py:2825-2832` 调
  `checkpoint_status()`，返回只有 `sandboxID/hasImage/imageMB/capturedAt/lastRestore`
  （`checkpoint_store.py:302-308`）。
* **口径句**"软账 + 并发可超"全库只在计划 `:102`（`rg '软账|并发可超' docs/` 唯一命中）。
* **无告警**：`rg 'alert|PrometheusRule' deploy/` 0 命中。

### 1.4 决策 4 的展开（`E2B_PAUSED_TTL_S`）

```
$ rg -n 'E2B_PAUSED_TTL_S|PAUSED_TTL|paused_ttl' . --glob '!*.log' --glob '!.git/**'
./docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md:103: ... Task E6 |
```

只命中计划自身 ⇒ **没有实现**（`envd_service/config.py` 里没有这个开关，也没有别的
"paused 过期"机制：`pause` 今天就是"冻结 / 写图"，没有任何定时清理）。按纪律**不发明形状**，
登记为**待决策（默认 0 = 不启用）**。

### 1.5 Task E8 的守卫项：docstring 与 FORBIDDEN 是否与事实一致

计划 `:21` 与 Global Constraints 断言"Task E8 必须把它的 docstring 改到与事实一致"。
**逐条验下来它今天已经一致**，所以**不需要改 docstring**（这本身就是一条审计结论：
计划的那句假设是旧的，S0 已经把它修过了，见 `docs/checkpoint-restore-e2b-half.md` §3 的 S0 行）。

| docstring 说法 | 事实（文件:行） |
|---|---|
| stub 靠描述符投递（`execveat(AT_EMPTY_PATH)`）+ 规则集给那一个宿主文件 `EXECUTE\|READ_FILE` | `third_party/sandlock/crates/sandlock-core/src/sandbox.rs:1445-1463` |
| `test_restore_resumes_inside_a_chroot_root` 两种根都跑、断言计数器前进 + fd 表干净 | `.../tests/integration/test_restore.rs:192`（`:199` 两态循环、`:283-294` 断言） |
| 引擎覆盖 x86_64/aarch64/riscv64；stub 有 `__aarch64__`；build.rs 对缺失 stub 判 fatal | `sandbox.rs:1392-1398`、`restore-stub.c:2/85`、`build.rs:47-58` |
| E2B 侧已建（S2/S3/S4） | `docs/checkpoint-restore-e2b-half.md` §3/§6 |

`FORBIDDEN = (".checkpoint(", "restore_interactive", ".restore_skipped(")` 与计划 `:21` 逐字相同，
与代码事实一致（这三条正是 wheel 的 `Sandbox`/`SandboxInstance` 绑定形状）。

### 1.6 守卫的"看着在守、其实没守"（本轮修）

**洞**：`assert offenders == []` 同时被"扫到了、没人违规"与"什么都没扫到"满足。
`tmp/e5e8/guard_probe.py` 实测：

```
real envd_service *.py files scanned: 50
REAL TREE: clean (test passed)
needle '.checkpoint(': caught (RED as expected)
needle 'restore_interactive': caught (RED as expected)
needle '.restore_skipped(': caught (RED as expected)
EMPTY TREE: silently passes -> vacuous if envd_service vanishes (or has no .py)
```

即**三根针都会红（不是假守卫）**，但 `envd_service/` 一旦改名/不再有 `.py`，它会静默变绿。

**修法（TDD）**：把扫描抽成可传入根目录的 `_worker_sources(repo)` / `_offenders(repo)`，加两条用例：

* `test_the_scan_actually_reads_the_worker_tree` —— 真树必须非空；
* `test_the_scan_flags_a_call_site_in_a_synthetic_tree` —— 合成树里的 `.checkpoint(` 调用点必须
  被判成 offender，**整表相等**：`assert _offenders(tmp_path) == ["envd_service/bad.py: .checkpoint("]`
  （禁用子串判据；用 `tmp_path` 而不是 monkeypatch 真 `REPO`，避免与并行 agent 抢工作树）。

RED→GREEN：

```
$ tmp/testenv/bin/python tmp/e5e8/guard_probe2.py
empty tree, _worker_sources: []
empty tree, _offenders     : []
RED on empty tree -> the non-vacuity assertion fires (fix works)
GREEN on the real tree -> ok
$ tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_restore_unused.py -q
3 passed in 0.07s
```

## 2. 两条条件任务的决定门

| 任务 | 决定门命令 | 今天的结果 | 处置 |
|---|---|---|---|
| F3 | `rg -n "os\.replace\|mv \|rename" docs/checkpoint-restore-e2b-half.md docs/k8s-deployment.md` | 命中：`docs/checkpoint-restore-e2b-half.md:339-340`（引擎保存 `<dir>.tmp`→rename）、`:501`（验收脚本"临时文件 + `os.replace`"）、`docs/k8s-deployment.md:1678/2006/2139/2523/2563/2596/2630`（磁盘记账/迁移的 rename）。**没有"业务文件在 pause 与 resume 之间被 replace"的用法** | **不做（默认）**；"同路径换 inode 不校验"作为已知边界补进 §6(k)⑤ |
| F4 | `rg -n "restore-from\|restore_from" --glob '!third_party/sandlock/target*' envd_service control_plane deploy tests` | **0 命中**（无消费者） | **不做（默认）**；结论补进 §2 D9 行 + §6(k)⑤ |

## 3. 补写的"决定已拍过、只是没写下来"的话（落点）

全部落在 `docs/checkpoint-restore-e2b-half.md` 新增的 **§6(k)**（六条）：

1. 恢复出来的进程 stdout/stderr = `/dev/null` ⇒ **恢复的沙箱日志消失**（决策 2）；
2. 平台账是**软账**、允许并发短超；数字已上报、**告警未做**（决策 3）；
3. **孤儿图会被 reconcile 回收**（Task E4 的文档面）；
4. `E2B_PAUSE_CHECKPOINT` 生产清单默认 `"1"` 的**代价**（决策 7）；
5. F3/F4 两条决定门的结论（含 F3 的已知边界）；
6. `E2B_PAUSED_TTL_S` 未拍板 ⇒ 有意不实现（决策 4 的登记指针）。

另外就地更正了两处**过期/断链**：

* `docs/checkpoint-restore-e2b-half.md` §0 的"仓库里没有任何'用户要这个'的记录 / 需求仍未确认"
  —— 计划 `:27-28` 就点名它是过期的（用户 2026-09-26 已裁定有需求）；
* 同文 `:62` 的 `（见 §6(h)）` —— §6 没有 `(h)` 小节，`exclude_main` 实际在 §6(i)②，改为
  `（见 §6(i) ②）`；
* `docs/open-issues.md` 的 checkpoint 行两处"需求仍未确认"同步更正。

## 4. 文件清单与提交

改动（只碰本单负责的文件；含 `git diff --cached --name-only` 核对）：

* `tests/unit/test_checkpoint_restore_unused.py` —— 守卫补非空断言 + 合成树 RED 用例；
* `docs/checkpoint-restore-e2b-half.md` —— §0 更正、§6(i)② 引用、D9 行、新增 §6(k)；
* `docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md` —— 尾部新增《E5–E8 收口审计》；
* `docs/open-issues.md` —— checkpoint 行：需求状态更正 + E5–E8 审计结论（待决策/告警/缺口）。

未提交（gitignored）：本报告、`tmp/e5e8/guard_probe.py`、`tmp/e5e8/guard_probe2.py`。

验证：`tmp/testenv/bin/python -m pytest tests/unit/test_checkpoint_restore_unused.py -q` = 3 passed；
checkpoint 家族（`test_checkpoint_store` + `test_agent_checkpoint_restore` +
`test_sandlock_executor_route_b` + `test_quota_maintenance` + `test_checkpoint_status_api`）
= 134 passed。

## 5. 残留与担忧

1. **E5 的形状仍然缺**：本单只能证明"计划没定义它"，不能替计划作者定它是什么。已登记
   open-issues；若它本该是某项 E2B 工作（架构行的"只读查询端点/孤儿图回收/paused 过期策略/
   计数指标"四件里，E3/E4/E6/E7 各有归属，E5 没有），需要一个形状才能判"做没做"。
2. **决策 3 的"告警"没人做**：数字已在节点视图上，但没有任何消费者报警；平台账满时
   只有"拒绝这次 checkpoint + 退回 SIGSTOP"这一条用户可见路径。已登记。
3. **`E2B_PAUSED_TTL_S` 的默认 0 只是"计划里的默认"**，不是被复核过的决定；本轮按纪律
   不实现。登记为待决策。
4. **`docs/checkpoint-restore-e2b-half.md` 与 `open-issues.md` 的 checkpoint 行仍有多处
   2026-09-25 的历史叙述与"需求未确认"不同期**，本轮只改了被点名的两处；没做大扫除，
   免得与并行 agent 抢工作树。
5. **未跑容器 lane 与集群**：本单是文档/守卫改动，按"只读集群"约束未起资源；若需要，
   `tests/unit/test_checkpoint_restore_unused.py` 与家族单测已足够覆盖本次代码改动。
