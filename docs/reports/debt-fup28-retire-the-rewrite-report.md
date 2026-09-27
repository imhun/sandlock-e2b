# FUP-28 收口：撤掉 E2B 侧的 `..` 相对软链改写（2026-09-27）

**状态：撤了。** 三条前提全部满足（各有证据路径，见 §5），`image_resolver.py` 的
`_root_absolute_links`（连同它唯一的调用点链 `_prepared_rootfs`/`_PREPARED_ROOTFS`）已删除，
两条钉子按"先红后绿"同步调整，两档 lane 在**同一冻结树**上复跑 **0 failed** 且与基线
**相等或更好**（§4）。没有动 fork 的引擎代码，没有重建 wheel。

工作目录 `/Users/polus/project/ai/sandlock-e2b`。本任务开工时 HEAD = `ebee2f1`，验收时
HEAD = **`26739e1`**（开工后**并行 agent** 落了 `858d5d8`/`26bca22`/`26739e1` 三个提交，
见 §4.4 —— 它加了一个测试文件，这件事被我发现并处理掉了，不是悄悄混进数字里）。

---

## 0. 结论摘要

1. **撤掉了什么**（§1）：`envd_service/runtime/image_resolver.py` 的
   `_root_absolute_links()`（改写本体）、`_prepared_rootfs()` + `_PREPARED_ROOTFS` +
   `_PREPARED_GUARD`（它唯一的调用点）、`_confine_at_root()`（只被改写用），以及
   `resolve_image_rootfs()` 里 5 处 `return _prepared_rootfs(image, X)` → `return X`。
   该文件 `git diff --numstat` = **+13 / −128**（净删 115 行；旧文件 546–666 整段消失，
   `_materialize_entry` 由第 667 行上移到第 554 行，逐处清单见 §1/§6）。**没有**动
   `oci_registry._chroot_symlinks`
   —— 那条"绝对目标 → chroot 相对目标"是 `tarfile` 的 `data` filter 与"宿主侧读这棵树不逃逸"
   要求的，跟这道改写方向相反、必须留。
2. **两条钉子**（§2）：
   * `tests/unit/test_oci_registry.py::test_resolve_keeps_symlink_targets_resolving_inside_the_chroot`
     —— 从"整树不许有 `..` 相对软链"改成"**保留 `..`**、仍解析到同一个 inode"。
     RED（改写还在时）断言 `not target.is_absolute()` 红在
     `PosixPath('/etc/ssl/certs/ca-certificates.crt').is_absolute`，撤掉后同一条绿。
   * `tests/unit/test_image_rootfs_links.py` —— **整文件删除**（6 条用例全是那道改写的回归网：
     改写的 inode 等价、越界 clamp、幂等、`_confine_at_root` 的语义）。
3. **本机单测**（§3）：`14 failed / 1522 passed / 11 skipped`，失败名单与那 14 条已知
   Linux-only 红**逐条同名**（排序后 `diff` 为空）。
4. **两档 lane**（§4，冻结树、同一指纹）：gate A `2027 passed / 10 skipped / 3 xfailed`、
   gate B `2020 passed / 17 skipped / 3 xfailed`，**两档 0 failed**。与 Task 13 基线
   （`2022 / 10 / 3`、`2015 / 17 / 3`）比：**+5 passed、skip 与 xfail 逐字不变**，
   且 +5 逐条归因（§4.3）：基线之后落库的 **11 条新用例**（都在跑、全绿）减去本任务
   **撤掉的 6 条钉子用例**。
5. **对应三条前提**（§5）：① = 线上 wheel 里那份 `EAGAIN` 有界重试（manifest HEAD
   `7b60349c`，`sys/fs.rs` 逐字节相同）；② = 两托管宿主内核实测 EAGAIN；③ = 产品路径 soak
   （exec 判据已换成 arm64 上有效的"解释器挂 `..`"形状）+ 变异对照。

---

## 1. 撤掉的东西（逐处，不是"重构")

`envd_service/runtime/image_resolver.py`：

| 位置 | 动作 | 说明 |
|---|---|---|
| `_root_absolute_links()` | **删除**（原 571–646，含 docstring 76 行） | 改写本体：`os.walk` 整树，把带 `..` 的相对软链原子替换成 root-absolute 目标 |
| `_prepared_rootfs()` | **删除** | 它**唯一**的调用点；只做"每进程每 entry 一次"的去重 + 调 `_root_absolute_links` |
| `_PREPARED_ROOTFS` / `_PREPARED_GUARD` | **删除** | 上一条的进程内状态 |
| `_confine_at_root()` | **删除** | 只被改写用来算等价的 root-absolute 目标（`RESOLVE_IN_ROOT` 的 clamp 规则） |
| `resolve_image_rootfs()` 的 5 处 `return _prepared_rootfs(image, X)` | **改成 `return X`** | `cached_local` / `_resolve_local_oci(...)` / `shared_rootfs` / 缓存命中 / 新提取，五条出口 |
| 模块 docstring | **加一段**（+7 行） | 写明"树按镜像原样交付、`..` 留在里面、重试在引擎侧（fork FUP-26）"，免得下一个人以为这里漏了一道工序 |

合起来就是旧文件 **546–666 行整段消失**（`#: Rootfs trees this process has already normalized`
那段注释 + `_prepared_rootfs` + `_root_absolute_links` + `_confine_at_root`），
`_materialize_entry` 从第 667 行上移到第 554 行；模块 docstring 加了 7 行。

`envd_service/runtime/oci_registry.py`：`_chroot_symlinks()` 的 docstring 补一句
"resolver 曾经把这些改回 root-absolute，现在不了（FUP-28）"——**代码一行未动**。

**刻意没动的**：`_chroot_symlinks()` 本身（绝对目标 → chroot 相对目标）。它不是本 bug 的绕行，
是两条硬要求的产物：`tarfile` 的 `data` filter 直接丢绝对链接；原始绝对链接在宿主侧读这棵树时
会逃出 rootfs（见 `tests/unit/test_oci_registry.py` 里那条用例的 docstring）。
撤掉的是"它之后那道反向改写"。

`_root_absolute_links` 的删除还顺带消掉了它的三类失败面（`os.walk` 的 O(rootfs) 走查、
每链一次 `symlink`+`replace`、走不了的链打 warning），这些都不再出现在建箱路径上。

---

## 2. 两条钉子：RED → GREEN（原始输出）

顺序按 TDD 要求：**先把断言改成"撤掉之后应该成立的样子"，跑出红**（证明钉子确实钉在被撤的
那个东西上），**再撤代码**，同一条绿。

### 2.1 `tests/unit/test_oci_registry.py::test_resolve_keeps_symlink_targets_resolving_inside_the_chroot`

改后的断言（新）：链接目标**不是**绝对路径、**含 `..`**、按 `RESOLVE_IN_ROOT` 的规则
（`..` 在根上 clamp）解析到同一个 inode；再对整棵树逐链检查
`{usr/lib/ssl/cert.pem, usr/lib/ssl/certs}` 恰好是全部软链、都不绝对、都落到镜像本意的 inode。

**RED**（改写还没撤，`tmp/k0s/fup28-retire/red-nail.log`）：

```
        cert_link = rootfs / "usr/lib/ssl/cert.pem"
        assert cert_link.is_symlink()
        target = cert_link.readlink()
>       assert not target.is_absolute()
E       AssertionError: assert not True
E        +  where True = is_absolute()
E        +    where is_absolute = PosixPath('/etc/ssl/certs/ca-certificates.crt').is_absolute

tests/unit/test_oci_registry.py:357: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_oci_registry.py::test_resolve_keeps_symlink_targets_resolving_inside_the_chroot
1 failed in 1.36s
```

红出来的正是那道改写的产物（`/etc/ssl/certs/ca-certificates.crt`）⇒ 这条断言确实钉在被撤的行上。

**GREEN**（撤掉 `_root_absolute_links` + 调用点之后，`tmp/k0s/fup28-retire/green-nail.log`）：

```
tmp/testenv/bin/python -m pytest "tests/unit/test_oci_registry.py::test_resolve_keeps_symlink_targets_resolving_inside_the_chroot" -q
.                                                                        [100%]
1 passed in 0.59s
```

（该文件整体：`22 passed`。）

### 2.2 `tests/unit/test_image_rootfs_links.py` — 删除

6 条用例（`test_dotdot_link_is_rewritten_and_resolves_to_the_same_inode`、
`test_a_relative_link_without_dotdot_is_left_alone`、`test_an_absolute_link_is_left_alone`、
`test_a_dotdot_that_would_escape_the_root_clamps_like_resolve_in_root`、
`test_the_rewrite_is_idempotent`、`test_confine_at_root_matches_resolve_in_root_semantics`）
全部以 `ir._prepared_rootfs(...)` / `ir._confine_at_root(...)` 为被测面 —— 那些符号撤掉之后
不复存在，而这个文件存在的**唯一**理由就是钉住那道改写（FUP-28 原文点名了它）。
其中仍有价值的语义（"chroot 视图里解析到同一个 inode"）已按 §2.1 挪进
`test_oci_registry.py` 的端到端用例（走的是真 `resolve_image_rootfs`，比原来直接调私有
helper 更接近产品路径）。

---

## 3. 本机单测（判据：失败名单逐条同名）

```
$ tmp/testenv/bin/python -m pytest tests/unit -q -p no:cacheprovider
14 failed, 1522 passed, 11 skipped, 2 warnings in 112.02s
```

失败名单与 `tmp/k0s/task13/unit-failed-names-frozen.txt`（14 条已知 Linux-only 红）比对：

```
$ diff <(sort frozen) <(sort 本次)
IDENTICAL (14/14)
```

一条**无关的 flake** 如实记下来：同一天我用 `-rA` 跑同一条命令时多出过 1 条红
（`tests/unit/test_gateway.py::test_retry_replays_after_connection_failure`，网络时序），
重跑即绿（`tmp/k0s/fup28-retire/unit-ra.log` vs `unit-full.log`）。它与本改动无关
（本改动只碰镜像解析与两条 OCI/rootfs 用例），但按"不放宽、不隐藏"的规矩写在这里。

---

## 4. 两档 lane：逐档数字、基线对照、归因

命令就是简报指定的两条（共享 `:latest` 刚重烤、与 `wheels/fork` 同源 ⇒ 直接用默认镜像）：

```sh
sh tmp/k0s/gateA-full.sh tmp/k0s/fup28-retire/gateA-current.log   # E2B_BASE_IMAGE=python-mcp:3.14
sh tmp/k0s/gateB-full.sh tmp/k0s/fup28-retire/gateB-current.log   # E2B_BASE_IMAGE=（空，pure 形态）
```

### 4.1 逐档结果（末行逐字）

| 档 | 结果（末行逐字） | 基线（`pure-task-13-report.md` §1，revision `1374e87`） | 判定 |
|---|---|---|---|
| **gate A**（镜像形态，全量） | `2027 passed, 10 skipped, 3 xfailed, 13051 warnings in 490.64s (0:08:10)` | `2022 passed, 10 skipped, 3 xfailed` | **0 failed；+5 passed**，skip/xfail 逐字不变 ⇒ **相等或更好** |
| **gate B**（pure / `E2B_BASE_IMAGE=`） | `2020 passed, 17 skipped, 3 xfailed, 12153 warnings in 455.02s (0:07:35)` | `2015 passed, 17 skipped, 3 xfailed` | **0 failed；+5 passed**，skip/xfail 逐字不变 ⇒ **相等或更好** |

两档日志里 `^FAILED` 命中数都是 **0**（`grep -c`），退出码都是 0。

### 4.2 冻结树的指纹（两档之间没有任何东西动过）

跑第一档**之前**与跑完第二档**之后**各取一次指纹（HEAD + `git status` + `git diff HEAD` 的
sha256 + `envd_service`/`tests` 全部 `.py` 的 sha256 汇总）：

```
$ diff tmp/k0s/fup28-retire/fingerprint-before.txt tmp/k0s/fup28-retire/fingerprint-after.txt
11c11
< 1714bf26…  /dev/fd/12        # 同一个 hash，只是进程替换的文件名不同
---
> 1714bf26…  -
```

两档之间无差异 ⇒ 这两组数字是**同一棵树**上的，可以直接互相对照（`gateA-current.log`、
`gateB-current.log`）。

### 4.3 +5 是怎么来的（逐条归因，不是回归）

collect（同一套 `--ignore`）与通过数在**同一个** docker 配置下量的：

| 项 | 数字 | 出处 |
|---|---|---|
| 基线 collect（`1374e87`） | 2035 | Task 13 报告（= 它的 `2022+10+3`） |
| + 基线之后落库的新用例 | **+11** = `test_checkpoint_acceptance_pod_politeness.py` 6 条（新文件）+ `test_autoscaler_local_backend_shape.py` 1 条（`git diff 1374e87 HEAD` 里唯一新增的 `def test_`，该文件现在共 11 条）+ `test_compose_base_image_shape.py` 4 条（并行 agent `858d5d8`，见 §4.4） | `git diff --name-status 1374e87 HEAD -- tests/` + 逐文件 `--collect-only` |
| − 本任务撤掉的钉子用例 | **−6**（`tests/unit/test_image_rootfs_links.py`，整文件） | `git show HEAD:tests/unit/test_image_rootfs_links.py \| grep -c '^def test_'` = 6 |
| = 本轮 collect | **2040**（gate A 配置与 gate B 配置各量一次，都是 2040） | 容器内 `pytest tests --perf --collect-only -q …` |
| gate A | 2040 − 10 skipped − 3 xfailed = **2027 passed** ✓ 与末行逐字相符 | |
| gate B | 2040 − 17 skipped − 3 xfailed = **2020 passed** ✓ 与末行逐字相符 | |

等价地，"只在同一棵树上比较"：**没有**本改动时两档分别是 `2033`（2046−13）与 `2026`（2046−20），
本改动正好各 **−6**（删掉的那 6 条钉子用例）⇒ **0 failed、差别就是"删掉的钉子"**，
没有一条用例从绿变红。

### 4.4 并行 agent 的窗口：发现与处置（写清楚，不留暗账）

第一次跑的那一对是 `2023 / 10 / 3`（gate A）与 `2018 / 17 / 3`（gate B）。跑完做归因时发现
collect 对不上（2040 vs 运行时总项数 2036 / 2038），逐项查下来是**并行 agent 的工作树在跑动
窗口里变化**：`858d5d8`（08:19）新增了 `tests/unit/test_compose_base_image_shape.py`，而
gate A（08:06–08:14）跑的是它出现之前的树、gate B（08:14–08:22）恰好收进了它当时的一半
（2 条，之后该文件在 08:23 被改成 4 条，`26739e1` 08:29 提交）。

处置：**两档在同一棵冻结树（`26739e1` + 本改动）上重跑**（§4.1 是重跑的数字），旧的那两份
留档为 `gateA-run1-earlytree.log` / `gateB-run1-earlytree.log`，并在它们之后额外跑了一次
`-rA` 的 gate A 配置（`gateA-run2-ra-currenttree.log`，**`2027 passed, 10 skipped, 3 xfailed`，
0 error**）—— 与 §4.1 的 gate A 数字**独立复现**一致。

---

## 5. 与 FUP-28 三条前提的对应（各自的证据路径）

| 前提 | 判据 | 证据（可在磁盘上回看） |
|---|---|---|
| **① fork 侧 `EAGAIN` 有界重试已随 wheel 上线** | 线上 wheel manifest HEAD = `7b60349c`（含 FUP-26），且 soak 打的 `crates/sandlock-core/src/sys/fs.rs` 与该 revision 逐字节相同、`EAGAIN_RETRY_BUDGET: u32 = 4` | `.superpowers/sdd/debt-fup28-soak-report.md` §4（`git diff 7b60349c HEAD -- crates/sandlock-core/src/sys/fs.rs` 为空 + `git merge-base --is-ancestor 0164575 7b60349c`） |
| **② 目标平台宿主内核确实会返回 EAGAIN** | `.94` **8107/40000**、`.140` **6855/40000**（开发容器对照 730/20000），两宿主 `6.12.0-211.34.1.el10_2.aarch64` | fork `docs/fork-plan-followups.md` FUP-28 的前提②段（本次已就地更正 exec 判据形状）；探针 `tmp/k0s/probe_openat2_eagain.py` |
| **③ 产品路径 soak（带变异对照）** | 受管 open **0 / 97482**、`..`-interp exec **0 / 300**、同一次运行内核原始 EAGAIN **3411 / 40000（.94）、3125 / 40000（.140）> 0**；变异 `EAGAIN_RETRY_BUDGET 4→0` ⇒ exec **122 / 300、223 / 300** 红（同一批运行里旧形状的 `exec /bin/echo` 仍 0/300），改回来两宿主回绿 | `.superpowers/sdd/debt-fup28-exec-shape-report.md` §3–§5（前一轮 `.superpowers/sdd/debt-fup28-soak-report.md`） |

**本任务自己的回归网**（撤掉之后的判据）：gate A **2027 / 10 / 3 / 0 failed**、
gate B **2020 / 17 / 3 / 0 failed**（§4），外加 2026-09-27 一整天部署宿主上的 soak 与变异对照
（③）——即"撤掉改写之后，产品路径上没有出现新的 127 + 空 stderr、也没有受管 open 失败"。

登记处的同步（摘掉"保留该改写"的理由、写明①②③的证据路径）：

* 本仓 `docs/open-issues.md` 的 FUP-28 行（状态改成 **✅ 已撤**，三段证据路径 + 撤回明细）；
* 本仓 `docs/HANDOFF.md` 的"还剩什么"表里那一行（改成 **✅ 已撤**，不再需要拍板）；
* 本仓 `docs/task-backlog.md` 里"本轮保留改写"那句（追加 2026-09-27 的更正指针）；
* **fork** `docs/fork-plan-followups.md` 的 FUP-28 条：标题标"已撤，2026-09-27"、前提③就地
  更正为 arm64 上的有效形状、结尾那段"在那之前保留改写（代价只是……收益不足以承担风险）"
  换成"已撤"的完整记录（①②③逐条证据 + 撤了哪些行 + 两条钉子怎么改 + 两档 lane）。

---

## 6. 文件清单与提交

**代码 / 测试 / 文档（本仓）**

`git diff --numstat`（本仓，本任务的文件）：

```
+13  -128  envd_service/runtime/image_resolver.py    (删改写 + 调用点；docstring +7 行)
+5   -0    envd_service/runtime/oci_registry.py      (docstring 一句：反向改写不再做)
+53  -31   tests/unit/test_oci_registry.py           (钉子改成"保留 ..、同 inode")
+0   -131  tests/unit/test_image_rootfs_links.py     (整文件删除，6 条用例)
+1   -1    docs/open-issues.md                       (FUP-28 行)
+1   -1    docs/HANDOFF.md                           ("还剩什么"表)
+3   -1    docs/task-backlog.md                      (2026-09-27 更正指针)
 0    0    third_party/sandlock                       (子模块指针：fork 文档那条提交)
```

**fork 仓**（`third_party/sandlock`，分支 `upstream-pr/netns-free-clean`）：
`docs/fork-plan-followups.md` 的 FUP-28 条（+29/−4）。

**报告**：本文件（`.superpowers/` 按惯例 gitignored，不提交）。

**提交**（都按 pathspec 加、提交前 `git diff --cached --name-only` 核对过只有自己的文件）：

| 仓库 | 提交 | 内容 |
|---|---|---|
| 本仓 | `d42d563` | `fix(image): retire the `..` symlink rewrite -- the engine retries that walk`（上表的 7 个文件） |
| 本仓 | `1549255` | `chore(fork): bump the sandlock submodule for FUP-28's closure`（只动 gitlink：`290761e..c4d18c0`） |
| fork（`third_party/sandlock`） | `c4d18c0` | `docs(fup28): the rewrite is retired -- three premises, two nails, both lanes` |

**日志与原始输出**（`tmp/k0s/fup28-retire/`，gitignored，留在本机）：

| 路径 | 内容 |
|---|---|
| `red-nail.log` / `green-nail.log` | 钉子的 RED / GREEN 原始输出（§2.1） |
| `unit-full.log` / `unit-failed-names.txt` / `unit-failed-names-frozen-sorted.txt` | 本机单测 + 14 条名单比对（§3） |
| `unit-ra.log` | `-rA` 那次（多出的那条 gateway flake，§3） |
| `gateA-current.log` / `gateB-current.log` | **验收用的两档**（§4.1） |
| `fingerprint-before.txt` / `fingerprint-after.txt` | 两档之间的冻结树指纹（§4.2） |
| `gateA-run1-earlytree.log` / `gateB-run1-earlytree.log` | 第一次那对（跑在并行 agent 的窗口里，§4.4） |
| `gateA-run2-ra-currenttree.log` | 冻结树上的 `-rA` 复核（`2027/10/3`，§4.4） |
| `collect-all-ids.txt` / `unit-collected-ids.txt` / `unit-ran-ids.txt` | 归因时用的 collect / 运行项清单（§4.3） |

---

## 7. 担忧 / 局限（按重要性）

1. **"沙箱看到的文件系统与镜像不同"这件事，从今天起才真的没有了 —— 但平台侧还有别的
   改写面**：`_chroot_symlinks` 仍然把绝对目标写成相对目标（必需），所以"沙箱里的链接文本
   与镜像层里的文本不同"这件事**依然存在**。FUP-28 撤掉的是"相对 → 绝对"那半，别把
   "树已经与镜像逐字节相同"当成结论。
2. **热缓存里的老 entry 是"改写过的形态"**。节点上 `E2B_IMAGE_CACHE_DIR` 里已经存在的
   rootfs 是旧 resolver 交付的（链接是 root-absolute），新提取的 entry 会是 `..` 形态。
   两种形态在 chroot 里语义相同（一个根本不需要 `..` walk、一个靠重试），所以**不修是对的**；
   代价是"缓存里形态不统一"这一点会持续到下次重烤/清缓存，读代码的人要知道。
3. **arm64 上暴露面很窄但非零**：soak 报告 §0 第 5 条量过，arm64 镜像里有 9 条 `..` 相对软链，
   其中 `/etc/os-release -> ../usr/lib/os-release` 是沙箱里真会读到的（`python3` 探
   `platform`、apt、发行版嗅探）——撤掉之后它走的就是"重试"这条路（绿 0/97482、变异
   2600/97482）。也就是说：这条 bug 的彻底消失靠的是 ①（重试），不是"镜像里没有 `..`"。
4. **lane 只覆盖单机形态**：两档 lane 跑在本机 orbstack（x86_64）的 docker 里，走的仍是
   `E2B_BASE_IMAGE=python-mcp:3.14` / pure 两形态；**部署宿主（arm64 k0s）上没有跑 lane**
   ——那里只有 ③ 的 soak（同样是产品代码，但走的是 `SandboxInstance` 接线，不是 envd →
   route B → wheel 那条链）。这与 soak 报告 §6 第 2 条的局限是同一条：**接线不在回路里**。
   按任务约束本轮**没有碰 k0s 集群**。
5. **并行 agent 的窗口**（§4.4）：验收数字是重跑后的干净数字，但同一工作树里还有别的 agent
   在提交（`858d5d8`/`26bca22`/`26739e1`）。若在别的时间点复核，collect 数会因为这些提交
   而变化 —— 判据应当是"0 failed + 与基线相等或更好 + 差额能逐条归因"，而不是"某个绝对数字"。
6. **`docs/` 里还有别的"带日期的旧口径"**：我只改了 FUP-28 相关的四处（本仓三处 +
   fork 一处）。`docs/HANDOFF.md` 顶部那张"本轮的验收数字"表仍是 N15 当天的 `1772/1765`
   （Task 13 已把唯一权威表落在 `docs/pure-shape-decision.md` §7，那张表不在本任务范围）。
