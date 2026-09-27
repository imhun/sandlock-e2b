# docs/reports —— 从 gitignored 工作笔记里固化下来的证据报告

本目录里的每个 `.md` 都是 `.superpowers/sdd/<同名>.md` 的**逐字节副本**（`cp`，
sha256 对照见 `.superpowers/sdd/artifact-promotion-report.md`）。原件目录
`.superpowers/` 写在 `.gitignore` 里，所以这些报告在本仓库里**从来不存在**：换一台机器、
或清一次工作区之后，`docs/**` 正文里那些"结论见 `.superpowers/sdd/n43-diagnosis.md`"
的句子就断链了 —— 2026-09-27 已经因为纯形态验收表只躺在 gitignored 报告里吃过一次亏。

## 为什么固化

判据可以重跑，但**推导过程**（RED→GREEN 的原文、被排除的假设、当时的实测表）只在报告里。
把报告搬进仓库，是为了让 `docs/**` 的引用在**任何一次干净的 checkout** 里都能落到实物上；
机制、实测表的权威版本仍在正文文档（`docs/*.md`）里，这里的副本只是那批引用的落脚点。

固化是**只读**的：这一份是 2026-09-27 的快照，不改写、不合并；原件继续在
`.superpowers/sdd/` 里作为工作笔记存在（它是 gitignored 的 scratch，随它继续演进）。

## 清单（原件 → 固化位置）

| 原件（gitignored） | 固化后 |
| --- | --- |
| `.superpowers/sdd/checkpoint-e5-e8-audit-report.md` | `docs/reports/checkpoint-e5-e8-audit-report.md` |
| `.superpowers/sdd/debt-fup28-exec-shape-report.md` | `docs/reports/debt-fup28-exec-shape-report.md` |
| `.superpowers/sdd/debt-fup28-retire-the-rewrite-report.md` | `docs/reports/debt-fup28-retire-the-rewrite-report.md` |
| `.superpowers/sdd/debt-fup28-soak-report.md` | `docs/reports/debt-fup28-soak-report.md` |
| `.superpowers/sdd/debt-n40-pool-mcp-report.md` | `docs/reports/debt-n40-pool-mcp-report.md` |
| `.superpowers/sdd/debt-n44-compose-base-image-report.md` | `docs/reports/debt-n44-compose-base-image-report.md` |
| `.superpowers/sdd/debt-n44-env-example-report.md` | `docs/reports/debt-n44-env-example-report.md` |
| `.superpowers/sdd/fix-a-report.md` | `docs/reports/fix-a-report.md` |
| `.superpowers/sdd/fix-b-report.md` | `docs/reports/fix-b-report.md` |
| `.superpowers/sdd/fix-c-report.md` | `docs/reports/fix-c-report.md` |
| `.superpowers/sdd/fix-d-report.md` | `docs/reports/fix-d-report.md` |
| `.superpowers/sdd/n27-identity-residual-report.md` | `docs/reports/n27-identity-residual-report.md` |
| `.superpowers/sdd/n27-task-7-report.md` | `docs/reports/n27-task-7-report.md` |
| `.superpowers/sdd/n30-task-3-report.md` | `docs/reports/n30-task-3-report.md` |
| `.superpowers/sdd/n37-tree-size-report.md` | `docs/reports/n37-tree-size-report.md` |
| `.superpowers/sdd/n39-pool-env-report.md` | `docs/reports/n39-pool-env-report.md` |
| `.superpowers/sdd/n41-residual-report.md` | `docs/reports/n41-residual-report.md` |
| `.superpowers/sdd/n43-diagnosis.md` | `docs/reports/n43-diagnosis.md` |
| `.superpowers/sdd/n43-fix-report.md` | `docs/reports/n43-fix-report.md` |
| `.superpowers/sdd/n45-pid-ns-report.md` | `docs/reports/n45-pid-ns-report.md` |
| `.superpowers/sdd/netns-task-1-report.md` | `docs/reports/netns-task-1-report.md` |
| `.superpowers/sdd/netns-task-3-report.md` | `docs/reports/netns-task-3-report.md` |
| `.superpowers/sdd/o1-t1-fleet-report.md` | `docs/reports/o1-t1-fleet-report.md` |
| `.superpowers/sdd/progress.md` | `docs/reports/progress.md` |
| `.superpowers/sdd/task-A7-report.md` | `docs/reports/task-A7-report.md` |
| `.superpowers/sdd/task-cowprobe-report.md` | `docs/reports/task-cowprobe-report.md` |
| `.superpowers/sdd/task-F1-report.md` | `docs/reports/task-F1-report.md` |
| `.superpowers/sdd/task-f1probe-report.md` | `docs/reports/task-f1probe-report.md` |
| `.superpowers/sdd/task-final-repin-report.md` | `docs/reports/task-final-repin-report.md` |
| `.superpowers/sdd/task-usernsprobe-report.md` | `docs/reports/task-usernsprobe-report.md` |
| `.superpowers/sdd/task-w4-controlplane-report.md` | `docs/reports/task-w4-controlplane-report.md` |
| `.superpowers/sdd/task-Z-report.md` | `docs/reports/task-Z-report.md` |

`progress.md` 是那份工作台账的快照（原件仍在被各轮任务追加）；固化的是"截止 2026-09-27
它长什么样"，不是让仓库里的副本继续滚动。

`fix-{a,b,c,d}-report.md` 是 **C1 wave 3**（把 C1 收尾时记账的遗留项全部做掉）四支并行的
工作流报告，2026-09-27 同日追加；正文引用在 `docs/deploy-clusters.md` §7.3 与计划文件
`docs/superpowers/plans/2026-09-27-priv-broker-externalization.md`。

## 与钉子测试的关系

`tests/unit/test_docs_only_point_at_repo_artifacts.py` 只把**活文档**（`docs/` 下除本目录
以外的一切）纳入"必须指向仓库产物"的钉子。本目录是冻结归档：正文里的 `tmp/...` 提法是
当时的历史叙述，不是"怎么再跑一遍"的指令 —— 把它一起钉住只会让钉子随归档一起膨胀。
这条排除是写进测试里的显式决定（`test_the_frozen_archive_is_not_live_docs`），不是 glob
漏掉了文件。

**判据脚本**（`deploy/scripts/acceptance/**`）的原路径 → 新路径映射表、以及每条"不搬"
的理由，都在 `.superpowers/sdd/artifact-promotion-report.md`。
