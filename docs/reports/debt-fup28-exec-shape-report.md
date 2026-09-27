# FUP-28 前提③：把 exec 判据换成有效形状后的重跑（2026-09-27）

**状态：跑完了。两宿主（`172.18.80.94` / `172.18.80.140`）上，换过形状的 exec 判据
绿，同一份源码的变异（`EAGAIN_RETRY_BUDGET 4 → 0`）红，就地改回来又绿。**
按任务约束，`envd_service/runtime/image_resolver.py::_root_absolute_links` 的改写
**一行没动**（`git diff -- envd_service/runtime/image_resolver.py` 为空）—— 本任务只产出判据。

上一份报告（`.superpowers/sdd/debt-fup28-soak-report.md`，`020ff58`）已经指出：arm64 镜像里
loader 路径**不含 `..`**，所以它那条「300 条 `exec /bin/echo` 0 个 127 + 空 stderr」在 arm64 上
**恒真**。本任务做的是：把这条判据换成真正会走 `..` 的形状、在**两台部署宿主上重跑**、
并给出 **红 → 绿** 的变异对照与两条形状对照。**判据换形状 = 本文件 §5 的那三条**。

---

## 0. 结论摘要

1. **arm64 上原来那条判据为什么是空的（本轮实测，不是引用）**：镜像里
   `/bin/echo`、`/usr/local/bin/python3.14`、`/usr/bin/dotdot` 之外的任何动态链接程序，
   其 `PT_INTERP` 都是 **`/lib/ld-linux-aarch64.so.1`**，而 `/lib -> usr/lib`、
   `/lib/ld-linux-aarch64.so.1 -> aarch64-linux-gnu/ld-linux-aarch64.so.1`
   —— **整条链里一个 `..` 分量都没有**（§2 的原始输出）。x86 镜像之所以有这条 bug，
   靠的是 `/lib64/ld-linux-x86-64.so.2 -> ../lib/x86_64-linux-gnu/…` 那个 `..`。
2. **有效形状**：在 arm64 上把 x86 的形状**等价复现** ——
   `lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1`，再用
   `gcc -Wl,--dynamic-linker=/lib64/ld-linux-aarch64.so.1` 编一个打印 `ok` 的
   `/usr/bin/dotdot`。竞态加在 `<rootfs>/lib64`（**dotdot 自身文件在 `/usr/bin`，
   唯一经过 `/lib64` 的路径就是它的 `PT_INTERP`**）。
3. **形状自证（裸内核，无中介）**：同一台机器、同一条链 —— 把链翻成 root-absolute
   （**这正是 E2B 那道改写做的事**）之后，`openat2(RESOLVE_IN_ROOT)` 的 `EAGAIN`
   **从 566/40000、776/40000 掉到 0/40000**；而 dotdot 自身文件那条路径（无 `..`）
   本来就 0/40000（§2）。
4. **两宿主原始输出**（§3）：绿配置下 受管 open **0 / 97482**、
   `exec /bin/echo` **0 / 300**、`..`-interp exec **0 / 300**、裸机 `EAGAIN`
   **3411 / 40000（.94）**、**3125 / 40000（.140）**。
5. **变异对照红 → 绿（§4）**：同一份部署源码只改一个常量（4 → 0）——
   `..`-interp exec **122/300（.94）**、**223/300（.140）** 红（签名 `Code(127)` +
   `sandlock-init: exec "/usr/bin/dotdot" failed (errno 11)`）；
   **同一批运行里 `exec /bin/echo` 仍然是 0/300** —— 这就是"旧判据恒真"的直接证据。
   改回来（`fs.rs` sha256 回到 `c5218362…`）重编，**两个宿主都回到 0/300 绿**；
   `.94` 上还做了一次**就地往返**（改 0 → 149/300 红、改回 4 → 0/300 绿），
   且改回来编出的二进制与验收二进制**逐字节相同**（`55beea3a…`）。
6. **三条判据逐条结论见 §5；节点清理见 §6（两宿主 `/opt/fup28` 已删并复核 ABSENT）。**

---

## 1. 欠账、范围与做法

欠账原话（`debt-fup28-soak-report.md` §6 第 3 条）：

> arm64 的镜像 loader 路径没有 `..` ⇒ FUP-28 那条「exec /bin/echo」判据在 arm64 上是
> **恒真的空检查**（需改成"解释器挂 `..`"形状）。

FUP-28 的判据原文（fork `docs/fork-plan-followups.md` 前提③ + `docs/open-issues.md` FUP-28 行）：

> 受管 open 连续 97482 次必须 0 失败、300 条 `exec /bin/echo` 必须 0 次「127 + 空 stderr」，
> 同时内核侧原始 `EAGAIN` 必须 > 0。

本轮把中间那条**换成**：

> 300 条 **`exec <PT_INTERP 挂着一个 `..` 相对软链的二进制>`** 必须 0 次「127 + 空 stderr」
> （arm64 上就是 `lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1` + `dotdot`）。

**"被验的代码 = 线上代码"的钉子**：本轮 soak 二进制编自 fork **`d750fa1`**
（= 部署镜像 tag `0.1.0-597-g3701a53-20260926-163057` 的父提交 `3701a53` 所钉的
submodule 指针，`git ls-tree 3701a53 third_party/sandlock`），
`crates/sandlock-core/src/sys/fs.rs` sha256 = `c5218362…`，第 44 行
`const EAGAIN_RETRY_BUDGET: u32 = 4;`。
该 revision 与当前 fork HEAD `290761e` 在**被测文件上逐字节相同**
（`git diff --stat d750fa1 290761e -- crates/sandlock-core/src/sys/fs.rs crates/sandlock-core/src/chroot/ crates/sandlock-core/src/init/mod.rs` 为空）。

**harness**：`tmp/k0s/fup28/soak/f26_soak.rs`（临时目标，不进版本库），
sha256 `2ec3e053…`；三段 `a`/`b`/`c` 由 `FUP28_PHASES` 选择，
起一个 mainless、exec-only 的 `SandboxInstance`（`chroot=<rootfs 副本>` + `/workspace`、
`/home/user` 挂载），4 条线程在 `<rootfs>/lib64` 里来回 rename scratch 条目放大竞态。

**节点侧脚本（本轮新写，`tmp/k0s/fup28/node2/`）**：`stage2.sh`（铺台 + 造形状 + 钉子）、
`build-bins2.sh`（绿/变异两个二进制）、`run2.sh`（跑一次并同时起裸内核探针）、
`shape2.sh`（形状自证）、`inplace2.sh`（就地变异往返）、`cleanup.sh`（清理）。

---

## 2. 有效形状是怎么造的 + 形状自证（裸内核，两宿主）

**造法**（`stage2.sh`，两宿主输出一致）：

```
== the arm64 image's own interpreter path (no `..` anywhere): ==
   /lib -> usr/lib
   /lib/ld-linux-aarch64.so.1 -> aarch64-linux-gnu/ld-linux-aarch64.so.1
   resolves to: /opt/fup28/rootfs/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1
== constructing the x86 shape on arm64: lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1 ==
dotdot_link_count=10
== the link under test: ../lib/ld-linux-aarch64.so.1 -> /opt/fup28/rootfs/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1
== loader sha256: 1d8b77f28b7cec0329dca107a28fb0a850193b1dcce59be68fa0b06b514548cc
DOTDOT PT_INTERP = /lib64/ld-linux-aarch64.so.1
IMAGE python3.14 PT_INTERP = /lib/ld-linux-aarch64.so.1
```

外加一条（本轮补测）：**`IMAGE /bin/echo PT_INTERP = /lib/ld-linux-aarch64.so.1`**
—— 这就是"`exec /bin/echo` 在 arm64 上永远不经过 `..`"的直接原因。

`dotdot_link_count=10` = 镜像本来有的 9 条 `..` 相对软链（被解析器改写/裁剪，这里按
FUP-28 的说法取未改写形态还原）+ 新造的 `lib64/ld-linux-aarch64.so.1`。
`loader sha256 = 1d8b77f2…` 与上一份报告逐字节相同 ⇒ 同一份镜像根。

**形状自证**（`shape2.sh`，`raw_openat2.py` 走裸 `openat2(RESOLVE_IN_ROOT)`，40000 次，无中介）：

`.94`：

```
== host=iZuf697v12g31dyz4uvsjlZ arch=aarch64 kernel=6.12.0-211.34.1.el10_2.aarch64
== raw openat2, race in /usr/bin, path has no `..` (dotdot's own file) ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/usr/bin/dotdot n=40000 racer_rounds=15643 hist={'OK': 40000}
RAWOPENAT2 eagain=0
== raw openat2, race in /lib64, path is the `..`-interp link ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=25146 hist={'OK': 39434, 'EAGAIN': 566}
RAWOPENAT2 eagain=566
== same path with the link flipped to the root-absolute (rewritten) form ==
   now: /lib/ld-linux-aarch64.so.1
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=26211 hist={'OK': 40000}
RAWOPENAT2 eagain=0
== restored to the pristine form: ../lib/ld-linux-aarch64.so.1
```

`.140`：

```
== host=iZuf6d1usviqv6x9qk1hpcZ arch=aarch64 kernel=6.12.0-211.34.1.el10_2.aarch64
== raw openat2, race in /usr/bin, path has no `..` (dotdot's own file) ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/usr/bin/dotdot n=40000 racer_rounds=13120 hist={'OK': 40000}
RAWOPENAT2 eagain=0
== raw openat2, race in /lib64, path is the `..`-interp link ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=21989 hist={'OK': 39224, 'EAGAIN': 776}
RAWOPENAT2 eagain=776
== same path with the link flipped to the root-absolute (rewritten) form ==
   now: /lib/ld-linux-aarch64.so.1
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=26391 hist={'OK': 40000}
RAWOPENAT2 eagain=0
== restored to the pristine form: ../lib/ld-linux-aarch64.so.1
```

读法：**该形状确实会被内核拒**（566 / 776 次 `EAGAIN`，只来自那条 `..` 链）；
**E2B 那道改写确实能挡住它**（同一条链、同一次竞态、同 40000 次 ⇒ 0）。
dotdot 自身文件那条路径（无 `..`）本来就 0 ⇒ 竞态加在 `/lib64` 时，能红出来的只可能是 `PT_INTERP` 那次 open。

---

## 3. 两宿主原始输出（验收配置：绿）

**`.94`**（`tmp/k0s/fup28/logs2/green-abc-94.log`，命令 `sh /opt/fup28/run2.sh /opt/fup28/f26_soak.bin abc`）：

```
== host=iZuf697v12g31dyz4uvsjlZ kernel=6.12.0-211.34.1.el10_2.aarch64 arch=aarch64
== bin=f26_soak.bin sha256=55beea3a33d96b9620a73b8a67ce8708fa4f8ad10f7e15d937219b4c2720704b phases=abc
== lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1
== loader sha256=1d8b77f28b7cec0329dca107a28fb0a850193b1dcce59be68fa0b06b514548cc

running 1 test
test fup28_product_path_soak ... FUP28 phases = abc
FUP28 race dir = /opt/fup28/rootfs/lib64
FUP28 phase-a status=Code(0)
FUP28 phase-a stdout="OPENLOOP n=97482 fails=0 hist=[]\n"
FUP28 phase-a stderr=""
FUP28 phase-b execs=300 bad=0 first=None
FUP28 phase-c execs=300 bad=0 first=None
FUP28 race rounds = 1437937
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=0
ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 17.06s

== kernel-side raw probe ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=4190 hist={'OK': 36589, 'EAGAIN': 3411}
RAWOPENAT2 eagain=3411
SOAK_EXIT=0
```

**`.140`**（`logs2/green-abc-140.log`，**同一份二进制**，经跳板机从 `.94` 中继，两端 sha256 一致）：

```
== host=iZuf6d1usviqv6x9qk1hpcZ kernel=6.12.0-211.34.1.el10_2.aarch64 arch=aarch64
== bin=f26_soak.bin sha256=55beea3a33d96b9620a73b8a67ce8708fa4f8ad10f7e15d937219b4c2720704b phases=abc
== lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1
== loader sha256=1d8b77f28b7cec0329dca107a28fb0a850193b1dcce59be68fa0b06b514548cc

running 1 test
test fup28_product_path_soak ... FUP28 phases = abc
FUP28 race dir = /opt/fup28/rootfs/lib64
FUP28 phase-a status=Code(0)
FUP28 phase-a stdout="OPENLOOP n=97482 fails=0 hist=[]\n"
FUP28 phase-a stderr=""
FUP28 phase-b execs=300 bad=0 first=None
FUP28 phase-c execs=300 bad=0 first=None
FUP28 race rounds = 1292216
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=0
ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 19.43s

== kernel-side raw probe ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=4137 hist={'OK': 36875, 'EAGAIN': 3125}
RAWOPENAT2 eagain=3125
SOAK_EXIT=0
```

两个二进制（都在 `.94` 编、`strip` + gzip，两端 sha256 逐字节一致）：

| 二进制 | 内容 | `.gz` sha256 | `gunzip` 后 sha256 |
|---|---|---|---|
| `f26_soak.bin` | 部署源码原样（预算 4） | `9a304898…` | `55beea3a33d96b9620a73b8a67ce8708fa4f8ad10f7e15d937219b4c2720704b` |
| `f26_soak_mutant.bin` | 只把预算改成 0 | `31dbc9b9…` | `28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90` |

（绿二进制 sha256 与上一份报告 §2 表里的 `55beea3a…` **完全相同** ⇒ 两次 session 编的是同一份代码。）

---

## 4. 变异对照：改 0 红 → 改回来绿

**改动内容**：`crates/sandlock-core/src/sys/fs.rs:44`
`const EAGAIN_RETRY_BUDGET: u32 = 4;` → `= 0;`（与 FUP-26/FUP-28 报告同一条变异）。
同一份 rootfs 副本、同一次竞态、同一台机器。

### 4.1 `.94` 变异（`logs2/mutant-bc-94.log`）

```
== bin=f26_soak_mutant.bin sha256=28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90 phases=bc
test fup28_product_path_soak ... FUP28 phases = bc
FUP28 phase-a SKIPPED (not in FUP28_PHASES=bc)
FUP28 phase-b execs=300 bad=0 first=None
FUP28 phase-c execs=300 bad=122 first=Some((Code(127), "", "sandlock-init: exec \"/usr/bin/dotdot\" failed (errno 11)\n"))
FUP28 race rounds = 355028
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=122

assertion `left == right` failed: no `..`-interp exec may come back 127 + empty stderr
  left: 122
 right: 0
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 4.42s

== kernel-side raw probe ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=4957 hist={'OK': 36522, 'EAGAIN': 3478}
RAWOPENAT2 eagain=3478
```

### 4.2 `.140` 变异（`logs2/mutant-bc-140.log`）

```
== bin=f26_soak_mutant.bin sha256=28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90 phases=bc
FUP28 phase-b execs=300 bad=0 first=None
FUP28 phase-c execs=300 bad=223 first=Some((Code(127), "", "sandlock-init: exec \"/usr/bin/dotdot\" failed (errno 11)\n"))
FUP28 race rounds = 367095
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=223

assertion `left == right` failed: no `..`-interp exec may come back 127 + empty stderr
  left: 223
 right: 0
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 5.69s

== kernel-side raw probe ==
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=2898 hist={'OK': 36931, 'EAGAIN': 3069}
RAWOPENAT2 eagain=3069
```

**同上一次运行里 `exec /bin/echo` 仍是 `bad=0`（300/300 全绿）** —— 旧判据在 arm64 上
无论预算 4 还是 0 都是 0/300，这就是"恒真的空检查"的直接证据（两次变异运行、两宿主各一条）。

### 4.3 「改回来就绿」：就地往返（`.94`，`logs2/inplace-94.log`）

```
== step 1: mutate the deployed source in place: EAGAIN_RETRY_BUDGET 4 -> 0 ==
44:const EAGAIN_RETRY_BUDGET: u32 = 0;
== bin=f26_soak_inplace.bin sha256=28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90 phases=c
FUP28 phase-c execs=300 bad=149 first=Some((Code(127), "", "sandlock-init: exec \"/usr/bin/dotdot\" failed (errno 11)\n"))
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=149
  left: 149
 right: 0
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.69s
RAWOPENAT2 ... hist={'OK': 35827, 'EAGAIN': 4173}
SOAK_EXIT=101

== step 2: put the deployed source back: 0 -> 4 ==
44:const EAGAIN_RETRY_BUDGET: u32 = 4;
fs.rs restored sha256: c521836204761691c2d896cc7f847ae6eccbcc24aa91e7915a753c09b971d15a
== bin=f26_soak_restored.bin sha256=55beea3a33d96b9620a73b8a67ce8708fa4f8ad10f7e15d937219b4c2720704b phases=c
FUP28 phase-c execs=300 bad=0 first=None
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=0
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 2.06s
RAWOPENAT2 ... hist={'OK': 36420, 'EAGAIN': 3580}
SOAK_EXIT=0
```

即：**同一份源码、同一台机器，改 0 立刻红（149/300）、改回 4 立刻绿（0/300）**，
且改回来编出的二进制与验收二进制 sha256 相同（`55beea3a…`）。
（"改回来要 `touch`，否则 cargo 会复用变异对象"这条坑，上一次已经踩过并写在这里复现：
`inplace2.sh` 在 `cp -f fs.rs.orig` 之后显式 `touch`。）

### 4.4 反向对照：把那条链翻成 E2B 改写的形状，变异也变绿

如果红是"环境噪声"或"二进制本身有毛病"，那么**只**把 `lib64/ld-linux-aarch64.so.1`
的目标换成 root-absolute（`../lib/ld-linux-aarch64.so.1` → `/lib/ld-linux-aarch64.so.1`，
即 `_root_absolute_links` 干的事），变异应当立刻不红：

`.94`（`logs2/mutant-rewritten-94.log`）：

```
link now: /lib/ld-linux-aarch64.so.1
== bin=f26_soak_mutant.bin sha256=28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90 phases=c
FUP28 phase-c execs=300 bad=0 first=None
FUP28_RESULT opens=97482 opens_fail=0 exec_bad=0 dotdot_bad=0
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 2.21s
RAWOPENAT2 root=/opt/fup28/rootfs path=/lib64/ld-linux-aarch64.so.1 n=40000 racer_rounds=5240 hist={'OK': 40000}
SOAK_EXIT=0
link restored: ../lib/ld-linux-aarch64.so.1
```

`.140`（`logs2/mutant-rewritten-140.log`）：同样的形状，`bad=0`、`RAWOPENAT2 eagain=0`、`SOAK_EXIT=0`。

⇒ 红的来源被钉死在"那条 `..` 链"上：**只有它挂着 `..` 时才红，改成 root-absolute 就绿**，
而这正是 E2B 那道改写存在的理由。

---

## 5. FUP-28 三条判据逐条结论（判据的**新形状**就是第二条）

| # | 判据（本轮口径） | `.94` | `.140` | 结论 |
|---|---|---|---|---|
| ① | 受管 open 连续 **97482** 次 **0 失败** | **0 / 97482**（`hist=[]`） | **0 / 97482**（`hist=[]`） | **满足（本轮复测）** |
| ② | **`exec <PT_INTERP 挂着 `..` 相对软链的二进制>` 300 次 0 个「127 + 空 stderr」**（arm64 上 = `dotdot` + `lib64/ld-linux-aarch64.so.1 -> ../lib/…`） | **0 / 300** | **0 / 300** | **满足（新形状）** |
| ③ | 同一次运行里内核侧原始 **EAGAIN > 0** | **3411 / 40000**（绿）、3478 / 4173（变异/就地） | **3125 / 40000**（绿）、3069（变异） | **满足** |

逐条说明：

* **① 受管 open**：本轮在验收配置里重测，两宿主都是 `OPENLOOP n=97482 fails=0 hist=[]`
  （`n=97482` 逐次核对过，不是"没跑"）。它自己的变异对照（预算 0 ⇒
  `fails=2616 hist=[(11,2616)]` / `fails=2015 hist=[(11,2015)]`，errno **只有** 11 = EAGAIN）
  是**上一份报告**跑的那一对，本轮**没有**重跑 ① 的变异（本轮变异跑的是 `phases=bc`）；
  原始输出在 `tmp/k0s/fup28/mutant-94.log` / `mutant-140.log`。
* **② exec（换成的新形状）**：两宿主绿 `0 / 300`；变异（预算 0）**红 122/300、223/300**，
  失败签名是 `Code(127)` + `stderr` 一行 `sandlock-init: exec "/usr/bin/dotdot" failed (errno 11)`
  （errno 11 = EAGAIN，不是 ENOENT ⇒ 是 `..` walk 被拒，不是文件缺失）。
  **同一批运行里 `exec /bin/echo` 依然是 0/300**，所以：如果判据还是旧形状，这条 bug 在 arm64 上
  可以被完全漏掉；换成新形状后它有判别力（**变异对照红 → 绿成立**）。
* **③ 内核原始 EAGAIN > 0**：每一次运行都 > 1000（3017–4173 区间），
  证明①的"归零"来自有界重试而不是"内核突然不报 EAGAIN"。判据要求只是 > 0。

**fup-28 现状（本任务口径）**：三条前提（①②③）现在都有**在部署宿主上跑出来的、带变异对照的**证据，
其中②换了有效形状。**撤不撤 `_root_absolute_links` 那行改写仍然是"之后"的事**，本任务没动它。

---

## 6. 节点清理（本次用完就清）

清理由 `tmp/k0s/fup28/node2/cleanup.sh` 执行（推到 `/root/cleanup-fup28.sh` 再跑，
跑完把自己也删掉），两宿主输出：

```
== iZuf697v12g31dyz4uvsjlZ 6.12.0-211.34.1.el10_2.aarch64
before: /opt/fup28 = 1.9G
OPT-FUP28: REMOVED
after: df / => /dev/nvme0n1p2  100G   22G   78G  22% /
kept (pre-existing toolchain cache from the earlier arm-lane task): /root/.cargo = 402M
kept (earlier arm-lane task, not this one): /opt/arm-lane = 174M
kept (this repo's normal deploy docs, untouched): /var/lib/e2b-images = 3.8G
CLEANUP_DONE

== iZuf6d1usviqv6x9qk1hpcZ 6.12.0-211.34.1.el10_2.aarch64
before: /opt/fup28 = 241M
OPT-FUP28: REMOVED
after: df / => /dev/nvme0n1p2  100G   28G   72G  29% /
kept ... /var/lib/e2b-images = 2.5G
CLEANUP_DONE
```

**清没清：清了。** 事后用跳板机直连（**不再经 `run-target.exp`**，避免又把通道文件写回去）复核：

```
$ ssh <bastion> "ssh root@172.18.80.94 'ls -d /opt/fup28; ls -la /root/cleanup-fup28.sh; ls -la /tmp/sandlock-task.sh'"
/opt/fup28 ABSENT
cleanup script ABSENT
/tmp/sandlock-task.sh ABSENT
$ ssh <bastion> "ssh root@172.18.80.140 '...'"
/opt/fup28 ABSENT
cleanup script ABSENT
/tmp/sandlock-task.sh ABSENT
```

两宿主磁盘都回到出发前的用量（`.94` 22%、`.140` 29%，与 §6 开头那次 `df` 完全相同）。
留在节点上的只有**不是我这次建的**东西：`/opt/arm-lane`（更早的 arm-lane 任务）、
`/root/.cargo`（更早任务装的 cargo/rustc 的工具链缓存，本次只做 `cargo fetch/build`，**没有装任何新东西**）。
**镜像缓存 `/var/lib/e2b-images` 一个字没改**（soak 用的是 `cp -a` 出的副本）。

---

## 7. 担忧 / 局限

1. **新形状是我构造的，不是镜像自带的** —— arm64 镜像里确实没有 `..`-interp（§2），
   这正是要写进 FUP-28 的修订点。它的合法性靠三点：① 与 x86 镜像那个真实缺陷**同形**
   （`/lib64/<loader> -> ../lib/…`）；② 竞态加在 `/lib64`，而 dotdot 自身文件在 `/usr/bin`，
   所以能红的只有 `PT_INTERP` 那次 open（§2 的 `usr/bin/dotdot` 裸探针 0/40000 佐证）；
   ③ 换成 root-absolute 就绿（§4.4）。
2. **fork 侧的判据原文还没改。** `third_party/sandlock/docs/fork-plan-followups.md` 的 FUP-28
   前提③里那两句话仍写着 `exec /bin/echo`（那是另一个 git 仓库，本任务没在里面提交）。
   本仓的登记已按新形状改成 §5 的口径；fork 文档下次动到时应当同步（"在 arm64 上按
   `..`-interp 判，不按 `exec /bin/echo` 判"）。
3. **harness 仍是重建物，不是原物**（同上一份报告）：`.superpowers/sdd/task-f26-report.md` §3
   用的临时 fork 测试目标已经不在磁盘上，`f26_soak.rs` 是按 §3/§3.1 描述重建的。
   判别力靠变异对照自证，但"数字与 x86 当年可比"只能到此为止 —— **三个数是重新量的**。
4. **soak 走的是 fork 引擎（`SandboxInstance` exec-only + chroot），不是 E2B 的产品接线**
   （没经 envd → route B `sandlock-supervise` → wheel 那条链，也没走 seccomp 档 / COW upper）。
   被验**代码**是部署 revision（`d750fa1`，见 §1 的钉子），**接线**不在回路里。
5. **① 的变异对照本轮没重跑**（明确写在这里，避免被读成"本轮全跑了"）：本轮变异跑的是
   `phases=bc`，① 的变异数字引用同一份 harness 在 2026-09-27 早些时候跑出的那一对
   （`2616 / 97482`、`2015 / 97482`），原始日志在 `tmp/k0s/fup28/mutant-94.log` /
   `mutant-140.log`。
6. **`.140` 跑的是 `.94` 编的二进制**（它没有 cargo）：两端 `sha256` 逐字节一致、同发行版
   （Rocky 10.2）、同内核（`6.12.0-211.34.1.el10_2.aarch64`）、同 glibc；
   风险是"用户态差异没被覆盖"，对"内核行为 + 同一份二进制"这两条判据无影响。
7. **红出来的比例是竞态的函数，不是常数**：`122 / 223 / 149`（300 次里）随机器与当次调度变化；
   判据是"**0 vs 非 0**"，不是某个百分比。同理 §2 的 `566 / 776 / 40000` 也只是"这条形状
   会被内核拒"的存在性证据。

---

## 8. 文件清单

**提交物（本仓，只有这一个文件）**：`.superpowers/sdd/debt-fup28-exec-shape-report.md`（本文件）。
本仓另有两处**登记性**改动（把 FUP-28 的 exec 判据换成 §5 的口径）：
`docs/open-issues.md` 的 FUP-28 行、`docs/HANDOFF.md` 的 FUP-28 行。

**证据与工具（`tmp/`，gitignored，留在本机）**：

| 路径 | 内容 |
|---|---|
| `tmp/k0s/fup28/node2/stage2.sh` | 节点侧铺台：解源码、还原/新造 10 条 `..` 链、编 `dotdot`、打钉子 |
| `tmp/k0s/fup28/node2/build-bins2.sh` | 绿/变异两个二进制的构建（同一份源码、只改一个常量） |
| `tmp/k0s/fup28/node2/run2.sh` | 跑一次 soak（`FUP28_PHASES` 可选）+ 同时起裸内核探针 |
| `tmp/k0s/fup28/node2/shape2.sh` | 形状自证（三条裸 `openat2`） |
| `tmp/k0s/fup28/node2/inplace2.sh` | 就地变异往返（改 0 红 / 改回 4 绿） |
| `tmp/k0s/fup28/node2/cleanup.sh` | 节点清理（含自删） |
| `tmp/k0s/fup28/src-d750fa1.tgz` | 被测 fork 源码（`git archive d750fa1`，sha256 `fa702bb4…`） |
| `tmp/k0s/fup28/small2.tgz` / `small3.tgz` | harness + 节点脚本（`small3` 多了 `inplace2.sh`） |
| `tmp/k0s/fup28/logs2/stage-94.log`、`stage-140.log` | 铺台原始输出（镜像链、`PT_INTERP`、10 条 `..` 链、loader sha） |
| `tmp/k0s/fup28/logs2/shape-94.log`、`shape-140.log` | 形状自证原始输出 |
| `tmp/k0s/fup28/logs2/build-bins-94.log` | 两个二进制的构建与 sha256 |
| `tmp/k0s/fup28/logs2/green-abc-94.log`、`green-abc-140.log` | 验收配置（绿）原始输出 |
| `tmp/k0s/fup28/logs2/mutant-bc-94.log`、`mutant-bc-140.log` | 变异（红）原始输出 |
| `tmp/k0s/fup28/logs2/mutant-rewritten-94.log`、`-140.log` | 反向对照（改写形状 ⇒ 绿） |
| `tmp/k0s/fup28/logs2/inplace-94.log` | 就地往返原始输出 |
| `tmp/k0s/fup28/logs2/cleanup-*.log` | 清理原始输出 |
| `tmp/k0s/fup28/mutant-94.log`、`mutant-140.log` | ① 的变异对照（上一份报告跑的，本轮引用） |

**一句话**：arm64 上「exec」这条判据原来什么都不证明；换成"解释器挂 `..`"的形状后，
它在两台部署宿主上都**有判别力**（绿 0/300、变异 122–223/300、改成 E2B 改写的形状又回 0/300），
连同受管 open（0/97482）与内核原始 EAGAIN（>3000/40000）一起，FUP-28 的三条前提现在都是**有效**的。
节点已清干净；那行改写**一行没动**。
