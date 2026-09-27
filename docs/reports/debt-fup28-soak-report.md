# FUP-28 前提③：部署宿主上的 arm64 产品路径 soak（2026-09-27）

**状态：跑完了。两台 worker 宿主（`172.18.80.94` / `172.18.80.140`）都满足 FUP-28 的
三条判据，正反向对照都在同一宿主、同一 rootfs 上跑过。**
按任务约束，`envd_service/runtime/image_resolver.py::_root_absolute_links` 的改写
**一行没动** —— 本任务只产出判据。

---

## 0. 结论摘要

1. **能做，今天做成了**。缺的"arm64 soak 二进制"不是靠交叉编译，而是：节点 `.94` 上
   本来就有**原生 arm64 的 cargo/rustc**（早先 arm-lane 任务 `dnf install -y cargo rust`
   装的，`/usr/bin/cargo 1.92.0`）+ crates.io 可达（`index.crates.io` 200），而 arm64 的
   镜像缓存里正好有一份**未改写**的 rootfs。于是整条链路是"在部署宿主上构建 + 在部署宿主上跑"，
   既不是本机交叉编译，也没在集群里起镜像。
   `.140` 上没有 cargo，**没有给它装任何东西**：在 `.94` 上编出 arm64 测试二进制
   （`strip` 后 13.3 MB，gzip 4.2 MB），用跳板机做**主机间中继**送到 `.140` 运行，
   两端 `sha256` 逐字节一致。
2. **三条判据两宿主都过**（§3）：
   受管 open 连续 **97482 次 0 失败**、300 条 `exec /bin/echo` **0 次「127 + 空 stderr」**、
   同一次运行里内核侧原始 **EAGAIN > 0**（1124–3713 / 40000）。
3. **一个必须写下来的形态事实**：**arm64 基础镜像的动态链接器路径不含 `..`**
   （`PT_INTERP=/lib/ld-linux-aarch64.so.1` → `/lib -> usr/lib` → `usr/lib/
   aarch64-linux-gnu/ld-linux-aarch64.so.1`，全程没有 `..` 分量），而 amd64 的是
   `PT_INTERP=/lib64/ld-linux-x86-64.so.2` + `usr/lib64/ld-linux-x86-64.so.2 ->
   ../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2`（**有** `..`）。
   ⇒ FUP-28 判据里那条「300 条 `exec /bin/echo`」在 arm64 上是**恒真的空检查**：
   把重试预算改成 0 之后它仍然是 **0/300**（见 §3 的变异列），因为这条 exec 路径
   根本不经过 `..`。真正会在 arm64 上被这条 bug 打中的 exec 形状，是"解释器挂在
   `..` 软链后面"，我把它造了出来（`lib64/ld-linux-aarch64.so.1 -> ../lib/ld-linux-aarch64.so.1`
   \+ `gcc -Wl,--dynamic-linker=/lib64/ld-linux-aarch64.so.1` 编的 `/usr/bin/dotdot`），
   两宿主 **0/300**（变异后 120/300、248/300）。
4. **绿不是"环境没触发"**。把 `crates/sandlock-core/src/sys/fs.rs` 的
   `EAGAIN_RETRY_BUDGET` 由 4 改 0（与 FUP-26 报告同一条变异）在两宿主都**红**：
   受管 open **2015–2616 / 97482**（errno 直方图**只有 11 = EAGAIN**），
   `..`-interp exec **120–248 / 300**。同一台机器、同一份 rootfs、同一套竞态。
5. **改写并不是 arm64 上的空操作**：arm64 镜像里有 9 条 `..` 相对软链，其中
   `/etc/os-release -> ../usr/lib/os-release` 是沙箱里真会走到的（`python3` 探
   `platform`、apt、各种发行版嗅探都读它）。把它当目标打点：绿 0/97482、变异
   **2600/97482**。所以撤改写之后，arm64 上仍有一条（虽然很窄的）暴露面，
   靠的正是 ① 带来的那次有界重试。

---

## 1. 可行性判定：为什么"能"（以及每一步的依据）

| 问题 | 事实（本机/节点实测） | 依据 |
|---|---|---|
| 本机能不能出 arm64 二进制 | 本机 macOS x86_64（`Darwin 25.6.0 x86_64`），有 `zig`/`cargo-zigbuild` 与 `aarch64-unknown-linux-{gnu,musl}` target | 本机 `uname`/`rustup target list` |
| 节点上有没有工具链 | `.94`：`cargo 1.92.0` + `rustc 1.92.0`（native aarch64）+ `gcc 14.3.1` + `python3.12`；`.140`：**没有 cargo/rustc** | `tmp/k0s/fup28/host-probe-94.log`、`build-probe-94.log` |
| 节点能不能拉 crates.io | `.94` `https://index.crates.io/config.json` → **200** | `build-probe-94.log` |
| 有没有"未改写的 rootfs" | 缓存里就有：`/var/lib/e2b-images/registry…python-mcp_3.14_sha256_3675662d…-72f187e0…/rootfs`（= 线上 `E2B_BASE_IMAGE`，arm64 manifest `72f187e0…`） | `rootfs-probe-94.log`、`kubectl get sts e2b-worker -o yaml` |
| 缓存里的 link 是原始形态吗 | 不是 —— 解析器交付时**就地**改写，缓存里已经是 0 条 `..` 软链 ⇒ 按 FUP-28 的说法**显式还原** | `rootfs-probe-94.log`（`count=0`） |
| 要不要起 dev 镜像 | 不用。节点原生 cargo 直接编，比在集群里拉 dev 镜像更省事、也不动集群 | 见 §2 |
| 为什么不在 `.140` 上也编 | 它没有 cargo。**没有为了跑 soak 在 worker 宿主上装任何东西**：`.94` 编一次，`strip`+gzip 后经跳板机主机间中继过去 | `tmp/k0s/fup28/relay.exp` + 两端 `sha256sum` |

**两宿主内核**（soak 的目标）：都是 `6.12.0-211.34.1.el10_2.aarch64`（与
`docs/deploy-clusters.md` 一致），soak 运行时逐次打印。

**rootfs 的身份钉子**：两宿主上 `usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1` 的
`sha256` 都是 `1d8b77f28b7cec0329dca107a28fb0a850193b1dcce59be68fa0b06b514548cc`
（`stage-94.log` / `stage-build-140.log`）—— 同一份镜像根。

**还原/新造出来的 `..` 软链（两宿主一致，`dotdot_link_count=10`）**：

```
etc/os-release                        -> ../usr/lib/os-release          (镜像原样)
usr/bin/ld.so                         -> ../lib/ld-linux-aarch64.so.1   (镜像原样)
usr/bin/pidof                         -> ../sbin/killall5               (镜像原样)
usr/lib/apt/planners/dump             -> ../solvers/dump                (镜像原样)
usr/share/zoneinfo/Arctic/Longyearbyen-> ../Europe/Oslo                 (镜像原样)
usr/share/zoneinfo/Asia/Istanbul      -> ../Europe/Istanbul             (镜像原样)
usr/share/zoneinfo/Atlantic/Jan_Mayen -> ../Europe/Oslo                 (镜像原样)
usr/share/zoneinfo/Europe/Nicosia     -> ../Asia/Nicosia                (镜像原样)
var/spool/mail                        -> ../mail                        (镜像原样)
lib64/ld-linux-aarch64.so.1           -> ../lib/ld-linux-aarch64.so.1   (新造：arm64 上的 x86 形状)
```

前 9 条是**镜像本来就有**的 `..` 相对软链（被解析器改写/裁剪，这里按 FUP-28
"取一个未改写的镜像 rootfs"还原）；第 10 条是把 x86 的
`/lib64/ld-linux-x86-64.so.2 -> ../lib/x86_64-linux-gnu/…` 在 arm64 上**等价复现**。

---

## 2. soak 本身是怎么跑的（可复现）

**harness**（`tmp/k0s/fup28/soak/f26_soak.rs`，临时目标，不进 fork 仓库）：
起一个 mainless、exec-only 的 `SandboxInstance`（`chroot=<rootfs 副本>` +
`/workspace`、`/home/user` 挂载），在**被走的目录里持续 rename**的竞态下跑三段：

| 段 | 做什么 | 判据 |
|---|---|---|
| a | 沙箱内一个长命子进程连续 **97482** 次受管 `open` 那条 `..` 软链（`tmp/k0s/fup28/soak/openloop.py`，进程内循环，就是中介替 `PT_INTERP` 做的那次 open 的客户端形态） | 失败数必须 **0** |
| b | 连续 **300** 条 `exec /bin/echo ok` | 不得出现非 0 退出 / 空 stderr |
| c | 连续 **300** 条 `exec /usr/bin/dotdot`（PT_INTERP 走 `..` 的二进制） | 同上 |

竞态放大器 = 4 条线程在 `<rootfs>/lib64` 里把两个 scratch 目录来回 rename
（FUP-26 报告 §3.1 量过：同目录 rename 让 `..` walk 的 EAGAIN 从 0.02% 抬到
1.7–5%）。三段跑完后打印 `FUP28_RESULT …`，`assert_eq!` 精确要求三个 0。

**内核侧原始 EAGAIN**：同一次运行里并发跑 `tmp/k0s/fup28/soak/raw_openat2.py`
（裸 `openat2(dirfd=rootfs, "lib64/ld-linux-aarch64.so.1", RESOLVE_IN_ROOT)`，
自己的 4 条 rename 线程，40000 次），打印 errno 直方图 —— 它**绕过**中介，
所以它报的 EAGAIN 就是"内核真的在这个形状上说不"。

**执行方式**（原始命令都在日志首行）：

```bash
# 0) 通道（任何 kubectl/上节点前）
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"

# 1) 传文件（分片 base64；单 argv ≤128 KiB，见 push2.sh 的守卫）
sh tmp/k0s/fup28/push2.sh third_party/sandlock/tmp/fup28-src.tgz /opt/fup28/src.tgz 172.18.80.94 40000
sh tmp/k0s/fup28/push2.sh tmp/k0s/fup28/small.tgz         /opt/fup28/small.tgz 172.18.80.94 40000

# 2) 铺台（复制缓存 rootfs、还原/新造 10 条 `..` 链、编 dotdot、写 sha 钉子）
#    经 deploy/scripts/lib/run-target.exp 在节点上执行 /opt/fup28/stage.sh

# 3) 只读探针式的构建 + 跑（.94）
#    run-build.sh → cargo fetch + cargo test --no-run（arm64 原生，约 1 分）
#    run-soak.sh  → 后台裸探针 + cargo test -- --nocapture --test-threads=1

# 4) .140：中继 .94 编好的二进制，不装工具链
expect tmp/k0s/fup28/relay.exp 172.18.80.94 172.18.80.140 /opt/fup28/f26_soak.bin.gz
#    再 sh /opt/fup28/run-phases.sh /opt/fup28/f26_soak.bin abc
```

**正向/反向的两种二进制**（都由 `.94` 编、都在两端验过 `sha256`）：

| 二进制 | 内容 | sha256（gunzip 后，两端一致） |
|---|---|---|
| `f26_soak.bin` | 树上的原样（`EAGAIN_RETRY_BUDGET = 4`） | `55beea3a33d96b9620a73b8a67ce8708fa4f8ad10f7e15d937219b4c2720704b` |
| `f26_soak_mutant.bin` | 只把预算改成 0 | `28c793303369ef7500045ae7a364da58437e6605aa164c6d46c2d2bc4ef50d90` |

---

## 3. 对照 FUP-28 的三条判据

原始输出（`tmp/k0s/fup28/`，逐条可回看）：

| 宿主 | 配置 | 判据 a：受管 open | 判据 b：300×`exec /bin/echo` | arm64 真正的 exec 形状（300×`..`-interp） | 内核原始 EAGAIN | 日志 |
|---|---|---|---|---|---|---|
| `.94` | **绿**（预算 4） | **0 / 97482** | **0 / 300**（`bad=0 first=None`） | **0 / 300** | **3713 / 40000** | `phasesbc-green-g94.log`、`soak-94-run1.log`、`soak-94-run3.log` |
| `.94` | 变异（预算 0） | **2616 / 97482**（`hist=[(11, 2616)]`） | 0 / 300 | **120 / 300**（`Code(127)`、stderr 一行 `errno 11`） | 3711 / 40000 | `mutant-94.log`、`phasesbc-mutant-m94-full.log` |
| `.140` | **绿**（同一二进制） | **0 / 97482** | **0 / 300** | **0 / 300** | **3017 / 40000** | `phasesbc-green-g140.log`、`soak-140-run1.log` |
| `.140` | 变异（同一变异二进制） | **2015 / 97482**（`hist=[(11, 2015)]`） | 0 / 300 | **248 / 300** | 2546–2651 / 40000 | `mutant-140.log`、`phasesbc-mutant-m140-full.log` |
| `.94` | 绿 / 变异：**镜像自带的 `..` 链**（`/etc/os-release`） | **0 / 97482** / **2600 / 97482** | —— | —— | —— | `natural-os-release-94-f26_soak*.log` |

逐条判读：

* **① 受管 open 连续 97482 次 0 失败 —— 满足（两宿主）。** 变异列给出对照：
  失败 errno 直方图**只有 11（EAGAIN）**，与 FUP-26 报告的机制一致；绿列的
  `hist=[]` 是"一次都没被拒"，不是"没跑"（`OPENLOOP n=97482` 逐次核对）。
* **② 300 条 `exec /bin/echo` 0 次「127 + 空 stderr」—— 形式上满足，但在 arm64 上
  这条判据是空的。** 变异二进制同样是 0/300：arm64 的 `PT_INTERP` 路径没有 `..`
  分量，这条命令本来就不会经过会返回 EAGAIN 的那次 walk。FUP-28 的这句判据
  是按 x86 镜像写的；在 arm64 上要**换成"解释器挂在 `..` 软链后面"**的形状才有判别力
  —— 我做了这个形状（上表第 5 列），绿 0/300、变异 120/300 与 248/300
  （失败签名正是 `exit 127` + 空 stdout）。
* **③ 内核侧原始 EAGAIN > 0 —— 满足（两宿主，每一次运行都 > 1000）。**
  数字比前提②记录的 `8107/40000`、`6855/40000` 小，因为这里的 racer 形状不同
  （前提② 是在**被走路径下面** rename，我这里是在**被走目录里** rename 两个 scratch
  条目，正是 FUP-26 §3.1 里 1.7–2.5% 的那一档）。**只要 > 0 就满足判据**，
  它证明"归零来自重试，不是内核突然不报 EAGAIN"。

**变异为什么是可信的对照**：只改一个常量（`EAGAIN_RETRY_BUDGET 4 → 0`），
同一份 rootfs、同一次竞态、同一台机器；红出来的 errno 与 FUP-26 报告里的
`Resource temporarily unavailable` 同源。另外 `.94` 上第一次变异跑完恢复源码时，
`cp -a` 把 mtime 也恢复了 ⇒ cargo 认为无需重编、**误跑了变异二进制**（`soak-94-run2.log`
里 3848/97482 的假绿失败），`touch` 后重编才是真绿（`soak-94-run3.log`）。这条坑记下来：
**恢复源码要 `touch`**。

---

## 4. 与既有前提的连接（①②）

* **① 含 FUP-26 的 wheel 已上线** —— 本任务没有重新走构建链，但把"我 soak 的代码 = 上线
  代码"钉住了：`crates/sandlock-core/src/sys/fs.rs` 的**最后一个**改动就是 FUP-26 的
  `0164575`，之后无人动过；`git diff 7b60349c HEAD -- crates/sandlock-core/src/sys/fs.rs`
  为空，而 `7b60349c` 正是 `docs/open-issues.md` 记的线上 wheel manifest HEAD，
  且 `git merge-base --is-ancestor 0164575 7b60349c` 成立、该提交里
  `EAGAIN_RETRY_BUDGET: u32 = 4`。也就是说 soak 打的就是线上那一份重试代码。
* **② 每个 worker 宿主内核实测通过** —— 本任务顺带复测到：两宿主在 40000 次裸
  `openat2(RESOLVE_IN_ROOT)` 上都报 EAGAIN（见上表），与 `docs/fork-plan-followups.md`
  记录的方向一致。

---

## 5. 文件清单

**提交物（本仓）**：只有这一个

* `.superpowers/sdd/debt-fup28-soak-report.md`（本文件）

**证据与工具（`tmp/` 内，gitignored，留在机器上）**

| 路径 | 内容 |
|---|---|
| `tmp/k0s/fup28/soak/f26_soak.rs` | soak harness（临时 fork 测试目标；不进 fork 仓） |
| `tmp/k0s/fup28/soak/openloop.py` | 沙箱内的受管 open 循环（判据 a 的客户端形态） |
| `tmp/k0s/fup28/soak/dotdot.c` | PT_INTERP 走 `..` 的二进制（arm64 上的 x86 形状） |
| `tmp/k0s/fup28/soak/raw_openat2.py` | 裸内核探针（判据 c：原始 EAGAIN 直方图） |
| `tmp/k0s/fup28/stage.sh` | 节点侧铺台（复制 rootfs、还原/新造 10 条 `..`、编 dotdot、sha 钉子） |
| `tmp/k0s/fup28/run-build.sh` / `run-soak.sh` / `run-soak-bin.sh` / `run-phases.sh` | 节点侧跑法 |
| `tmp/k0s/fup28/build-both-bins.sh` / `make-mutant-bin.sh` / `mutant.sh` | 绿/变异两个二进制的构建与变异 |
| `tmp/k0s/fup28/push2.sh` / `relay.exp` | 传输（分片 base64；跳板机主机间中继） |
| `tmp/k0s/fup28/*.log` | 全部原始输出（`soak-94-run1/run2/run3`、`soak-140-run1`、`mutant-94`、`mutant-140`、`phasesbc-*-g94/g140`、`phasesbc-mutant-m94/m140-full`、`natural-os-release-94-*`、`stage-94`、`stage-build-140`、`build-94`、`host-probe-94`、`build-probe-94`、`rootfs-probe-94`、`image-shape-arm64-vs-amd64`、`wheel-manifest-probe*`） |

**节点上留下的东西**（两宿主）：`/opt/fup28/`（rootfs 副本 ~200 MB、源码、二进制；
`.94` 另有 cargo `target/`）。**未清理** —— 需要的话一条命令可删（§6 第 5 条）。

---

## 6. 担忧 / 局限（按重要性）

1. **harness 是我重建的，不是原物。** `.superpowers/sdd/task-f26-report.md` §3 用的是
   临时 fork 测试目标 `f26_soak`，它的副本（`tmp/f26/keep-f26_soak.rs.txt`）连同
   `tmp/f26/` 整个目录已经不在磁盘上了，fork 仓库里也从未提交过它。
   我按 §3/§3.1 的描述重建（受管 open 计数、127 判据、同目录 rename 竞态），并用
   **变异对照**证明它有判别力（预算 0 ⇒ 两宿主都红，errno 只有 EAGAIN）。
   但"数字与当年可比"这一点只能到此为止：**判据本身（三个数）是重新量的，不是引用旧值。**
2. **soak 走的是 fork 引擎，不是 E2B 的产品接线。** 我起的是 `sandlock-core` 的
   `SandboxInstance`（exec-only + chroot，就是 FUP-26 §3 的形态），没有经过
   envd → route B `sandlock-supervise` → wheel 那条链，也没有走 seccomp 档 / COW upper。
   被验的**代码**与线上逐字节相同（§4），但**接线**不在回路里。
3. **arm64 上 FUP-28 的 exec 判据是空的**（§3 ②）。如果谁只看"300 条 `exec /bin/echo`
   0 次 127"就下结论，那在 arm64 上等于什么都没证明。真正的判据是
   "解释器挂在 `..` 软链后面"的那条，我把它补上了，但它**是我构造的形状**，
   不是镜像自带的（arm64 镜像里没有 —— 这正是需要写进 FUP-28 的修订点）。
4. **`.140` 跑的是 `.94` 编的二进制。** 两宿主同发行版（Rocky 10.2）、同内核
   （`6.12.0-211.34.1.el10_2.aarch64`）、同 glibc，二进制 sha256 两端一致；
   风险是"这台机器的用户态差异没被覆盖"，对"内核行为"这条判据无影响。
5. **节点上留了现场**（`/opt/fup28`，两宿主）。需要清理：
   ```bash
   set -a; . deploy/scripts/bastion.env; set +a
   for h in 172.18.80.94 172.18.80.140; do
     cmd=$(printf '%s' 'rm -rf /opt/fup28' | base64 | tr -d '\n')
     TARGET_HOST=$h expect deploy/scripts/lib/run-target.exp "$cmd" root
   done
   ```
   （`.94` 上还留了 `/opt/arm-lane`，那是更早的 arm-lane 任务留下的，不是本任务。）
6. **临时传输工具 `tmp/k0s/fup28/push-file.sh` 是坏的**（用文件中转 `$(cat …)` 之后，
   远端文件会多出"脚本自身的 base64"一截），我换成 `push2.sh`（变量直传 + sha256 校验）
   才拿到逐字节一致。坏脚本留在 tmp 里是因为它是个"别这么写"的样本；
   要复用时**只用 `push2.sh`**。

**一句话**：FUP-28 的判据在**两台部署宿主**上都满足（而且有变异对照证明不是空跑），
arm64 上要按"解释器挂 `..`"的形状判而不是按"`exec /bin/echo`"判；撤不撤改写仍然
由你拍板 —— 本任务没动那行代码。
