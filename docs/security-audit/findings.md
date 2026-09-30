# 沙箱逃逸深度安全审计 —— 发现（2026-09-16）

范围：L1 逃逸 / L2 横向 / L3 越权 / L4 可用性（四类全部纳入）。
方法：先静态审计攻击面，再用可复现探针在**生产形态容器**（Landlock ABI=8）里实证，
结论一律以运行时证据为准，文档只当线索。

基线见 [baseline.md](baseline.md)：`1475 passed / 3 skipped / 0 failed`。

修复后全量回归（同一 lane）：`1478 passed / 3 skipped / 0 failed`
（1475 + 新增 3 条逃逸用例），日志 `tmp/sec-regression-after-fix.log`。

---

## SEC-001（P0，已修复）默认私网拒绝清单漏 `0.0.0.0/8` 与 `::1/128` ⇒ 沙箱直达 worker 回环服务

### 机理

`allowInternetAccess=true` 且没有显式 `allowOut`/`denyOut` 时，策略是**拒绝清单模型**
（默认放行公网 − 私有网段），因此**清单没写到的写法就是洞**。

`DEFAULT_NETWORK_DENY_CIDRS` 原值：

```
10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, fd00::/8
```

缺两组：

1. **`0.0.0.0/8`** —— `connect(0.0.0.0)` 是 INADDR_ANY，Linux 把它路由到本机
   （`0.0.0.1` 同理）。`127.0.0.0/8` 拦住了 `127.0.0.1`，但拦不住这个写法。
2. **`::1/128`（及 `::/128`）** —— IPv6 回环没有任何规则。fork 的
   `IpCidr::contains` 明确「不同地址族永不匹配」（`network/rules.rs`），所以
   `127.0.0.0/8` 结构上不可能覆盖它。v4-mapped（`::ffff:127.0.0.1`）已被
   `materialize.rs::parse_ip_from_sockaddr` 的 `to_canonical()` 折成 IPv4，**是安全的**；
   纯 v6 写法不是。

### 关键点：netns 不缓解

fork 的代执行模型是 **supervisor 在 worker 网络命名空间里替沙箱建连**
（`fd_inject_connect`），所以「沙箱自己的 netns」只隔离入站/回环，
**不约束目的地址** —— 出去的连接落在 worker netns 里，`0.0.0.0` 指向的是
worker 自己的回环。实测两种形态都可达（见下表）。

### 证据

沙箱内（`tests/security/conftest.py` 的 pure 形态 + 官方 SDK 的默认网络配置）：

| 目的写法 | 修复前 | 修复后 |
|---|---|---|
| `127.0.0.1:47112`（worker 回环监听器） | 拒绝 | 拒绝 |
| `0.0.0.0:47112` | **REACHED（拿到监听器返回体）** | 拒绝 |
| `0.0.0.1:47112` | **REACHED** | 拒绝 |
| `::1:49983`（真实 envd 端口，监听器绑 `::1`） | **REACHED** | 拒绝 |

端到端（**真实 envd ASGI 应用**跑在真实端口 49983，沙箱在共享 netns 形态下发起请求）：

```
修复前: {"0.0.0.0 + internal key": "HTTP/1.1 200 OK | {\"nodeID\":null,\"address\":null,\"images\":[\"python:3.11-slim\"],"}
        {"127.0.0.1 + internal key": "ConnectionRefusedError:111"}
修复后: {"0.0.0.0 + internal key": "ConnectionRefusedError:111", ...}
```

即：**沙箱内一条 TCP 连接即可调用 worker 的内部管理 API**。`0.0.0.0` 与 `127.0.0.1`
同一端口、同一监听器，只有目的写法不同 —— 这一对照排除了「本来就没在监听」的解释。

netns 形态（`E2B_ENABLE_NET_ISOLATION=true`，compose stack 默认，2026-09-26 起
`deploy/compose` 的 prod/multinode 示例与本地池 `autoscaler/backends/local.py` 也是同一形态）
复跑同一探针，
修复前**同样 REACHED**，证实 netns 不构成缓解。

### 影响面

- 到达 worker 回环上的**任意**服务，不止 envd：redis、quota-agent、
  本地 registry/buildkit 的 TCP 端点、任何绑回环的 sidecar。
- envd 的 `/agent/*` 是**内部管理面**，只靠 `X-Internal-Key`：
  - `GET /agent/sandboxes/{id}/export` —— 把**任意沙箱**的 workspace 打成 tar.gz 流出
    ⇒ 配合可预测的 sandbox id 即为**跨租户全量读**（L2）；
  - `POST /agent/sandboxes` / `/agent/sandboxes/{id}/network` / `DELETE` —— 在 worker 上
    以平台身份增删改沙箱；
  - `POST /agent/images/{image}/warm`、`GET /agent/untrusted`、`GET /agent/logs`。
- `E2B_INTERNAL_API_KEY` 的**代码默认值是 `"internal-key"`**
  （`envd_service/config.py`），生产靠部署覆盖；compose 是 `${E2B_INTERNAL_API_KEY}`，
  `.env` 缺失时该变量存在但为空 —— 本次实测**空 key 仍是 401**（不是洞），
  但弱默认值的风险随本项一起放大。

### 修复（已落地）

1. `envd_service/config.py::DEFAULT_NETWORK_DENY_CIDRS` 扩到 15 条，补齐两个地址族：
   `0.0.0.0/8`、`100.64.0.0/10`（CGNAT）、`224.0.0.0/4`、`240.0.0.0/4`、
   `::/128`、`::1/128`、`::ffff:0:0/96`、`fe80::/10`、`ff00::/8`
   （`::ffff:0:0/96` 是纵深防御：万一将来有条路径跳过 `to_canonical()`）。
2. 四处部署默认值同步：`deploy/compose/.env.example`、`deploy/stack/.env.example`、
   `deploy/compose/docker-compose.prod.yml`、`deploy/stack/docker-compose.prod.yml`。
3. 回归用例 `tests/security/escape/test_network_deny_bypass.py`：
   - 清单自检（每个写法都要落在某条规则内 + 公网地址**不得**被误伤）；
   - 真沙箱对 worker 回环监听器的实测（五种写法全 DENIED，精确断言）。

**显式 `allowOut` 不受影响**：用户明确放行内网时照旧放行（`sandlock_network_policy`
只在隐式分支替换默认清单）。

### 部署提示

已部署实例的 `.env` 里如果**显式写了** `E2B_NETWORK_DENY_CIDRS=旧清单`，
升级不会自动改它 —— 要一并替换成新清单（`deploy/*/.env.example` 已是新值）。

---

## 已核实为**干净**的面（无逃逸）

以下都是实测结论，不是文档转述。

### L1 内核接口（pure 形态，seccomp）

`unshare(CLONE_NEW{USER,NS,PID,NET,IPC})`、`mount`、`umount2`、`pivot_root`、
`ptrace(ATTACH,1)`、`process_vm_readv(1)`、`io_uring_setup`、`keyctl`、`bpf`、
`perf_event_open`、`userfaultfd`、`init_module`、`kexec_load`、`reboot`、`acct`、
`quotactl`、`open_by_handle_at` —— **全部 EPERM**；
`AF_PACKET`/`AF_VSOCK`/`SOCK_RAW(ICMP)` 建 socket 失败；
`/var/run/docker.sock` 打不开（EACCES，Landlock）+ 连不上。

唯一放行：`socket(AF_NETLINK)` 成功（只读路由/接口信息，非逃逸；见 OBS-2）。

### L1 文件系统

pure 形态：`/etc/passwd`、`/etc/shadow`、`/proc/*`（含 `/proc/1/environ`）、
`/sys/*`、`/dev` 列目录、`/root`、`/app` —— 全部 EACCES/拒绝；
`/var/lib/e2b-sandboxes` 不可见。
只有 `/etc/hostname`、`/etc/hosts`（fork 合成的虚拟 hosts）与 `/usr` 白名单内可读。

chroot 形态：**沙箱内是 uid 0（user namespace 内 fake root），且 `chroot(2)` 返回成功**，
但仍然逃不出去 —— `/app/...` 仍 EACCES，`/proc/1/environ`、`/proc/self/root/...` 仍被拒，
`chroot("/workspace/mychroot")` 直接 ENOENT（中介按镜像 rootfs 视图解析该路径）。
详见 OBS-1（值得 fork 侧再看一眼语义，但**未构成逃逸**）。

### L1 fd 继承

pure 形态沙箱内只有 fd 0/1/2，**没有任何多出来的 socket**（SL-4 族不可复现）。
chroot/route-B 形态多出 fd 3/4/6/7，实测都是**普通 ELF 文件**（不是 socket、不是目录，
`fchdir` 全部 ENOTDIR），不构成控制通道或目录逃逸原语。

### L2 跨沙箱

两个沙箱（host uid 10001 / 10002，`0770 owner=<sandbox uid> group=<worker gid>`）：
读 / 列目录 / 写 / 删另一个沙箱 workspace 与卷内容 —— **全部 EACCES**。
两层防护同时成立：Landlock（路径白名单）+ 每沙箱独立 uid 的 DAC。

### 静态扫描

| 工具 | 结果 |
|---|---|
| `pip-audit -r requirements.txt` | 无可利用漏洞（依赖已 `==` 锁定） |
| `bandit`（envd/gateway_common/control_plane/autoscaler） | 2 HIGH，**均为误报**：`oci_registry.extract_layer` 的 `extractall` 有 `_member_is_safe` + `filter="data"` 双重过滤（bandit 看不到）；`volumes.py` 的 `0o1777` 是共享卷既定模型 |
| `semgrep`（p/python + p/security-audit，209 规则 / 93 文件） | 6 findings，**逐条核对全为误报**：2 条 logger 只打主机名/异常不打印 secret；1 条 `strcpy` 上方紧邻 `strlen(text) >= sizeof(buf)` 边界检查；3 条宽松权限同上 |
| 文档与代码一致性 | 旧文档里「未解决」的 8.1 输出缓存、8.2 上传 body、8.3 worker root、8.4 依赖锁定 **实际均已修复**（`CAPTURE_LIMIT_DEFAULT=10MiB`、`stream_body_to_file` 流式落盘、`Dockerfile.envd` 已 `USER 65534:65534`、requirements 全 `==`）——**文档滞后，需纠偏** |

---

## 观察项（未达 finding 门槛，但建议跟踪）

- **OBS-1 `chroot(2)` 是真实的 seccomp fallthrough（中，已修复）**

  > **2026-09-17 更正**：上一版把这项写成"落到 rootfs 里的空挂载点、无实际影响"，
  > 那是被**调用顺序**误导的误判。加做「每个目标在独立进程里做**第一次** chroot」的
  > 判据后，真实机理完全相反。

  **事实**：`chroot(2)` 在 fork 里完全没有被处理 —— 不在
  `seccomp_plan::chroot_path_syscalls()`，不在 dispatch 表，也不在
  `DEFAULT_BLOCKLIST_SYSCALLS`。而**模拟 chroot 形态下子进程的内核根就是宿主 `/`**：
  fork 从不调用 `chroot(2)`，它只是让子进程 `chdir` 到 rootfs 内的**宿主路径**
  （`context.rs`：*"Chroot path mediation does not exist yet at this point — seccomp is
  installed later — so the real chdir must succeed"*），再由中介把每个路径 syscall 翻译成
  `<chroot_root>/<虚拟路径>`。

  `landlock.rs` 的注释写明这套设计的前提：*"Only chroot-translated paths are added —
  host paths are NOT added, so **any seccomp fallthrough is blocked by Landlock
  (fail-closed)**."* —— 即兜底依赖"fallthrough 集合为空"。`chroot` 就是那个洞。

  **实测（每个目标各起一个独立进程，chroot 作为第一条语句）**：

  | 目标 | 修复前 | 修复后 |
  |---|---|---|
  | `/`、`/tmp`、`/etc`、`/usr`、`/workspace` | **全部 OK** | EPERM |
  | `/obs1f_hostdir`（**只在宿主根存在**的目录） | **OK** | EPERM |

  宿主独有目录可 chroot 成功 ⇒ 子进程确实是拿宿主根在解析路径。修复前它**本身不泄露数据**
  （chroot 之后每个路径 syscall 仍要么被中介按静态根翻译、要么被 Landlock 否掉），但它证明了
  fallthrough 集合非空，且它正是"把未来某个 fallthrough 变成逃逸"的那类原语 —— 一个沙箱
  没有任何正当理由需要 `chroot`。

  **修复（已落地）**：`sys/structs.rs::DEFAULT_BLOCKLIST_SYSCALLS` 加入 `"chroot"`
  （含机理注释）；fork 侧新增两条回归 —— 单元 `context::tests::test_chroot_is_blocklisted`
  （钉 resolved plan 里的 syscall 号，防止"名字没解析成号被静默丢弃"）+ 集成
  `integration::test_seccomp_enforce::test_chroot_blocked`（断言沙箱内 `chroot("/")` 得到
  **EPERM(1)**，不是"命令失败"这种弱断言）。

  **验证**：fork `cargo test -p sandlock-core --lib` = **849 passed / 0 failed**；
  chroot 相关集成用例 `-- test_chroot` = **50 passed / 0 failed**（含
  `test_chroot_write_file`、`test_fs_mount_read_write`、`test_sandbox::test_chroot`）；
  wheel 重建后 E2B 侧同一矩阵**全部 EPERM**。

  **残留（fork 侧后续）**：`chroot` 只是被发现的**一个**成员。既然兜底前提是"fallthrough 为空"，
  应当把 `chroot_path_syscalls()` 与内核里"带路径"的 syscall 全集做一次系统性差集核对
  （本轮已顺带验证 `creat`/`mknodat`/`openat2`/`faccessat2`/`xattr` 家族在当前形态下拿不到
  宿主内容），而不是继续逐个发现。

  **→ 该项已完成，并且立刻又抓到一个真洞（见 OBS-2）。**

- **OBS-2 路径面清单化：`inotify_add_watch` 实测是活的宿主信息泄露（高，chroot 形态已修）**

  把"fallthrough 为空"从散文变成机器不变量后（见下节"路径面账本"），`chroot` 之外又暴露出
  一批**未被中介、也未被拒绝**的带路径 syscall。逐条实测后，其中一条是**活的、可复现的**：

  **`inotify_add_watch` 按宿主根解析，且真的会投递宿主事件。**

  ```
  沙箱内: inotify_add_watch(fd, "/obs2_watch", IN_CREATE|IN_MODIFY) -> wd=1 (成功)
  宿主侧: 同时创建 /obs2_watch/HOST_CREATED_FILE
  沙箱内: read(inotify_fd) -> [(256, "HOST_CREATED_FILE"), (2, "HOST_CREATED_FILE")]
  ```

  即沙箱拿到了**虚拟根之外目录里的文件名与事件**。Landlock 不覆盖 `inotify_add_watch`
  （不是它 hook 的 FS 操作），所以 pure 形态同样成立（实测两形态注册都成功）。

  **生产影响**：worker 的 workspace base（`/var/lib/e2b-sandboxes`）是各租户沙箱树的父目录。
  沙箱内 `inotify_add_watch("/var/lib/e2b-sandboxes", ...)` 即可持续获知**其他租户的沙箱 id
  与其文件活动**（创建/修改/删除的文件名）—— 在"无租户隔离默认开启"（OBS-6）的部署上，
  这直接就是把跨租户的元数据暴露出去。

  **修法：选中介（保留文件监听类工作流），chroot 形态已落地**：
  - `chroot_path_syscalls()` 收进 `inotify_add_watch`；新 handler
    `chroot::dispatch::handle_chroot_inotify_add_watch` 把路径在虚拟根内解析、
    过 `can_read`，然后**以子进程身份代执行**（`dup_fd_from_pid` 复制它的 inotify fd
    后由 supervisor 注册 watch，返回 ReturnValue）。
  - 为什么是代执行而不是"改写 path 指针 + Continue"（exec 用的那招）：内核会重读子进程内存里的
    字符串，兄弟线程可以竞争改写 —— exec 那条路的竞争被 Landlock 兜住，而 Landlock 不覆盖
    inotify，所以这里必须走"内核对象而非用户内存"的路子（同 `handle_bind`）。
  - 返回的 wd 属于子进程自己的 inotify 实例（同一对象、复制 fd），它的
    `inotify_rm_watch`/`read` 与拿到手的值一致。

  **验证（E2B 侧验收）**：`tests/security/escape/test_path_surface_inotify.py`
  - 宿主独有目录：**不可监听**（修复前可监听并收到宿主文件名）；
  - 沙箱自己的 `/workspace`：**仍可监听**（修复前同样可以，但那时它监听的是宿主路径），
    并如实收到 `[[IN_CREATE, "INSIDE_FILE"], [IN_MODIFY, "INSIDE_FILE"]]`。
  - E2B `tests/security` 全量 **38 passed / 1 skipped / 1 xfailed**。

  **残留（已 pin，尚未修）**：**pure（无 chroot）形态没有中介**，只有 Landlock 一道网，
  而它对 inotify 没有访问位 ⇒ 该形态下同一条调用仍打到宿主根。
  `test_pure_shape_inotify_still_reaches_the_host_root` 用 `xfail(strict=True)` 钉住；
  修法是给 `NotifPolicy` 补上非 chroot 的 readable/writable 集合，并在"仅 Landlock"档位下
  注册一个做策略判定的变体 handler。

  同批实测的其余未中介带路径 syscall（详见账本）：`open_tree`（返回宿主目录的 O_PATH fd，
  但经由该 fd 的遍历被中介拒（openat→EACCES）、getdents64→EBADF，**未证实内容泄露**）；
  `fchmodat2`/`getxattrat`/`setxattrat`/`listxattrat`/`removexattrat`/`statmount`/`listmount`/
  `open_tree_attr`/`file_getattr`/`file_setattr`（审计内核上 **ENOSYS**，新内核上会活）；
  `creat`/`mknod(at)`/`utime(s)`/`futimesat`（Landlock 拦写，实测 EACCES）；
  `move_mount`/`fsopen`/`fsmount`/`mount_setattr`/`fspick`/`fanotify_mark`
  （CAP 拦截，实测 EPERM）；`uselib`/`mq_open`/`mq_unlink`（ENOSYS / mqueue 未挂载）。

  > **2026-09-17 更正（重要，影响两处归因）**
  >
  > ① **"审计内核上 ENOSYS"是错的说法。** 内核**实现了**这些 syscall（452、463–466、
  > 457–458、468–469）；返回 ENOSYS 的是**部署用的 worker seccomp profile** —— 它的默认
  > 动作拒掉 allow 列表之外的一切。同一台机同一个调用实测：
  > `deploy/seccomp/sandlock-worker.json` 下 **ENOSYS(38)**，`seccomp=unconfined` 下
  > **EINVAL(22)**（真的到达内核）。**seccomp 过滤器是叠加的，沙箱继承外层**，所以这 9 条
  > 在**部署形态下根本不可达**，只有在"worker 跑更宽 profile"的宿主上才可达。结论方向不变
  > （要**中介**而非拒绝 —— glibc 已在用 `fchmodat2`，拒绝会表现为"工作负载坏了"而不是
  > "攻击被挡"），但**暴露面比我先前写的窄**。
  >
  > ② **`open_tree` 那条是在比生产更宽的测量环境里测到的。** 探针 runner 复刻的是 lane 的
  > **能力超集**（带 `SYS_ADMIN`），而生产 A6 之后没有 SYS_ADMIN —— profile 里 `open_tree`
  > 恰属"仅当容器带 CAP_SYS_ADMIN 才 allow"那一组。实测到的是**更宽**的形态，所以结论偏保守
  > （测到的是最坏情况），但"部署形态下可达"对 `open_tree` 并不成立。
  >
  > 已按"新 mount API 与 `mount` 同类、Landlock 覆盖不到"把 `open_tree`/`open_tree_attr`
  > 加进沙箱默认拒绝集（fork `2666ce5`）作为外层 profile 缺失时的兜底；其余 9 条保留 `Open`
  > 并改写理由为上述真实机制。

### 路径面账本（fork 侧已落地，把"清单完整"变成不变量）

- **OBS-7 pure（无 chroot）形态的缺口是"一类"而不是一条（中，已定性，未修）**

  N14 讨论时顺带做的判定：pure 形态没有 rootfs、也没有任何路径中介，**只有 Landlock 一道网**，
  而 Landlock 的访问位是闭集（execute / read / write / remove / make / refer / truncate /
  ioctl-dev）。凡是"带路径但不在这个集合里"的 syscall，在 pure 形态下**没有任何机制拦**。
  实测（同一沙箱、同一宿主文件，`/obs5_host_xattr`，0644，宿主写了 `user.obs5`）：

  | 调用 | pure 形态结果 | 为什么 |
  |---|---|---|
  | `openat(path, O_RDONLY)` | **EACCES** | 落在 Landlock 的 `READ_FILE` 访问位上 → 被拦 |
  | `getxattr(path, "user.obs5")` | **成功，读回 `XATTR-FROM-HOST`** | Landlock 没有 xattr 访问位 → 无人可拦 |
  | `open_tree(path)` | **返回 fd** | 同上；且这条不经任何中介 |
  | `inotify_add_watch(path)` | **成功并投递宿主事件**（OBS-2） | 同上 |

  所以纯靠"逐条把 inotify 补上"是在追尾巴：同一类的成员还有 `getxattr` 家族、`open_tree`
  家族，以及将来内核新增的同类。**要一次关掉这一类，只有两条路**：
  ① 给 pure 形态也造一个"真根"（= N14 的扩展形态，见下）；② 在 pure 形态对
  "Landlock 覆盖不到的带路径 syscall"做统一闸门。账本应当是这两条路共同的事实源。

  **账本项目前的盲点（需补）**：`MEDIATED_PATH_SYSCALLS` 的"Mediated"是**按 chroot 形态**说的 ——
  pure 形态下这些 syscall 根本不在拦截列表里（没有 chroot feature 就没有这些 handler），
  它们靠 Landlock 兜。同一张表里，`openat` 在 pure 被 Landlock 拦住、`getxattr` 不被拦，
  账本现在**区分不了这两者**。需要给每条加一个 pure 形态维度
  （`Landlock-gated` / `Open` / `Blocked`），否则"这个形态下哪些没人管"仍然要人肉推。

  **→ 该盲点已补（2026-09-17），并且算出来的数字比预期大得多：pure 形态的未闸门集合是 35 条，不是 1 条。**

  账本现在按形态分类，三张清单 **恰好划分**路径面（用例 `pure_shape_classifies_every_path_taking_syscall`
  钉住这个划分）：

  | 清单 | 条数 | 含义 |
  |---|---|---|
  | `PURE_LANDLOCK_GATED` | 25 | Landlock 自己就拒（open/exec/mkdir/unlink/rename/link/truncate/getdents/mknod/creat） |
  | `PURE_GATED_ELSEWHERE` | 14 | 非 Landlock 但有内核侧闸门（capability / 废弃 / 全形态 blocklist） |
  | **`PURE_UNGATED`** | **35** | **没有任何闸门**（`pure_shape_ungated_set_is_pinned` 逐个钉住） |

  实测（宿主目录 0755、文件 0644，沙箱 uid 可穿越）：
  `openat` → **EACCES**（Landlock 的 READ_FILE/READ_DIR 生效），而
  `statx` → **OK**、`faccessat` → **OK**、`readlinkat` → 路径被解析（EINVAL=不是链接）、
  `listxattr` → **OK**、`getxattr` → 触达 inode（ENODATA）。

  35 条的构成：元数据族（`stat`/`lstat`/`newfstatat`/`statx`/`statfs`）、存在性探测
  （`access`/`faccessat`/`faccessat2`）、`readlink(at)`、`chdir`/`fchdir`/`getcwd`、
  `chmod`/`fchmodat`、时间戳族（`utime`/`utimes`/`futimesat`/`utimensat`）、
  **全部 8 条 xattr**、`inotify_add_watch`、`open_tree`，以及 5 条在新内核才会活的
  at 风格调用（`fchmodat2`、4 条 `*xattrat`）。

  **这暴露的是宿主"元数据"**：存在性、大小、时间戳、inode、符号链接目标、xattr 名与值，
  外加目录变更事件 —— 即"读不到内容但能知道它存在、多大、何时变过"。这是 pure 形态**真实的
  隔离边界**，也是"该形态不适合承载互不该知情的租户"的直接依据。chroot 形态不受影响。

`crates/sandlock-core/src/sys/path_surface.rs` —— 对每个能命名虚拟根之外对象的 syscall 记录
处置（`Blocked` / `Gated(理由)` / `Open(理由)`），并由 5 条用例把三条不变量钉死：

1. `MEDIATED_PATH_SYSCALLS` 与 `chroot_path_syscalls()` **双向相等** ⇒ 拦截清单不能悄悄增删；
2. 每个 ledger 名字都要在本架构解析得出来 ⇒ 错别字不会静默通过；
3. **任何 `syscalls` crate 知道的 syscall 都必须被分类**（中介 / 拒绝 / 其他有理由的处置 /
   无路径参数）⇒ 新内核新 syscall 会让测试变红，而不是悄悄扩大 fallthrough 集合。

`Open` 是诚实的"待决"桶：当前 12 条，`open_items_are_enumerated_and_reasoned` 把这个集合
**逐个钉住**，消除一条就必须同步改 pin，不能悄悄滑过去。

**验证**：fork `cargo test -p sandlock-core --lib` = **854 passed / 0 failed**（较上一轮 +5）。
**守卫非空转**已实测：故意删掉一个 syscall 的"无路径"标注 ⇒ `every_arch_syscall_is_classified`
变红；故意给中介清单塞一个不存在的名字 ⇒ `mediated_set_matches_the_ledger` 与
`every_ledger_name_resolves_on_this_arch` 同时变红。
- **OBS-2 AF_NETLINK 放行（低）**：可枚举接口/路由（信息面），非逃逸；若不需要可在 seccomp 档收掉。
- **OBS-3 k8s 清单未开 netns/pid_ns（中，已登记 N5/N10）**：与 compose stack 形态不一致。
  SEC-001 修复后这条不再等价于「可达 worker 管理面」，但形态漂移本身仍是风险源。
- **OBS-4 k8s 多副本共享 RWX 时 uid 池重叠（中，已登记 N13）**：跨 uid 保护会退化成同 uid。
  仍是最该先修的 k8s 侧缺口。
- **OBS-5 `max_disk` 在共享 workspace 形态下不生效（中，已知）**：实测 64 MiB 上限下写入
  192 MiB 无任何拒绝。生产靠 **XFS prjquota**（目标机已验证 `hard_blocks=1GiB`）兜底，
  所以这是「形态依赖」而非普遍缺口；非 XFS 部署（含本地容器 lane）没有硬上限。
- **OBS-6 租户隔离已有实现、但默认关闭（高，配置项非代码项）**：
  **更正上一版措辞** —— 这不是"架构级缺失"。E3.1 的租户模型已完整落地
  （`control_plane/auth.py` 的 `tenant_of` / `_require_owned` / `_require_related`，
  `E2B_TENANTS` + `E2B_ADMIN_API_KEYS`，`tenant_id` 归属字段，按租户限流/配额，
  `deploy/scripts/migrate-tenants.py` 存量迁移脚本，contract 用例
  `tests/contract/test_tenant_isolation.py`）。**真正的缺口是默认值**：
  `E2B_TENANTS` 未配置即"单租户兼容模式"，出厂 env 示例里根本没有这个变量。

  两 key 实测（同一个控制面，仅改 `E2B_TENANTS`）：

  | 动作（key B 对 key A 的资源） | `E2B_TENANTS` 未设（出厂默认） | 配置后 |
  |---|---|---|
  | 列举沙箱 | 看到 A 的沙箱 | `[]` |
  | 读 A 的沙箱详情 | 200 | 404 |
  | 读 A 的 volume | 200 | 404 |
  | **kill A 的沙箱** | **204** | 404 |

  即：默认部署下任一 API key 可列/读/删/驱动全部沙箱与卷。与 SEC-001 叠加时后果放大
  （内部 key 暴露 ⇒ 全量导出）。**修法是部署配置，不是改代码**：设
  `E2B_TENANTS` + `E2B_ADMIN_API_KEYS`（口径见 `docs/tenant-isolation.md`），
  存量资源需要跑一次 `migrate-tenants.py`。

  另外实测到一条纵深防御缺口：**envd 数据面不参与租户校验**（只校验
  `E2b-Sandbox-Id` + `X-Access-Token`）。上表"驱动 A 的 envd"在开启租户后仍是 204
  —— 因为请求带的是 A 的 access token。控制面列表已被过滤，token 拿不到，
  所以不构成越权；但 envd 侧没有第二道租户校验，属于纵深防御可加固项。

  **结论（2026-09-22，用户决定）：暂不做多租户。** 即保持"单租户兼容模式"的出厂默认，
  不往部署清单里加 `E2B_TENANTS`/`E2B_ADMIN_API_KEYS`，也不跑存量迁移。因此上表右列
  那种隔离**目前是有意不启用的**（不是没做）。这条决定带来两条必须记住的后果：
  * **只要部署是单租户，它就成立**：一旦有第二个使用者拿到任一 API key，默认语义
    就是"可列/读/删/驱动全部沙箱与卷"——对外开 API 之前必须回到这条；
  * envd 数据面的纵深防御缺口（上一段）在这个前提下**不构成越权**（token 拿不到），
    所以它随这条决定一起搁置。
  **触发条件（任一即回到这条，并且是部署配置 + 一次迁移，不是改代码）**：开放第二个
  使用者/租户；把 API 给第三方或跨团队共享；把 `sandbox_id` 或 access token 暴露给
  非资源所有者。届时按 `docs/tenant-isolation.md` 设两个变量并跑 `migrate-tenants.py`。

- **OBS-9 共享卷是单一信任域：集群内任意 root 可写任意沙箱树（中，2026-09-18 实测，
  超出本轮攻击者模型）**

  本轮攻击者模型是"已能在沙箱内执行任意代码"，而**对那个模型这不算洞**（C 层已干净：
  host uid 独立、Landlock 白名单、`0770` 只给 worker gid）。真正的缺口在**集群内 root**：

  * 实测现状：`deploy/k8s-k0s/storage-nas.yaml` 的 `: /sandlock` 被**控制面与两个 worker
    都以 root 挂载**，两个节点分别实测 `WRITE OK as 0:0` / `WRITE OK as 0:65534`，
    且卷根是 `drwxrwxrwt`（1777）⇒ **机器级没有任何边界**，只有进程级；
  * 因此"跨机写"不是理论：任何能在这台卷上拿到 root 的东西（任一挂载它的 pod、被攻破的
    worker、误操作）都能改任何沙箱的树；
  * **有一条具体可利用链**：OBS-4 的修法是让 fleet 级 uid 分配以**卷内文件**
    `sandbox.json` 的 `host_uid` 为权威（`uid_pool.py:_recorded_uids`，注释明说
    "on any worker sharing the workspace"），而该文件属主是 `0:65534`（root 可改）
    ⇒ 改写它可让两个沙箱**共用同一个 host uid**，从而**拆掉第二道墙**（跨 uid 隔离）。
    同一个文件也是 teardown 侧"沙箱可改写"的输入（那边已按名字/根做校验，分配侧还没有）；
  * **不能简单开 root squash**：E3.2 的每沙箱 uid 需要 worker 对卷执行 `chown`
    （`uid_pool.py:apply_sandbox_ownership` → `priv_helpers.chown` / `_chown_tree`），
    而 squash 是**服务端**行为，客户端再特权也会被映射成 nobody ⇒ 直接 EPERM。
    "卷上 root squash" 与 "卷上每沙箱 host uid" 目前**互斥**；
  * **建议**（按性价比）：① 把 fleet 级不变量（`host_uid`、卷切片归属）的权威搬到**卷外**
    （`SandboxRecord` / Redis），卷内文件降级为缓存并做一致性校验；② 控制面改最小权限挂载
    （`: /sandlock` 只读 + 仅 `_builds` 可写，worker 保持 RW）；③ 在已有的周期性扫描里加
    **uid 审计**（树内出现非本沙箱 uid、或非 worker gid 的属主即告警）；④ 在 NAS 控制台
    确认权限组与"是否有别的信任域也挂着同一路径"——这一条决定它是"缺纵深防御"还是
    "对外可写的真漏洞"。

  **① 与 ② 已落地（2026-09-18，`0.1.0-367-gca4a602`），并在集群上验证：**
（补充：同日稍后又落地了 **平台文件与用户 workspace 的分离**，见下面最后一条——它把"沙箱能改写自己记录"这条根因也切断了。）

  * **② 控制面最小权限挂载**：`deploy/k8s/control-plane.yaml` 里 `: /sandlock` 改为
    `readOnly`，控制面真正要写的六个目录用 **`subPath` 挂回来 RW**：
    `_builds`（模板构建）、`_images`（OCI 导出）、`_secrets`、`_templates`、`_snapshots`、
    `_volumes`；这些目录由 worker 的 `workspace-root-init` 预建（subPath 源不存在会让 pod 停在
    ContainerCreating，kubelet 会重试，所以首次安装会自愈）。
    实测（控制面 pod 内）：写已存在沙箱树 `sbx_…` 与写根层新名字**都是 EROFS**；
    六个目录全部可写。顺带修掉一个真回归：**快照记录原本落在根层的 `snap_<id>`**
    （而 worker 的 `fs/` 在 `_snapshots/<id>/fs`——同一个快照两套路径），现改为记录与 payload
    同目录 `_snapshots/<id>/`，读取仍兼容旧布局、删除两者都清；快照 create/restore/delete 实测通过。
  * **① uid 权威搬到卷外**：新增 `RedisUidLedger`（`HSETNX` 逐 uid 认领 + 反向索引，无 Redis 时等价
    内存实现），`SandboxRegistry.allocate_host_uid()/release_host_uid()`，`host_uid` 成为
    `SandboxRecord` 的持久字段；建箱前分配并随 `hostUID` 下发给 worker，
    worker `UidPool.claim()` **按给定值采用**，只有拿不到 uid（旧控制面/旧记录）才回落到它自己的
    磁盘扫描分配（也正因如此，非 per-uid 部署不会白白耗尽池）；释放挂在 `_release` 上
    （驱逐、TTL、所有回滚路径都走它）。
    **集群实测这条链**：A→10000、B→10001；把 A 的 `sandbox.json` 伪造成占用 **10002**（正是
    OBS-9 的攻击面）→ 新建 C **仍然拿到 10002**（旧扫描式分配器会跳过去给 10003）；
    kill A/C 后新建 D 拿到 **10000**（释放真的回到池里，不是每个沙箱漏一个 uid）。
  * 仍未做：**③ uid 审计**与**④ NAS 权限组确认**；另外"**卷切片归属**"这一半的权威仍在卷内
    （`volume_projects` 由 worker 写在 `sandbox.json`，teardown 侧已有名字/根校验，分配侧还没有）。
  * **根因已切断（同日稍后，`0.1.0-368-g65b1747`）**：实测沙箱**拥有自己树的目录**（`0770`），
    所以它能 `rm sandbox.json` 再写一份自己的（0644 只挡住"原地改"）。平台文件（record + 命令日志）
    已搬到 `<base>/_runtime/<id>/`（`0700`，属主 = worker）：沙箱实测 **读/写都 Permission denied**，
    而自己的 workspace 正常可写。对本文的意义：**"沙箱能改写自己记录"这条前提不再成立**，
    teardown 侧的 W7/C2 校验从"必需"降级为"纵深防御"；`volume_projects` 将来落到 `_runtime/`
    即天然不可被沙箱改写。

## L4 实测：哪些上限是真的

- **OBS-8 资源创建类端点只有沙箱创建有限流（中，已修）**

  旧 hardening 清单第 5 条写的是"模板构建无资源限额"，本轮复查发现**范围更大**：限流只落在
  **沙箱创建**（`sandboxes.py`）与**模板构建**（`templates.py`）上，而
  `POST /sandboxes/{id}/snapshots`（**复制整个沙箱文件系统**）与 `POST /volumes`
  （分配 quota slice + 目录树）**完全没有限制** —— 一个认证 key 可以零成本循环它们，
  而同一个 key 的沙箱创建早就被限了。

  **修复**：三个"资源创建"端点统一走同一套准入
  （`control_plane/ratelimit.py::enforce_resource_limit`：先按 key 的滑窗，再按租户），
  限流器**按端点独立**（一次快照风暴不花掉沙箱创建的预算），默认值与沙箱创建同为
  `DEFAULT_CREATE_RATE_LIMIT_PER_MIN = 120`（一个常量，三者不会漂移），各自可用
  `E2B_SNAPSHOT_RATE_LIMIT_PER_MIN` / `E2B_VOLUME_RATE_LIMIT_PER_MIN` 覆盖，
  按仓库惯例 `0` 关闭。测试夹具（`tests/conftest.py` 三处）与既有的
  `create_rate_limit_per_min=0` 一并置零，避免长会话累计触发 429。

  **验收**：`tests/contract/test_resource_create_limits.py` 4 条 —— 两个端点各自 429、
  预算按端点独立（快照风暴不影响沙箱创建）、`0` 关闭且默认值 = 创建预算。

## INFRA-1（非安全项，已修）非 root worker 形态的验收阶段根本无法运行

`deploy/scripts/test-prod-shaped.sh` 的第二阶段（"unprivileged worker (uid 65534 + the
file-capability brokers)"）在**任何镜像上**都会全量报错，与本次改动无关：

```
ERROR at setup of test_create_permission_error_is_500_with_reason
  tests/conftest.py:540: workspace fixture
  os.mkdir('/var/lib/e2b-test-runtime/458ee825c01d')
  PermissionError: [Errno 13] Permission denied
```

根因：`Dockerfile.test-runner` 里 `ENV E2B_TEST_TMP_ROOT=/var/lib/e2b-test-runtime`
且该目录由 **root 建为 0755**，而 phase 2 用 `--user 65534:65534` 跑整套 ⇒ 每个需要
workspace 的 fixture 都在 setup 期就崩。实测在**未改动的旧镜像**上一样：

```
$ docker run --rm --user 65534:65534 --entrypoint sh e2b-sandlock-test:latest \
    -c 'mkdir -p /var/lib/e2b-test-runtime/x'
mkdir: cannot create directory ...: Permission denied
```

即 phase 2 从来不是"红的"，而是**跑不起来**——这本身是保证面上的缺口，因为非 root worker
是两个生产清单都发的形态之一。把 `E2B_TEST_TMP_ROOT` 指到可写目录后，该形态**全绿**：
`51 passed / 1 skipped`（`tests/security/test_template_isolation.py`、
`test_sandlock_isolation.py`、`test_sandlock_executor_route_b.py`、
`test_policy_mapping.py`、`test_nonroot_route_b.py`）。

**修复**：`Dockerfile.test-runner` 的 `mkdir` 后加 `chown 65534:65534`
（与 worker 镜像对 `/var/lib/e2b-sandboxes` 的做法一致）。

| 维度 | 结论 |
|---|---|
| 内存（`max_memory`） | **真**：256 MiB 上限下进程被 SIGKILL（探针在 128 MiB 处终止） |
| 进程数（`max_processes`） | **真**：上限 24 时第 24 个 fork 返回 EAGAIN（实测 `FORK ERR 11 after 23`） |
| 磁盘（`max_disk`） | 共享 workspace 形态下 **不生效**（见 OBS-5），依赖 XFS prjquota 兜底 |
| 命令输出 | worker 侧 10 MiB 封顶（E4.1，`CAPTURE_LIMIT_DEFAULT`），非本轮改动 |

---

## 目标环境复验清单（本地容器只是第一道，最终以目标为准）

本地是 OrbStack 容器（Landlock ABI=8）。两条生产形态都要复跑，**结论分开记**：

1. **形态确认**（先做，否则复验没有意义）：
   ```bash
   # compose stack：默认已开 netns/pid_ns
   docker compose -f deploy/stack/docker-compose.prod.yml config | grep -E "NET_ISOLATION|PID_NS|NETWORK_DENY_CIDRS"
   # k8s：清单目前两者都没开（OBS-3）
   kubectl -n <ns> get deploy <worker> -o yaml | grep -E "E2B_ENABLE_NET_ISOLATION|E2B_PID_NS|E2B_NETWORK_DENY_CIDRS"
   ```
2. **`.env` 清单替换**：已部署实例若显式写了旧 `E2B_NETWORK_DENY_CIDRS`，
   升级不会自动改；改成 `deploy/stack/.env.example` 的值再滚。
3. **逃逸探针**：把 `tests/security/escape/` 跑到目标形态上
   ```bash
   E2B_REGISTRY_MIRRORS=... E2B_BASE_IMAGE=... UNPRIVILEGED_PHASE=1 \
     ./deploy/scripts/test-prod-shaped.sh tests/security/escape
   ```
4. **SEC-001 端到端复现**：目标机上从沙箱内 `connect(0.0.0.0:<envd_port>)` 与
   `connect(::1:<envd_port>)` 必须被拒；同时确认 `0.0.0.0` 打不通任何回环服务
   （redis / quota-agent / registry）。
5. **`cargo-audit`**：在能访问 RustSec advisory-db 的环境对
   `third_party/sandlock/Cargo.lock`（253 crate）跑一次。

---

## 2026-09-30 加固：第一档危险 syscall 禁用（k0s 集群实测）

### 怎么测的

探针 `tmp/syscall-probe/probe.py`（探针自带两套 ABI 的 syscall 号表，取自
sandlock 自己解析黑名单用的 `syscalls` crate —— worker 镜像没有内核头文件），
每条候选都用**故意非法的参数**调用，因此"到达内核"与"被 seccomp 拒"可以区分：

* `ENOSYS` ⇒ 外层 worker profile 的默认动作（该 profile 未列出的 syscall 一律 ENOSYS）；
* 其它 errno（`EINVAL`/`EFAULT`/`EBADF`/`ESRCH`，或返回 ≥0）⇒ **到达内核**；
* 列 `EPERM` 时要靠"worker 进程 vs 沙箱内"两列的差异判定是不是内层黑名单干的。

三列对照：① unconfined 容器内的裸调用（内核真值）② worker profile 容器内的裸调用
（外层）③ 真沙箱内（两层叠加）。**部署形态的结论来自 k0s 集群上的真 worker pod**
（`e2b-worker-1`，arm64，`uid=65534`，`CapBnd/Eff=0`，`Seccomp: 2`，
`kubectl exec` 进 pod 后 `python3 /tmp/probe/probe.py sandbox <前缀>`）。
本地 OrbStack 只在 unconfined 容器里跑通了裸调用两列：带沙箱的那次把本地 VM 弄挂
两次（`orb start` 恢复），所以部署形态一律以集群为准。

### 实测：第一档候选在**沙箱内**能到内核（外层也放行）的

| 调用（沙箱内） | 结果 | 说明 |
|---|---|---|
| `fsconfig` / `mount_setattr` | `EINVAL` | **参数解析跑到了**，说明确实进了内核；不是 cap 拦截 |
| `fsopen` / `fsmount` / `move_mount` / `fspick` | `EPERM` | 停在内核的 `may_mount()` 能力检查上，即内层没拦 |
| `process_madvise` / `process_mrelease` | `EBADF` | 与已禁的 `process_vm_readv/writev`、`pidfd_getfd` 同类 |
| `kcmp` | `ESRCH` | 同上（PTRACE_MODE_READ 那一类） |
| `quotactl_fd` | `EBADF` | worker 侧的配额实现（`envd_service/xfs_quota.py`）在过滤器之外，沙箱不需要 |
| `kexec_file_load` | `EPERM` | 停在 `CAP_SYS_BOOT`；`kexec_load` 早已禁用 |
| `io_setup` / `io_submit` | `EFAULT` / `EINVAL` | 旧 POSIX AIO，`io_uring` 的上一代 |
| `memfd_secret` | **`ok(fd=3)`** | 沙箱内真的拿到了句柄 |
| `get_mempolicy` / `set_mempolicy` | **`ok(0)`** | 非能力门控；`get_mempolicy` 还会泄露宿主 NUMA 拓扑 |
| `mbind` / `move_pages` / `migrate_pages` | `EFAULT`/`EPERM` | 同一族 |

对照组（内层黑名单**确实**拦下的，说明这套测法有效）：`mount`/`umount2`/`open_tree`
（worker 侧 `EFAULT` ⇒ 沙箱内 `EPERM`）、`chroot`、`bpf`、`setns`、`perf_event_open`、
`pidfd_getfd`、`process_vm_readv/writev`、`quotactl`、`open_by_handle_at`、
`name_to_handle_at`。

### 改了什么

* `sys/structs.rs::DEFAULT_BLOCKLIST_SYSCALLS` 新增 26 条：mount API 全家
  （`fsopen`/`fsconfig`/`fsmount`/`move_mount`/`fspick`/`mount_setattr`/`statmount`/`listmount`）、
  ptrace 类（`process_madvise`/`process_mrelease`/`kcmp`）、`quotactl_fd`、
  `kexec_file_load`、旧 AIO（`io_setup`/`io_submit`/`io_cancel`/`io_getevents`/`io_pgetevents`）、
  `memfd_secret`、`modify_ldt`（x86-only，arm64 上自动跳过）、NUMA 六条。
* `sys/path_surface.rs` 账本：`statmount`/`listmount` 从 `Open`、`move_mount`/`fspick`/
  `mount_setattr` 从 `Gated` 改为 `Blocked`，`Open`（待决策）集合收敛到 7 条
  （`fchmodat2` + 4 个 `*xattrat` + `file_getattr`/`file_setattr` —— 这几条按账本原判
  **要中介不要拒绝**，glibc 已经在用 `fchmodat2`）。
* 回归：`context::tests::test_first_tier_hardening_syscalls_are_blocklisted`（名字→号钉进
  解析后的计划）与 `integration/test_seccomp_enforce.rs::test_first_tier_blocklist_refused`
  （真沙箱内逐条断言 `EPERM(1)`，26 条）。

验证数字：`cargo test -p sandlock-core --lib` **914 passed / 0 failed**（需
`--privileged --security-opt seccomp=unconfined`，否则两条用例因 PID namespace /
`pidfd` 跨进程权限而在容器里必然失败）；`--test integration` 与改动前
`a21a507` 基线逐条 diff：**新增失败 0**（基线 27 条环境性失败，改动后 26 条）。

### 同批发现、**未**在本轮修的两个问题

1. **`clone3` 的命名空间位没有任何一层 sandlock 校验**。BPF 参数过滤只对
   `SYS_clone` 发 `JSET CLONE_NS_FLAGS`（`seccomp_plan.rs` 的 `nr_clone` 一路），
   `resource.rs::handle_fork` 也只检查 `nr == SYS_clone`，而那里的注释写着"clone3 由
   BPF 参数过滤兜住"——并不成立。`clone_flags()` 已经会读 `clone_args`，所以修法是
   在 `handle_fork` 里对 `clone3` 复用同一个判定。部署形态当下打不穿：外层 profile
   把 `clone3` 整条 deny 成 `ENOSYS`（本轮实测确认），也就是这条禁令实际由容器
   profile 承担，而不是 sandlock 自己。不能改成"禁用 `clone3`"——glibc 2.34+ 的
   `pthread_create` 走它。
2. **`deploy/seccomp/README.md` 的"变更表"与 profile 现状不符**：`mount`/
   `pivot_root`/`umount2` 在文件里是**无条件 allow**（N35 真根那次加的），README 却写
   "mount … 未放宽"。已在该 README 就地更正，并补上"同一个文件在不同引擎上对
   `caps:` 条件的解析不一致（`fsconfig` 一边被拒一边到内核）"这条实测 —— 结论是
   mount API 不能指望外层 profile 兜底，只能靠沙箱自己的黑名单。
