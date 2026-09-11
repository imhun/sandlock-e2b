# 生产部署要求（XFS project quota 磁盘配额）

<!-- 本文档同时收录 E6.4 NFS 共享存储形态部署要求与实测结论（§5）。 -->

## 1. 前置条件（已确认目标机满足）

| 要求 | 目标机现状 | 达标 |
|---|---|---|
| 文件系统为 **XFS** | `/dev/nvme0n1p2` = xfs | ✅ |
| XFS 支持 project quota（`projid32bit=1`） | `projid32bit=1` | ✅ |
| 内核 ≥ 4.5 | 6.12（EL 10.2） | ✅ |
| `xfs_quota` 工具 | quota 4.09 | ✅ |
| `lsattr`（e2fsprogs）| 未装则孤儿 project 只报不清 | ⚠️ **需安装**（`_scan_project_dirs` 靠它把 projid 映射回沙箱目录）|
| 挂载启用 `prjquota` | 当前 `noquota` | ⚠️ **需启用** |

## 2. 启用步骤（需维护窗口）

### 2.1 修改 /etc/fstab

根分区条目加 `prjquota` 挂载选项：

```fstab
/dev/nvme0n1p2 / xfs defaults,prjquota 0 0
```

### 2.2 在线启用（无需重启，首选）

```bash
mount -o remount,prjquota /
```

### 2.3 验证

```bash
grep " / " /proc/mounts          # 应看到 prjquota，不再是 noquota
xfs_quota -x -c "state" /        # 应显示 Project quota enabled
```

> 回退：`mount -o remount,noquota /` + 还原 fstab。XFS 的 quota 挂载选项
> 支持在线切换，无持久副作用。

## 2.4 每沙箱 host uid（E3.2）与 route-B 槽位（2026-09-09 起为默认开）

`E2B_PER_SANDBOX_UID` 现在默认 **true**：每个沙箱从池里拿一个独立 host uid，
workspace 按该 uid chown 0700。这不是可选项式的「加强安全」，而是另外两件事的地基：

- 共享卷的跨租户保护靠真实 DAC（1777+sticky），需要写者身份互不相同；
- chroot（镜像 rootfs）形态的 route-B `sandlock-supervise` 槽位就以该 uid 运行
  （`E2B_ROUTE_B` 默认 `auto`），路径中介的代打开由此落在沙箱自己身上（T5）。

要求与影响（逐条对照）：

| 项 | 说明 |
|---|---|
| 权限 | 分三层，**照抄会多给特权**（2026-09-10 实测，逐项见 §2.4.1）：**沙箱侧最小集 = `CAP_SETUID`+`CAP_SETGID`+`CAP_CHOWN`**；`CAP_DAC_OVERRIDE` 是**管理面**需要（对账 `os.walk`、删除 `rmtree`、配额扫描都要穿租户 0700 目录）；`CAP_SYS_ADMIN` **在 A6 之后 worker 已不需要**（共享卷 bind 由 A4 删除、配额改由 quota-agent 提供、低端口 sysctl 由容器 spec 声明），也**不是 E3.2 / route B 的前置**；`CAP_SYS_PTRACE` 只在走进程内 `RunAs` 时才需要。非 root worker 自动关闭 uid 池并保持「固定身份 + Landlock」（E5.1），启动时打一条 WARNING —— 按清单 `user: "65534:65534"` 部署时行为与翻默认前**完全一致**（但线上实际是 root，见下面审计）。 |
| 容量 | 并发沙箱数受 `E2B_UID_POOL_SIZE` 约束（默认 1000，起始 `E2B_UID_POOL_START=10000`）；池满即建箱失败。多 worker 共用同一 workspace 时必须配**互不重叠**的段。 |
| 进程/内存 | chroot 形态每沙箱多一棵 supervise 进程树（supervise + sandlock-init + 停车 M0）。它在沙箱 cgroup **之外**，不计入 `max_memory`/`max_disk`，并在 `max_processes` 里占 1；容量表按「N 沙箱 = N 额外进程」重算。 |
| 回收 | route-B 代次的结束由 envd 生命周期（TTL/idle eviction/删除 → `executor.close()`）决定，不再依赖 core 的 15 min idle；槽位进程退出前该 uid 不会被再次租出（W1）。 |
| 文件系统 | uid 只对**支持属主的存储**有意义：repo 的 virtiofs bind 挂载上 chown 是 no-op，生产请用容器原生 / XFS（本项目门禁把 workspace 放 `/var/lib/e2b-sandboxes` 的 XFS+prjquota 上）。 |
| 关掉它 | 显式 `E2B_PER_SANDBOX_UID=false` 回到旧的共享 uid（1000）形态：**pure（无 base image）形态照常跑**；**chroot 形态在 root worker 上会被 fork 直接拒绝建箱**（沙箱 host uid 1000 ≠ 中介 euid 0 ⇒ `mediation_run_as=caller refused`，见下条）。非 root worker 不受影响（沙箱就用 worker 自己的 euid，中介身份与沙箱身份同一个）。E2B 曾下发的 `mediation_run_as='supervisor'` 降级档（代打开文件属主变 worker = T5）已于 2026-09-10 删除，不再有静默逃生门。 |
| 删档的后果（2026-09-10） | 「特权进程内中介 + 路径中介 + 非 0 host uid」这一组合现在只有 route B 一条路：root worker + chroot 形态若拿不到槽位（`E2B_ROUTE_B=off`、wheel 不带 supervise、或 `E2B_PER_SANDBOX_UID=false`）就**建箱失败**，并打一条 ERROR 说明为什么没有槽位、怎么修（容器实测钉在 `tests/security/test_template_isolation.py::test_in_process_chroot_is_refused_without_a_slot`）。非 root worker（E5.1，中介就是它自己的 euid）**不受影响** —— 那条组合不构成拒绝，所以也不会打这条 ERROR。 |
| **CAP_SYS_PTRACE**（进程内 RunAs 才有） | 内核要求「给子进程写 `uid_map`」除 `CAP_SETUID` 外还要对该子进程的 **ptrace 访问权**。实测 `--cap-drop ALL`：只补 `SYS_ADMIN` ⇒ 每个建箱都挂在 `sandlock_create failed`；只再补 `SYS_PTRACE` 即通。**route B 不需要它**（槽位自己就是那个 uid，自映射 `0 -> euid` 无需特权）。root worker + 非 chroot 形态开 E3.2 时，worker 启动会打 WARNING（`PER_UID_NO_PTRACE_WARNING`）说明缺哪条 cap、怎么修。 |

### 2.4.1 特权最小集（2026-09-10 实测：非特权容器 + 真 fork wheel + 真槽位；`SYS_ADMIN` 行 = 终态，A6 迁出 + A7 门禁固化，2026-09-11）

| capability | 作用在哪 | 摘掉的实测后果 |
|---|---|---|
| `SETUID` + `SETGID` | 把 route-B 槽位起在该沙箱的 host uid 上（`setpriv --reuid X --regid X --clear-groups`） | 租不到槽位 ⇒ chroot 形态被 fork 拒绝建箱（见上「删档的后果」行） |
| `CHOWN` | workspace chown 给该 uid（0700）；uid 回收时再 chown 回来 | E3.2 的属主前提不成立 |
| `DAC_OVERRIDE` | **管理面**遍历租户 0700 目录树：孤儿对账的 `os.walk`、删除的 `rmtree`、配额扫描 | `PermissionError: …/sbx_a/workspace`；对账与共享卷持久化用例 4 failed / 4 error |
| `SYS_ADMIN` | **worker 侧不需要它（A6 迁出，A7 固化）。全库只剩一处用途，且不在 worker 上**：`deploy/stack/docker-compose.prod.yml` 的 **quota-agent** 服务（`profiles: ["quota"]`）执行 `xfs_quota -x` —— 内核按 effective `CAP_SYS_ADMIN` 门控（§2.4.3）。worker 侧原来那三处：① 共享卷 `mount --bind` 进 workspace —— **A4 删 bind**（卷视图 = 请求路径决定的符号链接）+ **A5 补祖先穿透位**；② 直接执行 `xfs_quota -x` —— **A6** 改由 quota-agent 提供（worker 只发 HTTP，`E2B_QUOTA_AGENT_URL` 即开关）；③ 写 namespaced sysctl（`ip_unprivileged_port_start`）—— **A6** 改由容器 spec 声明（compose `sysctls:` / `docker --sysctl`；k8s 形态是 root pod，由 `NET_BIND_SERVICE` 覆盖）。两个显式例外都不是部署默认：不配 `E2B_QUOTA_AGENT_URL` 时的本地直连配额（dev/legacy，需 root）与 `E2B_ENABLE_NETNS=true` 的 legacy netns 形态 | 摘掉它的后果**只剩配额降级**：quota-agent 未配置/不可达 ⇒ 建箱与挂卷照常、无 per-sandbox 磁盘硬限 + 一条 WARNING。共享卷不再是理由 —— A4/A5 的契约（`tests/contract/test_shared_volume_relative_cwd.py` 等 36 条）+ 13 条穿透单测在**无 `SYS_ADMIN`** lane 三连绿（`tmp/a4-final-step4-run{1,2,3}.log`）；A7 起整份套件也在**无 `SYS_ADMIN`** 下全绿：`PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh` = `1075 passed, 3 skipped, 0 failed`（`tmp/a7-nosa.log`，cap 探针 `CapEff 0xa02c35fb → 0xa00c35fb`）。A6 的配额 lane（同形状，无 `SYS_ADMIN`）：agent 形态 `tmp/a6-agent.log` = `107 passed`；降级形态 `tmp/a6-degrade.log` = `29 passed, 5 errors`（5 个 error 是 XFS prjquota 门用例被 `E2B_TEST_STRICT_SKIPS=1` 显式暴露；A7 起 `tests/unit/test_xfs_project_quota_agent.py` 不再被 deselect，见 §2.5） |
| `SYS_PTRACE` | 只服务**进程内 RunAs**（父进程给子进程写 `uid_map` 需要对该子进程的 ptrace 访问权） | 非 route-B 的 per-uid 沙箱每个建箱挂在 `sandlock_create failed`；route B 完全不需要 |

三条实测口径（`e2b-sandlock-test` 容器，`--cap-drop ALL` + 指定 capset）：

- 无 `SYS_ADMIN`、无 `SYS_PTRACE` 下跑 route-B + chroot 沙箱：
  `created=true, guest_uid="0", mknod_rc="mknod-rc=1", blk_node_left=false, file_owner=21710`
  —— 建箱成功、客体内仍是 uid 0、**造不出也不会残留块设备节点**、写出文件属主就是该沙箱 host uid。
- worker 持 `SYS_ADMIN` 时读 route-B 槽位的 `/proc/<pid>/status`：`uid 21710, CapEff=0`
  —— **一个 cap 都没有**：`setpriv` 降 uid 会清空 effective/permitted 集，E2B 也不用
  `--ambient-caps` 往下传。所以「worker 有 SYS_ADMIN」不会顺着中介进程进入租户路径；
  它的作用域是 worker→宿主，不是沙箱→宿主。
- 同一探针里进程内后端（`E2B_ROUTE_B=off` 的 chroot、以及 pure 形态的 per-uid 沙箱）在无
  `SYS_PTRACE` 时挂在 `sandlock_instance_launch failed`，补上 ptrace 即通 —— 与 F18 的
  `PER_UID_NO_PTRACE_WARNING` 同源。

> ✅ **backlog #25 已收口（A4/A5/A6）**：上面那条「没有 `SYS_ADMIN` ⇒ 共享卷退化成不可用的
> 符号链接」的缺口不再成立 —— A4 删掉 `mount --bind`（卷视图由请求路径决定）、A5 补齐卷根
> 及祖先对租户 uid 的穿透位（§2.4.2）、A6 把配额与低端口 sysctl 也迁出 worker。现在
> **无 `SYS_ADMIN` 的部署要用共享卷不需要任何保留条件**；唯一代价是配额需要 quota-agent
> （§2.4.3），否则降级为「无 per-sandbox 硬限 + WARNING」。

**线上审计（2026-09-10，只读探测：跳板→目标机，无写入 / 不重启 / 无副作用 exec）**：仓库清单写的
`user: "65534:65534"` **不等于已部署状态** —— 远端 `/opt/sandlock/docker-compose.prod.yml`
没有 `user:` 行、worker 镜像 `0.1.0-20260830-191728` 也没有 `USER` 声明 ⇒ **线上 worker 实际
以 root 跑**，`CapEff=0xa82425fb`（Docker 默认集 + `SYS_ADMIN`，**无 `SYS_PTRACE`**）。逐项：

| 检查项 | 实测 | 判定 |
|---|---|---|
| §2.4.1 的沙箱侧最小集 | root，`SETUID`/`SETGID`/`CHOWN`/`DAC_OVERRIDE` 都在默认集里 | 满足 |
| wheel 具备 route-B 语言面 | `sandlock_supervise_connect_fd` = **False**、`sandlock/bin/sandlock-supervise` **不存在**（`setpriv` 在、Landlock ABI 6） | **不满足** |
| 多 worker uid 段不重叠 | `worker-1`/`worker-2` 共用同一 `sandbox-shared` 卷，两边都没设 `E2B_UID_POOL_START` ⇒ 都从 10000 起 | **不满足** |
| 存储支持属主 / 配额 | 卷是 xfs ✓，但挂载选项是 `noquota`，且镜像内 `xfs_quota: not found` | 部分（配额仍降级） |
| 非特权 userns（F18 自映射前置） | kernel 6.12，`user.max_user_namespaces=30519` | 满足 |
| 在线沙箱 | worker 内只有 2 个进程（python + sh）；`sbx_*` 目录多为无记录残留（属主 `0:0 755`） | 空载，适合升级窗口 |

⚠️ 现网既是 **root worker** 又配了 `E2B_BASE_IMAGE`（chroot 形态在用）还**拿不到槽位**，
正好落在上面「删档的后果」那一格里 ⇒ **先用带新 wheel 的镜像 `build-and-push`，再升代码**，
顺序反了就会出现「镜像 rootfs 沙箱全部建不出来」；uid 段也要在同一次变更里拆开。
升级顺序、待授权事项与取证脚本见 `docs/HANDOFF.md`「特权最小集实测 + 线上就绪审计」。

### 2.4.2 共享卷路径的可穿越性（证据来自 A0/A3 探针实测，A5 沿用）

沙箱的路径中介以**沙箱自己的 host uid** 打开卷的宿主路径（route-B 槽位 / RunAs），
所以从 `/` 到卷视图的**每一级目录**都必须对该 uid 有 **o+x（可穿过）**；卷根自己的
`1777` 不够。**祖先不可穿过 ⇒ 卷视图整体不可用（绝对与相对同命）**：中介把
`/workspace/<rel>` 解析回宿主卷路径后逐级打开，缺哪级就死在哪级，沙箱里只看到一句
没有上下文的 `Permission denied`。

下表来自探针 `tmp/vol_fs_mount_probe.py` 的场景 `symlink-tight-ancestor`
（**A0/A3 轮次实测，日志 `tmp/a0-probe.log`**；A5 本轮沿用该结论，**未重跑**宿主探针，
现场复现归 Track Z 的本地部署测试）：

| 宿主卷路径祖先链 | 绝对路径 `cat /workspace/mnt/data/x` | 相对 `cat mnt/data/x` |
|---|---|---|
| 逐级 `0711`/`0755` | ✅ exit 0 | ✅ exit 0 |
| 某一级 `0700` | ❌ EACCES | ❌ EACCES |

（探针原始字段：`perms="0700"`、`abs-read=1`、`rel-read=1`；控制组 `perms="0755"` 时
`abs-read=0`、`rel-read=0`。绝对路径同命的原因是两条路径最终都由中介以同一个 uid
打开同一个宿主对象，DAC 判定完全相同。）

要求与实现：

- **卷根** `1777`（sticky，跨 uid 共享）+ 属主 = 第一个挂载该卷的沙箱 host uid；
  **每沙箱切片** `0700` + 属主 = 该沙箱 host uid（语义不变，穿透补位**不**下放到切片）；
  **卷根及其全部祖先**补到 `0711` —— 原本就是 `0755`/`1777` 的更宽目录保持原样，
  只补缺的 x 位。
- E2B 行为：`provision_sandbox_volume_mount(..., host_uid=...)` 在应用卷根权限时调用
  `_ensure_traversable()` 沿 `resolve()` 祖先链补 o+x（`resolve()` 抛 `OSError`
  ——符号链接环等——时退回字面父链并只打一条 WARNING；`envd_service/volumes.py`）；
  worker 启动时用 uid 池的第一个 uid 做一次可穿透性自检，失败打一条 WARNING
  （点名目录、实际 mode、修法）。
- **部署要求**：`E2B_SHARED_VOLUME_ROOT` 的每一级祖先（例如 `/var/lib/e2b-volumes`、
  `/var/lib`）都要 `0711`/`0755`；只把卷根设成 `1777`，在缺祖先 x 位时整卷不可达。

> 历史成因：删掉 `mount --bind` 材料化之前，bind 把卷内容物理复制进 workspace
> 子路径，缺 o+x 的祖先链被绕过，症状因此被 backlog #25 **记成**「只有相对路径
> EACCES」——探针（上表）表明缺祖先 x 位时绝对路径同样不可用。A4 删 bind（虚拟路径
> 由请求决定）、A5 补穿透位之后，无 `CAP_SYS_ADMIN` 的共享卷形态才在「绝对 + 相对」
> 两个方向上都完整。

### 2.4.3 配额能力的来源：quota-agent（A6）

worker 的**本地直连** `xfs_quota -x` 需要 effective `CAP_SYS_ADMIN`（内核按 cap 门控，不看
euid），而部署形态的 worker 是 uid 65534（Docker 对非 root 清零 CapEff），本地直连在
生产里本来就不可用。A6 把链路定成**一条**：配额由服务端 **quota-agent** 提供，worker 只发
HTTP，`SYS_ADMIN` 只留在 agent 上。

- **开关是 URL**：`E2B_QUOTA_AGENT_URL` 存在即启用 agent 形态（优先于 `E2B_QUOTA_VIA_AGENT`，
  后者默认 false）。同时配 `E2B_QUOTA_AGENT_TOKEN`（与 agent 同值，`X-Internal-Key`）。
  `E2B_QUOTA_VIA_AGENT=true` 而 URL 为空仍是受支持的误配：启动打一条 WARNING、配额降级。
- **清单**：`deploy/stack/docker-compose.prod.yml` 的 `quota-agent` 服务
  （`profiles: ["quota"]`、`cap_add: SYS_ADMIN`、与 worker 挂同一份 `sandbox-shared`）——
  `docker compose --profile quota up -d` + 上面两个 env 即开启；NFS 服务器形态见
  `deploy/compose/docker-compose.quota-agent.yml` + `E2B_QUOTA_AGENT_PATH_MAP`。
  `deploy/k8s/worker.yaml` 已不再声明 `SYS_ADMIN`（把 `E2B_QUOTA_AGENT_URL` 指向集群内或
  外部的 agent；k8s 的共享卷是 RWX PVC，走 NFS 时本地直连本来就不可能）。
- **降级语义（不变）**：agent 不可达/401/协议错误 ⇒ `ProjectQuotaError` ⇒ **建箱与挂卷成功、
  无 per-sandbox 限额 + WARNING**。agent 形态不会在 worker 上执行任何本地配额命令，回归钉在
  `tests/unit/test_xfs_project_quota_agent.py::test_agent_form_never_shells_out_to_a_local_quota_tool`。
- **低端口 sysctl（第 ③ 处用途）**：wildcard allowOut 的 DNS 网关绑 `<127.0.1.x>:53`
  （`resolv.conf` 带不了端口）。Docker 形态由容器 spec 声明
  `sysctls: net.ipv4.ip_unprivileged_port_start=0` —— 运行时应用，worker 不写 sysctl、不持
  `SYS_ADMIN`；k8s 形态的 worker 是 **root**，`NET_BIND_SERVICE` 已覆盖，**不需要**这条
  sysctl。该 sysctl 自 k8s 1.22 起属 **safe sysctl**（无需 kubelet
  `--allowed-unsafe-sysctls`），只有把 pod 改成非 root 时才需要按 **pod 级**
  `spec.template.spec.securityContext.sysctls` 声明；`hostNetwork: true` 下 `net.*` 会被拒。
  MCP 入站端口是 50005+，从来不需要低端口窗口。

## 2.5 门禁容器的两种形态（别把测试特权当成生产需要）

- **`deploy/scripts/test-prod-shaped.sh`（生产形，默认推荐）**：容器不带 `--privileged`，
  `--cap-drop ALL` 后给一组部署等价 cap（Docker 默认集 + `SYS_PTRACE` `NET_ADMIN`，
  `seccomp=unconfined`）。⚠️ A6 之后清单**已不再声明 `SYS_ADMIN`**，而脚本默认仍加着它
  ⇒ 不带参数的 lane 是清单的**超集**；**无 `SYS_ADMIN` 的形态由 A7 的参数固化**：
  `PROD_DROP_CAPS=SYS_ADMIN UNPRIVILEGED_PHASE=0 ./deploy/scripts/test-prod-shaped.sh`
  （实测 `1075 passed, 3 skipped, 0 failed`，`tmp/a7-nosa.log`）。
  注意 `PROD_DROP_CAPS` 是把 cap 从 `--cap-add` 列表里**摘掉**，不只是追加 `--cap-drop`：
  本机引擎（29.4.0）`--cap-add` 压过 `--cap-drop`，与参数顺序无关
  （`--cap-drop ALL --cap-add SYS_ADMIN --cap-drop SYS_ADMIN` 的 `CapEff` 仍含 SYS_ADMIN 位；
  探针对照 `0xa02c35fb → 0xa00c35fb`）。
  实测（2026-09-10）Landlock（ABI 8）与非特权 userns 都不需要任何特权；**唯一造不出来的是
  XFS prjquota 暂存盘**（容器内 loop 设备不可用，即使 `--cap-add SYS_ADMIN` +
  `--device /dev/loop-control` 也 `failed to setup loop device`）⇒ 只有**真挂 XFS prjquota**
  的 2 个契约文件显式 `--ignore`（`tests/contract/test_volume_quota.py`、
  `tests/contract/test_xfs_project_quota.py`）；A7 把首个过宽的名单收窄，4 个配额单测
  （`test_volume_quota` / `test_xfs_project_quota_agent` / `test_quota_agent_client` /
  `test_quota_maintenance`，靠 monkeypatch 不碰真文件系统）与一个**已不存在**的
  `tests/security/test_quota_enforcement.py` 都从名单移出，A5/A6 的新用例因此回到默认门禁。
  口径更正（A5 实测、A7 记录）：`E2B_TEST_STRICT_SKIPS=1` **只**升级
  `tests/conftest.py::_STRICT_SKIP_FORBIDDEN` 的 6 个 runner 能力标记，
  普通 `pytest.mark.skipif` 在 strict 下仍是 skip；上面两个契约文件恰好用第一条标记
  （"XFS quota integration requires"），所以漏列会变 error 而不是静默少跑。
  `NET_ADMIN` 是给**夹具**用的（往 lo 上放 198.18.0.99 作为可 allow/deny 的真实源地址），
  worker 自身不需要。
- **phase 2：`--user 65534:65534` 的无特权 worker 跑法**（`UNPRIVILEGED_PHASE=0` 可跳）：
  上面那条「生产形」lane 仍是 **root**（只是把 cap 削到部署等价集），而
  `docker-compose.prod.yml` 真正写的是 `user: "65534:65534"` —— 那是**第三种形态**：
  没有 `CAP_SETUID` ⇒ uid 池自动关、租不到 route-B 槽位、沙箱就用 worker 自己的 euid
  （E5.1），路径中介留在进程内。删掉 `mediation_run_as` 降级档之后，这一形态
  必须自己站出来跑一遍：`tests/security/test_template_isolation.py` +
  `tests/security/test_sandlock_isolation.py` + 两份 route-B/policy 单测，
  实测 `47 passed / 1 skipped / 0 failed`（唯一的 skip 是那条「euid 0 才谈得上被拒」
  的钉桩）。phase 2 用 `python:3.11-slim` 作基镜像（`PHASE2_BASE_IMAGE` 可改）：
  本地构建的 `python-mcp:3.14` 镜像在 registry 镜像站的白名单外，且它的 rootfs
  缓存落在 phase 1 那套 harness 目录里，65534 写不进去。
- **原有特权跑法**：继续承担 XFS 配额全量与 loop 相关用例。
  两条 lane 的差集只应当是「配额/loop」这一类，任何别处的差集都是生产可用性缺陷。

## 3. 运维要求

### 3.1 quota 管理（E2B worker 自动执行）

```bash
# 创建沙箱
xfs_quota -x -c "project -s -p /var/lib/e2b-sandboxes/<id> <projid>" /
xfs_quota -x -c "limit -p bhard=<quota>M <projid>" /

# 删除沙箱
xfs_quota -x -c "project -C -p /var/lib/e2b-sandboxes/<id> <projid>" /
rm -rf /var/lib/e2b-sandboxes/<id>
```

- projid 范围：1..2^31-1（32 位 project id），沙箱 id 哈希分配；
- 孤儿清理：worker 启动时对账 `sandbox.json` 与 quota 表，删除无主 project；
- 监控：`xfs_quota -x -c "report -p" /` 定期巡检，超限沙箱告警。

### 3.2 兼容性注意

- **EDQUOT 而非 ENOSPC**：超限写返回 `EDQUOT`，需确认沙箱内工具能正确处理
  （大多数处理一致，个别只处理 ENOSPC 的程序要验证）；
- **内核版本锁定**：开启 project feature 后，旧内核（< 4.5）无法挂载该文件
  系统——升级/回滚内核要评估；
- **挂载选项必须配套**：`prjquota` 挂载选项与 quota feature 配套，部署脚本
  要保证两者一致（漏挂 = quota 不生效但不报错）。

### 3.3 环境矩阵

| 环境 | quota | 说明 |
|---|---|---|
| 生产（目标机 XFS） | ✅ 生效 | 本文档要求 |
| 测试机（OrbStack 容器内 XFS loop） | ✅ 生效 | 需 `--privileged` + losetup 挂载 |
| 开发机（macOS / Docker Desktop / OrbStack 默认 VM） | ⏭️ 降级跳过 | E2B 检测不到 XFS 则跳过 + 警告 |
| CI（Linux 任意 fs） | mock | 单测不依赖真实 quota |

## 4. 验收清单（部署后）

1. `grep " / " /proc/mounts` 含 `prjquota`；
2. `xfs_quota -x -c "state" /` 显示 project quota enabled；
3. 创建沙箱后 `xfs_quota -x -c "report -p" /` 能看到该沙箱的 project 与限额；
4. 沙箱内写超配额 → 命令报 `EDQUOT`/写入失败；
5. 删除沙箱后 project 记录清理；
6. 迁移沙箱（worker 间）→ 配额保留；
7. 非 XFS 环境创建沙箱 → 正常创建 + 日志警告"project quota unavailable"。

## 5. NFS 共享存储形态（E6.4 实测结论与部署要求）

多节点共享 workspace/volume 采用 NFS（或等价 CSI）时，必须满足以下
语义；实测（2026-09-02，容器内核 nfsd + XFS prjquota 导出 + 两个 NFS
客户端挂载）结论如下。

### 5.1 路径语义与迁移

- **路径语义**：同一导出挂在两个 worker 上，`volume/<sandbox_id>/` 与
  workspace 目录完全一致（两挂载点文件列表、md5 相同），worker 侧无需
  区分“本机目录”与“共享目录”。
- **迁移保留文件**：`E2B_SHARED_WORKSPACE_ROOT` / `E2B_SHARED_VOLUME_ROOT`
  下的迁移只切路由、不搬文件（keepFiles），实测 NFS 上文件与 projid
  均保留；跨挂载点读写正常（迁移目标 worker 直接读到源文件）。
- **各节点挂载选项必须一致**：`rw,sync,no_subtree_check`（及 NFSv3
  `nolock` 如无锁服务）；`vers` 与 `sec` 需一致，否则配额与权限行为
  随节点漂移。

### 5.2 配额专项（服务器端 XFS + prjquota）

配额在 NFS **服务器端**执行；客户端通过 NFS 拿到的行为：

1. **projid 继承**：服务器端 `xfs_quota project -s` 设置
   `volume/<sandbox_id>/` 后，NFS 客户端在该目录创建的文件（含子目录
   递归）projid 正确继承（实测 `lsattr -p` = 目标 projid）。
2. **EDQUOT 传播形态**：服务器端超限返回 EDQUOT，NFS 客户端表现为
   **ENOSPC（errno 28）**，不是 EDQUOT——沙箱内工具需同时处理
   ENOSPC（多数只处理 ENOSPC 的程序反而正确）。
3. **sync 挂载**（建议，E2.6 concern）：写入在达到硬限额的写调用上
   立即返回 ENOSPC（探针断言：客户端写入计数 = 限额且 errno 28；服务器端
   `stat -c %s` = 限额字节，文件恰在限额处截断）。
4. **async 挂载**（不推荐）：全部写入进入页缓存后，`close()` 与
   `fsync()` 均返回 ENOSPC（探针断言：客户端写入 `限额+8` MiB 全部成功、
   两个错误路径均 errno 28；服务器端 `stat -c %s` 落盘量**少于客户端
   写入量且不固定**——本轮实测 close.bin 22MiB / fsync.bin 0MiB，异步
   突发下服务器可能已越过硬限额——忽略延迟错误会**静默丢数据**）。生产
   必须 `sync` 挂载，且命令执行器在写路径上显式 `fsync` 并透传错误。
5. **多 worker 独立限额**：两个 worker 同时写各自
   `volume/<id>/`（不同 projid），各自独立达到硬限额，服务器端 report
   分项目记账、互不串扰（探针断言：两客户端各自写入计数 = 限额、errno 28，
   report 显示 projid 1004/1005 各自 16MiB 截断）。
6. **root_squash / uid 映射**：
   - `root_squash` 下客户端 root 映射为 nobody：projid 继承与配额记账
     不受影响（实测文件属 nobody、projid 仍继承、限额仍生效）；
   - 但**沙箱目录权限必须可写**：volume 根建议 `1777` + 每沙箱独立
     子目录（E3.2 模型），否则 squashed 写被 EACCES 拒绝（实测 755
     root 属主目录 → EACCES）；
   - **E5.1 独立 uid 模型与 root_squash 冲突**：sandbox 内 uid 0 经
     NFS 被 squash 后失去独立 uid 语义，文件统一为 nobody 属主，租户间
     NFS 文件隔离退化为“目录权限”而非“uid”。生产若开启 per-sandbox
     uid + NFS 共享卷，建议导出用 `no_root_squash`（内网可信 NFS）或
     改用 `anonuid=<per-sandbox-uid>` 映射（需 NFS 支持 per-export
     配置），并在部署验证中逐项核对。

### 5.3 部署待验证项（环境限制）

本次实测在 OrbStack 容器内内核 nfsd 完成；OrbStack 宿主自带 NFS 代理
占用标准端口（2049/20048）且状态随宿主变化，容器化 nfsd 的线程/导出
表为 VM 内核全局状态，重复自动化运行不稳定（ESTALE / 挂载竞态），因此
**以下项需在真实生产 NFS（Linux 目标机）上复核**：

- `deploy/scripts/nfs_quota_probe.sh` 在目标机 NFS 挂载点重跑全 6 项
  （A projid 继承 / B sync ENOSPC + 服务端截断 / C async close/fsync
  延迟报错 + 服务端落盘量核对（少于客户端写入） / D 多 worker 独立限额 /
  E root_squash / F 共享路径+迁移保留）；
- 生产 NFS 的 `no_root_squash` 与 per-sandbox uid 组合是否保留 uid 隔离；
- NFSv4 与 v3 在目标内核/导出配置下的行为差异（本次用 v3）；
- 配额巡检（`xfs_quota report -p`）在服务器端持续校验，与 worker
  心跳告警对齐。
