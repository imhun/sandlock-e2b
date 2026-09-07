# A/B cleanup 剩余任务执行计划（FUP-11 + wheel/E2B 收口）

> **For agentic workers:** 按任务顺序执行；代码改动先落测试/证据再实现；每任务
> 独立可验证后提交。fork 侧进度账本 `third_party/sandlock/.superpowers/sdd/progress.md`，
> main 侧 `.superpowers/sdd/progress.md`；收口台账 fork-plan-followups /
> HANDOFF / task-backlog。

**Goal:** 把 fork A/B cleanup 波彻底收口——FUP-11 剩余硬化全部落地，wheel 按
当前 fork tip 重建并 verify，E2B 侧 bump + 探针 + full gates 复跑全绿，文档/报告
同步。

**Architecture:** fork（`third_party/sandlock`，本地分支
`upstream-pr/netns-free-clean`，HEAD=`85aef14`，不推送）剩余代码改动集中在
supervise 测试/日志与可能的小生产修正；wheel 重建产出同步到 main
`wheels/fork/`；随后 main 仓库 bump 子模块指针并做 E2B 探针/门禁。

**Tech Stack:** Rust workspace、sandlock-dev:latest 容器门禁、manylinux cp314
wheel 管线、E2B test-runner（e2b-sandlock-test:latest）、pytest。

## Global Constraints

- 不推送远程；fork/main 均为本地提交。
- 测试断言精确（禁 contains 部分匹配动态文本）；禁止新增 skip/掩盖。
- 门禁计数按 `docs/test-baseline.md` 登记增量；性能改动必录 profile/样本。
- 临时产物放各仓库 `tmp/`；证据日志带 ENV-HEADER（commit/env/镜像/时间）。

---

## Task 0 — 前置核对（30 分钟）

- [x] 确认 fork HEAD、main HEAD、`wheels/fork` 的 manifest HEAD 三者关系；
- [x] 确认当前 baseline 数字（fork 全量 833/534/100/100/36/3/0/454 +
  144/3/9；E2B gate A/B/macOS 上次基线）；
- [x] 把本计划登记到 fork `.superpowers/sdd/progress.md` 与 main
  `.superpowers/sdd/progress.md`。

## Task 1 — FUP-11 硬化（fork，预计 0.5–1 天工作量）

**Files:**
- 审计：`crates/sandlock-supervise/tests/supervise.rs`、
  `crates/sandlock-supervise/src/serve.rs`、
  `crates/sandlock-supervise/src/lib.rs`、
  `crates/sandlock-core/src/init/executor.rs`、
  fork `.superpowers/sdd/review-*.diff`（F2b.1/F2b.3 原始指向）

子项处置（每项独立提交或合并小提交）：

- [x] 1a error-path `contains` 收敛：supervise.rs 中 EOF / wrong-token /
  policy-oversize / registered 拒绝等错误断言转精确（先跑取证错误文本，
  用 prefix+fixed-suffix 或全行 pin，不猜）。
- [x] 1b `FORBIDDEN_RUNTIME_MEDIATOR_REMAP` 常量测试引用：mediation_2uid 或
  lib 单测断言 refusal 消息与该常量一致。
- [x] 1c registered 连接异常 eprintln：确认 serve_registered_path 的
  PeerGone 日志频度；若每连接一条且无上限，改为“首条点名 + 计数/节流”并在
  测试 pin（若本就是低频单次，记录证据关闭）。
- [x] 1d registered 首 verb recv 超时 vs connect 重试不对称：先定位 30 s /
  120 s 常数与注释；补注释说明取舍；若可低成本加单测则加。
- [x] 1e stats settle 断言强度：确认 `wait_stats_settled`（F2b.4 已加强）
  覆盖非 root path → 证据关闭子项，不重复改。
- [x] 1f `--program` + validate-exit：审计该模式是否仍存在（当前 supervise
  main 未见 validate-exit）；存在则补测试，不存在则记录“模式已移除”关闭。

**验证：** fork 非 root 全套 + root 三档按 baseline 或登记增量绿；逐子项提交；
followups FUP-11 状态行关闭；CHANGELOG 补 FUP-03/14/16/18 条目（若前波漏补）。

## Task 2 — wheel 重建 + verify（fork，约 20–40 分钟 wall）

- [x] `python/build-wheels.sh` 在当前 tip 重建 cp314 双架构 + supervise 注入
  （实跑 FUP-16 的 RECORD replace-in-place 新逻辑）；
- [x] `HEAD=<tip> python/verify-wheel.sh` 全绿：RECORD 行、0755、euid+--uid、
  指纹三方一致、FFI 符号双向相等；
- [x] 同步 `wheels/` → main `wheels/fork/`（含 supervise/ 与 SHA256SUMS）；
- [x] 日志/样本存 fork `tmp/sdd/ab-wheel-*.log`；wheel 产物不入库。

## Task 3 — E2B bump + 探针 + full gates（main，约 1–1.5 小时 wall）

- [x] main 子模块 bump：`chore: bump sandlock submodule to <fork tip> (A/B
  cleanup wave)`；
- [x] 重建 `e2b-sandlock-test:latest`（新 wheel）；
- [x] thread 探针 GREEN（`tmp/perf/ab-thread-probe.log`）：线程化 python A
  存活时 exec B exit 0 / `b-ok\n`；
- [x] FUP-E3 gateway+命令 pure 4/4（`tmp/perf/ab-gateway-probe-*.log` +
  evidence）：450M holder + 50M 控制成功 + 第二 450M 拒绝 137 +
  `memoryMB==1024`；
- [x] full gate A（image-rootfs python-mcp:3.14，concurrency=2）0 failed，
  计数登记（含 FUP-14 事件化在 E2B 栈上的 gateway+命令回归）；
- [x] full gate B（pure sandlock）0 failed；
- [x] macOS 全量（unit+contract+sdk python/js+security）0 failed；
- [x] 若门禁暴露问题：修 main 侧或回流 fork（新 fork 提交 → 重跑 Task 2/3）。

## Task 4 — 文档收口（main + fork）

- [x] main：docs/HANDOFF.md 顶部块（A/B cleanup + E2B 复跑数字与 commit）、
  docs/task-backlog.md（A/B wave 登记与 #15/#17/#18 状态行）、
  `.superpowers/sdd/progress.md`；
- [x] fork：fork-plan-followups 全部 A/B 状态行、CHANGELOG、
  e2b-integration §5 wheel/tip 行（如需要）；
- [x] 报告 `tmp/sdd/ab-cleanup-report.md`（fork 侧）与
  `tmp/sdd/ab-e2b-report.md`（E2B 侧）：门禁摘要、延迟/样本、commit 链；
- [x] main docs+pointer 提交；fork 无新改动则无需再提交。

## Task 5 — 终态核对

- [x] fork HEAD = wheel manifest HEAD = main 子模块指针；
- [x] 全量门禁摘要写入 HANDOFF；工作树仅剩既有 untracked `target/`；
- [x] open 项清单收敛到非本波范围：E2B #4（SDK 可见性，产品决策）、#5
  （T5 route-B）、#11（日志头纪律）、#13/#14（G2 登记）与环境受限项
  （SL-1/T1/O1-O3/E8.1）。

## Risks

- FUP-11 1a/1d 需先取证错误文本，禁止凭猜 pin；
- wheel 重建需要网络/buildx 可用，耗时以日志为准；
- FUP-14 事件化是 core 行为改动，E2B gateway+命令契约是主要回归面；
- 全部本地提交，无推送（约束照旧）。


---

## 执行结果（2026-09-07 收口）

Task 0–5 全部走完，未推送。终态：**fork HEAD == wheel manifest HEAD == main 子模块
指针 == `ee66234`**；三次 wheel 重建（`d054c11` / `603b546` / `ee66234`）的双 wheel 与
双 supervise sha256 逐个相同 ⇒ 文档提交不动产物。

- Task 1（FUP-11 六子项）：全部关闭。fork 提交 `1bd3b82`/`8e65476`/`eadd383`/`d054c11`；
  计数 supervise 36→42、supervise_root 3→4；唯一用户可见行为变化 = registered slot
  异常连接日志改节流。fork 非 root 8 档 + root 三档全绿。
- Task 2（wheel）：重建 + verify 全绿（FFI 156=156 双向、RECORD 精确、mode 755、
  三方指纹、`--uid` 冒烟）。**顺带修掉 verify 自身的假失败**（无 `unzip` 时
  `python3 -m zipfile -e` 不还原 mode ⇒ 正确 wheel 被判 0644 红），改以 wheel 记录的
  mode 为权威 = fork `6b76e71`。体积 10.4/9.5 → 8.3/7.4 MB（FUP-15 首次进 wheel）。
- Task 3（E2B）：镜像 `39ed2a82b08b`；pip 真机 0755 + 镜像内指纹 = manifest（FUP-16
  遗留项闭环）；thread 探针 GREEN；**gate A 982/2skip/1xfail/0、gate B 982/3skip/0、
  macOS 916/65skip/0**，与 F12–F14 收口档逐项相同。gate A 前四档红全部是公共镜像源
  劣化（详见 `tmp/sdd/ab-e2b-report.md`），最终用本地 `registry:2` 源跑绿。
- Task 4 / 5：文档与报告落盘（main HANDOFF / task-backlog #19–#22 / progress /
  两份报告；fork followups / CHANGELOG / test-baseline / e2b-integration / ledger）。
  工作树仅剩既有 untracked `target/`。

### 本波的意外收获（必须继续跟进）

`tmp/f11_fup3_probe.py` 由绿翻红没有被当成「脚本噪音」放过，二分定性为**本波引入的
真实回归**：pure 形态 exec stdio 在「客户端下一个可用 fd = 3」时把子进程 fd 1 接错
（stdout 全丢）。登记为 fork FUP-23 / main task-backlog #22（含复现配方与修法），
CHANGELOG 顶部有升级警示。**三档门禁的绿不排除它**（pytest 进程持有几十个 fd）。
建议：推 worker/测试镜像上线前先修 FUP-23（或临时回退 `7671240`）。
