# 跨平台构建与测试 lane（x86_64 容器 + aarch64 真内核）

这份文档是"以后不用从头再来"的那一份：两套本地 lane 各自能证明什么、脚本在哪、怎么跑、
以及一路上踩过的每一个**看起来像产品回归、其实不是**的坑。脚本已经入库
（`deploy/scripts/arm-lane/`），不再只活在 `tmp/` 里。

先说三条纪律，它们比命令重要：

1. **测试二进制不离开这台机器**。S0/S1 早期往线上 aarch64 节点推过二进制取证，S3 起改成
   本地 Lima VM，此后一律不推。
2. **数字要登记**。fork 的每个相位、E2B 的每种形态都有一个数，写在
   `third_party/sandlock/docs/test-baseline.md`（arm64 段在最下面）与本文第 6 节。
   改完对应的东西必须复跑那条相位并刷新数字，否则基线就是假的。
3. **RED 先于 GREEN**。每条"修好了"都要有改前的实测（日志或断言）+ 改后的实测；
   说不清改前是什么样，就等于没验证。

---

## 1. 三条跑道

| 跑道 | 是什么 | 能证明 | 不能证明 |
|---|---|---|---|
| `x86_64 容器` | `sandlock-dev:latest`（fork 门禁）/ `e2b-sandlock-test:latest`（E2B 生产形态），docker，宿主就是 x86_64 | fork 的四条门禁相位、E2B `test-prod-shaped.sh` 两相位、`tests/security` 生产形态 | 任何 ABI/架构相关的东西（它和开发机同构） |
| `aarch64 Lima VM` | 本机 qemu TCG 上的 Ubuntu 24.04 arm64，真内核、真 ptrace | fork 五条相位在 arm64 上的等价、`tests/security` 两态、任何 syscall 号/结构体布局/字长相关的行为 | 性能（PSS、延迟——TCG 比真机慢约 100×，`supervise_cost` 因此**不进**这条 lane） |
| 线上节点 | 部署出来的 aarch64 机器 | 部署形态本身 | 不再用于跑测试二进制（见纪律 1） |

宿主是 **amd64 Darwin**：容器是 x86_64，arm64 只能靠 VM（`--platform linux/arm64` 是
qemu-user，`ptrace`/`process_vm_writev` 直接 ENOSYS，C/R 用例在那里根本跑不了）。

---

## 2. 脚本清单（`deploy/scripts/arm-lane/`）

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `vm.yaml` | 宿主 | Lima VM 的定义，逐字可复现（见第 5.1） |
| `lima-vm.sh` | 宿主 | `start` / `stop` / `shell` / `run <cmd>` / `sync` / `prep`。**唯一**需要跟 VM 打交道的入口 |
| `xbuild.sh` | 宿主 | 用 zig 工具链容器交叉编译 aarch64 的 fork 产物 |
| `xbuild-debug.sh` | 宿主 | 同上，调试变体 |
| `e2b-sync.sh` | 宿主 | 把 E2B 侧（python 包 + `tests/security` + 绑定 + wheel 里的 `sandlock-supervise`）搬进 guest |
| `guest-prep.sh` | guest（stdin） | 测试容器 entrypoint 的 root prep：`/etc/hosts` 与 `198.18.0.0/15` 地址、`/usr/local/bin/python3`、低端口窗口 |
| `phase-run.sh` | guest（stdin） | 跑 fork 的五条相位并把每条 `test result: ok. N passed` 求和（与 `scripts/test-all.sh` 同口径） |
| `x86-security.sh` | 宿主 | 在 x86_64 生产形态容器里**只**跑 `tests/security`（`test-prod-shaped.sh` 做不到，见 §6.1） |

脚本都能从任意 cwd 调用（自己按 `$0` 定位仓根）。**产物**仍然落在 `tmp/arm-lane/`
（gitignored）：`target/` 是交叉编译产物（约 14G）。**证据**在 `deploy/scripts/arm-lane/evidence/`
——它是各轮跑完后拷进来的一份快照（本文引用的日志就是这些）；重跑会在 `tmp/arm-lane/` 下产生
同名活日志，两者不会互相覆盖。

---

## 3. aarch64 lane 的机制

### 3.1 为什么到处都在绕

四条硬约束，每条都对应一次真实的假红：

* **9p 挂载会给你旧内容**：宿主原地重写文件后，guest 通过 `/lima-repo` 读到的还是旧版本，
  连 mtime 都不变（实测：宿主 6→29 字节，12 秒后 guest 仍旧）。rsync 的 quick check 因此
  判定"无需复制"，lane 会拿**上一次的二进制**跑出结果。⇒ 源码与产物一律走 **ssh 上的 tar
  流**，9p 只当随手看的窗口。
* **工作区必须在 guest 本机文件系统上**：Landlock 规则在 9p 上"装得上但从不在匹配时生效"，
  于是每个限制 workspace 路径的沙箱用例都拿 EACCES。⇒ 产物复制进 guest 本机盘再跑。
* **`/src` 必须是 bind mount，不能是符号链接**：根相位会 canonicalize 自己的 tmp 根，
  符号链接会把路径还原成 `<mirror>/third_party/sandlock/tmp/...`，注册控制套接字随后撞
  `SUN_LEN`（108 字节）。x86_64 容器把仓挂在 `/workspace`，短得多，所以这条只在 lane 上出现。
* **目标根必须是 `/var/tmp/aarch64-target`，不能是 `/tmp`**：测试二进制把
  `CARGO_TARGET_TMPDIR` 编译期烘焙进去，而沙箱策略给 `/tmp` 开了 `fs_write`。夹具一旦落在
  `/tmp` 下就**落进了写授权**，五个 landlock named-unix 门禁（connect/sendto/sendmsg/
  sendmmsg/symlink escape）会从 EACCES 变成 CONNECTED/SENT。`/var/tmp` 同样是 1777，但在
  所有授权之外。

### 3.2 交叉编译（宿主）

`xbuild.sh` = 一个 `docker run`，工具链镜像 `sandlock-zig-builder:local`：

```
-v $repo/third_party/sandlock:/src
-v $repo/tmp/arm-lane/target:/var/tmp/aarch64-target
-e CARGO_TARGET_DIR=/var/tmp/aarch64-target
-e ZIG_TARGET=aarch64-linux-gnu.2.34
-e CC_aarch64_unknown_linux_gnu=zigcc
-e CC_x86_64_unknown_linux_gnu=cc
-e CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=cc
```

两条不能删的理由：镜像里的 cargo config 把**两个** target 都指向 zigcc，而 zigcc 只认一个
全局 `ZIG_TARGET` ⇒ 宿主单元（build script / proc macro）会被链接成 aarch64；所以把宿主
linker 钉回 `cc`。`aarch64` 的 stub 用 `-Wl,--image-base=`（zig 拒绝 GCC 的
`-Ttext-segment=`），x86_64 才需要 `-mcmodel=large`。

例：`deploy/scripts/arm-lane/xbuild.sh cargo test --no-run --target aarch64-unknown-linux-gnu -p sandlock-ffi -p sandlock-supervise`

### 3.3 搬运（`sync`）

`lima-vm.sh sync` 做三件事：

1. 源码走 tar 流进 guest 的镜像仓（默认与宿主同路径，`ARM_LANE_GUEST_MIRROR` 可覆盖）。
2. **按 cargo 的 dep-info 解析要传哪些二进制**，不按名字猜：两个 crate 都有
   `tests/integration.rs`（sandlock-core 551 条 / sandlock-oci 自己的），而后缀是 metadata
   hash，硬编码列表会静默漏掉新构建。取"最新匹配的 `.d`"。只传该相位真正会跑的（`supervise_cost`
   故意不传：它要 release 二进制并测 PSS/延迟，仿真 lane 上没有意义）。
3. `mount --bind $mirror/third_party/sandlock /src`（幂等），并把 `libsandlock_ffi.so`、
   `tests/rootfs-helper`（zigcc 静态 musl 编的 aarch64 版）带上。

一次 sync 约 3–5 分钟（本机实测，主要是 ~4G 测试二进制过 ssh）。

### 3.4 在 guest 里跑

```bash
# 五条相位（本机实测 172 s）
limactl shell sandlock-arm -- bash -s < deploy/scripts/arm-lane/phase-run.sh

# E2B 两态（先 e2b-sync.sh）
deploy/scripts/arm-lane/lima-vm.sh run \
  'cd <mirror> && sudo env E2B_REQUIRE_SECCOMP_FILTER=0 E2B_REAL_ROOT=0 \
     /opt/e2b-venv/bin/python -m pytest tests/security -q -p no:cacheprovider'
```

* `E2B_REQUIRE_SECCOMP_FILTER=0` 是必需的：那个自检要求 worker 进程
  `/proc/self/status` 里 `Seccomp:` 非 0（生产 worker 跑不可信负载），而 guest 里 pytest 是
  裸跑的、没有 seccomp 档。
* `E2B_REAL_ROOT=0/1` 就是两态开关（模拟根 / mount ns + pivot_root 真根）。
* **不要**开 `E2B_TEST_STRICT_SKIPS=1`：guest 没有 docker，"docker is required"那几条会从
  skip 变 fail。

### 3.5 guest 环境现状

`/opt/e2b-venv`（Python 3.12 + e2b + fakeredis + fork 的 ctypes 绑定 + `bin/sandlock-supervise`）、
`cc`（gcc 13.3）、`setpriv` / `unshare` 可用；**没有 cargo、没有 docker、没有 docker CLI**。
后者是 `tests/security` 在 lane 上比 x86 多 8 条 skip 的全部原因。

---

## 4. x86_64 跑道

### 4.1 fork 门禁（四条相位）

```bash
cd third_party/sandlock && chmod -R a+rwX tmp
docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest sh scripts/test-all.sh
docker run --privileged --rm -v "$PWD":/src -w /src --entrypoint bash sandlock-dev:latest \
  -c 'sh scripts/test-all.sh --oci-root'
# 同理 --supervise-root / --mediation-2uid
```

`scripts/test-all.sh` 会把每条相位与 `docs/test-baseline.md` 的数**逐条比对**，不一致即失败
（包括"少跑了几条"），也会因为出现 skip/ignored 而失败。改完 fork 必须这四条都复跑。

### 4.2 E2B 生产形态

```bash
E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
  UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh
```

两个相位：phase 1 = root worker + 线上 cap 集 + `deploy/seccomp/sandlock-worker.json`；
phase 2 = uid 65534 非 root worker。`-k` 只能按用例名过滤，路径参数不会缩小范围（pitfall B4）。

只跑 `tests/security` 用 `deploy/scripts/arm-lane/x86-security.sh <0|1> <log>`：它复刻 phase 1
的镜像、cap 集、seccomp 档与 registry mirror，但目标是 `tests/security` 一个目录——这样两套
lane 的两态数字才是同一口径取的。本机实测每条约 2.5 分钟。

---

## 5. 从零到绿

### 5.1 重建 VM

现役 VM：`sandlock-arm`，Ubuntu 24.04.5 LTS，内核 `6.14.0-37-generic`，6 vCPU / 8 GiB /
30 GiB，`vmType: qemu`（宿主是 x86_64 ⇒ TCG，无硬件加速）。定义就是
`deploy/scripts/arm-lane/vm.yaml`：

```bash
# 1) Ubuntu 24.04 (noble) arm64 cloud image（约 590M）。
#    注：当时只留下了 curl 的进度条，没有 URL；重建时取 noble 的 arm64 cloud image 即可。
curl -L -o tmp/arm-vm/noble-arm64.img \
  https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img
# 2) 起 VM（yaml 里的镜像路径指向 tmp/arm-vm/noble-arm64.img）
limactl create --name sandlock-arm deploy/scripts/arm-lane/vm.yaml
limactl start sandlock-arm
```

`vm.yaml` 里两处是刻意的：**不写 `networks:`**（`lima: shared` 要 sudo 装 socket_vmnet，而
这条 lane 只需要 Lima 自带的 ssh 端口转发）；`/lima-repo` 以**同路径**只读挂进来当传输通道
（二进制里烘焙了宿主的绝对路径）。

### 5.2 首次准备与每次改动后的循环

```bash
# 一次：guest 里装依赖（cc、/opt/e2b-venv 等）——见 §3.5
deploy/scripts/arm-lane/e2b-sync.sh          # 第一次会建 venv 里的绑定与 supervise

# 每次改了 fork/主仓之后：
deploy/scripts/arm-lane/xbuild.sh cargo test --no-run --target aarch64-unknown-linux-gnu \
    -p sandlock-ffi -p sandlock-supervise        # 只编你改过的 crate
deploy/scripts/arm-lane/lima-vm.sh sync
limactl shell sandlock-arm -- bash -s < deploy/scripts/arm-lane/phase-run.sh
deploy/scripts/arm-lane/e2b-sync.sh          # 改了 E2B 侧才需要
```

### 5.3 基线（本文写作时的值）

| lane | 相位 | 值 |
|---|---|---|
| arm64 | ffi（含 C ABI） | 104 |
| arm64 | supervise | 51 |
| arm64 | oci（root） | 157 |
| arm64 | supervise_root（root） | 4 |
| arm64 | mediation_2uid（root） | 9 |
| arm64 | core_lib / core_integ | 904 / 551 |
| x86_64 | 同上四条 root 相位 | 157 / 4 / 9（ffi 104、supervise 51） |

E2B `tests/security` 四格（同轮、同镜像/同 seccomp 档）：

| 形态 | arm64 | x86_64 |
|---|---|---|
| `E2B_REAL_ROOT=0` | 35 passed / 9 skipped / 4 xfailed | 43 / 1 / 4 |
| `E2B_REAL_ROOT=1` | 38 passed / 9 skipped / 1 xfailed | 46 / 1 / 1 |

每格耗时（本机）：arm64 两态各约 35 分钟（qemu TCG），x86_64 两态各约 2.5 分钟。

两边的 `passed + xfailed` 逐态相等（39 / 47），差的 8 条是 guest 没 docker。

---

## 6. 症状 → 真因（这张表最值钱）

### 6.1 看起来像产品回归、其实不是

| 症状 | 真因 | 处置 |
|---|---|---|
| `... : Permission denied` 到处出现，且**说文件不存在** | 二进制里烘焙的路径在 guest 不存在：`/root/.rustup/.../x86_64-unknown-linux-gnu/bin/cargo` 在 0700 的 `/root` 下，非特权 uid 得到的是 EACCES 而不是 ENOENT | 给测试留一个可覆盖的工具链入口（`SANLOCK_CARGO`），或用 lane 的校验式 shim |
| supervisor 起不来：`control socket setup failed ... Operation not permitted` | 共享 ctl root 由 root 创建再 `chmod 0777` 给将以 65533 运行的 supervisor；非属主 `chmod`（无 CAP_FOWNER）是 EPERM，而 `setup_runtime_dir` 会把根收紧到 0700 | 把根 **chown 给会用它的那个 uid**（真实 per-user 根本来就是这样） |
| `policy rejected: unknown syscall or group name(s): chmod` | generic syscall 表里**没有** `chmod`（aarch64 的 `chmod(2)` 由 libc 落到 `fchmodat`），fork 拒绝为没有 syscall 号的名字装规则 | 按架构选名字 |
| `rm/ln/rmdir: ... Resource busy`，断言要的是 `Device or resource busy` | 那是 `strerror(EBUSY)` 的**libc 措辞**：lane 用 zigcc 静态链接（只有 musl），glibc 说 "Device or resource busy" | 两种整行措辞都精确接受（拒绝是契约，措辞属于 libc） |
| `static_bin: applet not found`（exit 127） | 夹具挑到了 busybox，而 busybox **按 argv[0] 派发 applet**；用例又按 tini 的横幅断言 | 每个候选带自己的名字/参数/横幅 |
| `the channel greets exactly once: left=0 right=1` | 固定墙钟窗口在 TCG 上不够（旧代码 10 s 平窗），读成"通道从没接上" | 等**使断言成立的事实**（字节数），静默截止兜底；失败时 dump 子进程 stderr（"通道空"和"supervisor 没起来"从外面看一样） |
| `the writer had to make some progress after the tightening (2097152 -> 2097152)` | 第一个等值采样被当成"写不动了"，其实只是下一个 1 MiB 的 `dd` 还没落盘 | 等值只在"已越过打紧点之后"才算停止 |
| `RunAs(501, 501) refused: unprivileged supervisor ... cannot map an arbitrary host uid` | euid != egid 时 `RunAs(uid, uid)` 就是**重映射**，未特权 supervisor 必须拒（fail closed） | "以自己的身份跑"要写真实 uid **和**真实 gid |
| core_integ 42 条网络红，报 "run the test container entrypoint (root prep)" | 镜像里 entrypoint 做的 root prep 没人做：`/etc/hosts` 与 `198.18.0.0/15` 地址没预置 | 跑 `guest-prep.sh`（`lima-vm.sh prep`） |
| ACL/egress 用例连不上，strace 显示只连了**代理** | Lima 把宿主的 `http_proxy` 转发进 guest，被测工作负载也遵守它 | `lima-vm.sh run` 会先 unset 全部 proxy 变量 |
| 五个 named-unix 门禁返回 CONNECTED/SENT（应 EACCES） | 目标根落在 `/tmp` 里 ⇒ 夹具落进了 `fs_write("/tmp")` 授权 | 目标根改 `/var/tmp/aarch64-target` |
| `bind DNS gateway: Permission denied (os error 13)` | 共享 netns + 非 root 绑 `<127.0.1.x>:53`（stock 1024） | lane 保留 `ip_unprivileged_port_start=0`（**仅这条 arm lane**，不代表任何部署形态：stack/k8s/compose 示例/本地池都不需要窗口）；**不能**用 `CAP_NET_BIND_SERVICE` 替代（file-cap exec 让进程 non-dumpable ⇒ `pidfd_getfd: EPERM`） |
| 注册控制套接字 `path must be shorter than SUN_LEN` | `/src` 是符号链接 ⇒ canonicalize 后路径变长 | `/src` 用 **bind mount** |
| 到处 `NotFound` / 结果像上一次的 | 9p 给了旧文件；rsync 认为不用复制 | 源码/产物走 ssh 上的 tar |
| 想按目录缩小 pytest 范围却缩不动 | `test-prod-shaped.sh` 是 `pytest tests ... "$@"`，路径只是追加 | 用 `-k`，或用 `x86-security.sh` |

### 6.2 只有跨架构 lane 才抓得到的一类

这四条都是 **x86_64 全绿、arm64 才红**，而且第一条最贵：

| 位置 | x86_64 | aarch64 | 后果 |
|---|---|---|---|
| `__NR_pivot_root`（E2B `_REAL_ROOT_PROBE`） | 155 | **41**（155 是 `sched_getattr`） | 探针**从没问过内核 pivot_root**，把别人的 ESRCH 报成"seccomp 档没放行" ⇒ `E2B_REAL_ROOT` 在**生产架构**上不可能被打开 |
| `struct epoll_event` 布局 | 12 字节 / `data@4`（packed） | 16 字节 / `data@8`（LP64 通例） | supervisor 从每条被拦截的 epoll 记录里读到错的那一半 |
| `path_surface` 的穷尽性检查按 **syscall 名字**比对 | 表里都有 | generic 表没有 `open`/`stat`/... | 中介账本在 aarch64 上比对的是不存在的名字 |
| `NON_PATH_SYSCALLS` | — | aarch64 表在 403..422 带 32 位 time64 组（内核在 64 位 ABI 上不实现） | 被当成"新 syscall"而报红 |

写钉子的方式（第 1 条的实例）：断言**行为**而不是常量——把选中的号在一个**子进程**里调用
（一个真的会工作的 `pivot_root` 不能挪动测试运行器的根），要求回答是 pivot_root 会给的
（`EINVAL`/`EBUSY`/`EPERM`），**绝不是** `ESRCH`（那个号是别的 syscall）或 `ENOSYS`。
在 x86_64 上 `155` 答 EPERM、`41` 答 EINVAL，两者都"像"，所以这条断言只在 generic 表架构上
真的会红——而那正是需要它的地方。

---

## 7. 不在本方案里

* 连**远程已部署实例**跑 SDK 用例：见 `docs/remote-testing.md`。
* fork wheel 的交叉构建与推送：`deploy/scripts/build-sandlock-wheels.sh`（早就出 aarch64 腿）。
* 多节点 / 模板 / 卷的远端验收：`deploy/scripts/smoke.sh`（在目标机本机跑）。
* 性能类相位（`supervise_cost`）：只在 x86_64 容器里跑，仿真 lane 上无意义。
* 历史：`tmp/arm-vm/run.sh` 是 Lima 之前手搓的 qemu 直启 lane，已被 `vm.yaml` 取代，留作参考。
