# 攻击面清单（2026-09-16）

攻击者模型：**已通过 SDK 在沙箱内执行任意代码** —— 任意命令 / PTY / 文件 API /
任意 env / 任意 cwd / 网络按策略。可读自己的整个 rootfs，可无限次尝试。

「漏点」分四类，判定标准不同：L1 逃逸（沙箱→宿主/worker）、L2 横向（沙箱→其他沙箱）、
L3 越权（沙箱→平台面）、L4 可用性（沙箱→打垮 worker/节点）。

## 分层表

| # | 层 | 关键代码 | 本轮覆盖 | 状态 |
|---|---|---|---|---|
| A | 沙箱 → 内核接口 | `third_party/sandlock/crates/sandlock-core/src/{seccomp,sys,procfs,landlock}/` | 17 类特权 syscall + 4 类 socket + docker sock | ✅ 干净（见 findings） |
| B | 沙箱 → 宿主文件系统 | chroot 中介、COW、`filesystem/ops.py`、`gateway_common/paths.py` | pure + chroot 两形态的宿主路径/`/proc`/`/sys` 读，`chroot` fallthrough 判定，image 层解压路径过滤 | ❌ **OBS-1（已修）**：`chroot(2)` 未被拦截，实测可 chroot 到宿主独有目录；已加入默认拒绝集 |
| C | 沙箱 → 其他沙箱 | `uid_pool.py`（0770 owner=sandbox uid/gid=worker）、volumes、PID ns | 双沙箱读写删互攻（10001 vs 10002） | ✅ 干净；k8s 形态见 OBS-4 |
| D | 沙箱 → 网络 | `gateway_common/network.py`、`network/rules.rs`、`network/connect.rs`、`egress/libegress_proxy.c` | 目的写法矩阵（v4/v6/mapped/link-local/metadata/短写/八进制/十六进制）× 共享 netns 与 per-sandbox netns | ❌ **SEC-001（已修）** |
| E | 沙箱 → worker 中介 | `route_b.py`、`priv_helpers.py`、seccomp 通知、fd handoff | fd 继承（两形态）、AF_NETLINK、控制通道可达性 | ✅ 干净（SL-4 族不可复现） |
| F | 沙箱 → 平台 API | `http/auth.py`、`connect/router.py`、`control_plane/api/*` | traversal、恶意 sandbox id、内部 key 空值路径 | ✅ 本轮干净；架构级无租户隔离见 OBS-6 |
| G | 可用性/资源 | `process/manager.py`（10 MiB 封顶）、`upload.py`、quota 链路 | 磁盘 / 内存 / 进程 / 输出四维实测 | ⚠️ `max_disk` 在共享 workspace 形态不生效（OBS-5） |
| H | 生命周期 | pause/resume、snapshot、migration、orphan GC | 由既有 `tests/contract/test_pause_resume_*`、`test_orphan_tree_gc.py` 覆盖 | 本轮未新增探针，沿用基线绿 |

## 已确认干净的细节（证据见 findings.md）

- **seccomp**：namespace/mount/ptrace/io_uring/keyctl/bpf/perf/userfaultfd/模块加载/重启
  等全部 EPERM；raw socket 与 AF_PACKET/AF_VSOCK 建不出来。
- **Landlock**：pure 形态是白名单（`/usr`,`/lib`,`/bin`,`/opt`+workspace），
  `/etc`、`/proc`、`/sys`、`/root`、`/app`、其他沙箱树全部拒绝。
- **每沙箱 uid**：host uid 独立（10000+），`0770` 的 group 位只对 worker 有效，
  沙箱 gid 永不等于 worker gid ⇒ 即使 Landlock 被绕过，DAC 仍是第二道墙。
- **fd**：pure 形态只有 0/1/2；route-B 形态多出的 fd 都是普通 ELF 文件，非 socket、非目录。

## 网络策略的正确读法（SEC-001 的根因）

`E2B_NETWORK_DENY_CIDRS` 只在**隐式全放行分支**生效（`allowInternetAccess=true`
且无显式 `allowOut`/`denyOut`），该分支是**拒绝清单**模型 ⇒ 清单没写到的写法即洞。
显式 `allowOut` 的用户不受影响。

**netns 不是这层的防线**：fork 的代执行（`fd_inject_connect`）在 **worker netns**
里替沙箱建连，所以 per-sandbox netns 只隔离入站/回环，出站目的地址完全由 CIDR 规则决定。

## chroot（image rootfs）形态的正确读法

**子进程的内核根是宿主 `/`，不是镜像 rootfs。** fork 在这个形态下从不调用 `chroot(2)`：
它让子进程 `chdir` 到 rootfs 内的**宿主路径**（`context.rs`），再由中介把每个被拦截的路径
syscall 翻译成 `<chroot_root>/<虚拟路径>`。`fs_mount` 同理是代执行模拟，不是子进程 mount ns
里的真 bind mount。

因此该形态的路径隔离**只有两道**：中介的翻译表（对拦截到的 syscall）+
Landlock（`landlock.rs` 只登记 chroot-前缀后的宿主路径，注释明说这是给
"any seccomp fallthrough" 兜底的 fail-closed 网）。**推论：任何不在
`chroot_path_syscalls()` 里的带路径 syscall，都会以宿主根为基准执行。**
`chroot` 就是这样一个（见 OBS-1，已修）；这条推论也应当是后续 fork 侧审计的固定检查项。
