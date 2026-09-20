# k8s 部署指南（upgraded 2026-09-16）

本文是 `deploy/k8s/` 这套清单的部署指南，与 `deploy/scripts/README.md`（compose/目标机
那条线）并列。**读之前先知道两件事**：

1. **compose 才是当前线上形态**（`deploy/stack/docker-compose.prod.yml`，目标机
   `172.18.80.140`，两 worker，netns + bind 注入 + pid_ns + seccomp 自检 + 配额 agent 都已上线）。
   k8s 清单**没有在真实集群验证过**，本文把"哪些是已验证事实、哪些是待验证"逐条标出来。
2. k8s 与 compose **故意不是同一形态**：netns、pid_ns、配额口径、卷与身份池都不同。差异表见
   §3，切换项见 §5。不要假设 compose 的结论能直接搬过来。

---

## 1. 清单与拓扑

| 文件 | 内容 | 说明 |
|---|---|---|
| `namespace.yaml` | Namespace `sandlock` | 所有资源的家 |
| `pvc.yaml` | PVC `sandbox-shared`（RWX，50Gi） | **必须 RWX**（NFS/CephFS）：所有 worker 与两个 control-plane 副本共享它；沙箱工作区、`_images` 缓存、卷切片都在里面 |
| `redis.yaml` | redis Deployment + Service | **默认无认证**（见 §4 密钥） |
| `control-plane.yaml` | control-plane Deployment（2 副本，合并镜像，`:3000`） | 内含 envd gateway；root 运行；`E2B_ENABLE_LOCAL_NODE=false`；initContainer 建/验 `_images` 属主 |
| `gateway.yaml` | Service `gateway`（49983 → 3000） | 保住 compose 时代的 DNS/入口契约 |
| `k8s-k0s/gateway-nodeport.yaml` | Service `gateway-nodeport`（**NodePort 31907** → 3000） | **只在自建集群的 overlay 里**：托管集群由 SLB/ingress 承担同一角色，这里没有 LB，所以用固定 NodePort 给集群外一个不漂的入口（访问方式见 `deploy/k8s-k0s/README.md`） |
| `worker.yaml` | worker Deployment（1 副本）+ headless Service + PDB | 非 root（镜像自带 `USER 65534`）+ 4 个 file-capability broker 所需 cap + pod 级低端口 sysctl + `Localhost` seccomp profile |
| `autoscaler.yaml` | autoscaler（SA/Role/RoleBinding + Deployment） | `E2B_AS_BACKEND=k8s`，直接 scale `e2b-worker`，`MIN=1 / MAX=16` |
| `seccomp-installer.yaml` | ConfigMap `sandlock-worker-seccomp` + DaemonSet `seccomp-installer` | 把 `deploy/seccomp/sandlock-worker.json` 写到**每个节点的** `/var/lib/kubelet/seccomp/sandlock-worker.json` |

节点要求：

* kubelet 的 seccomp 根可写（DaemonSet 用 `hostPath: /var/lib/kubelet/seccomp`；kubelet 换了
  `--seccomp-root` 就要同步改清单）；
* 存储类支持 **RWX**，并且 worker（uid 65534）能在上面建目录 —— NFS 上如果开了 `root_squash`，
  `initContainer` 的 `chown` 会被拒，它会**验证**属主并停在 `Init:Error` 并打印一次性修法，
  而不是起来以后每个 image resolve 都失败（§2.7.1）；
* Pod Security：worker 用 `Localhost` seccomp + 4 个 cap + 一个 pod 级 `net.*` sysctl，
  在 baseline 档内；**不要**给它 `no-new-privileges`（会让内核直接忽略 file capabilities，
  broker 失效）。

---

## 2. 部署顺序

```bash
NS=sandlock

# 1) 命名空间 + 存储
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/pvc.yaml

# 2) 密钥（先照着 §4 把占位值换掉，再 apply 业务清单）
kubectl -n $NS create secret generic e2b-secrets \
  --from-literal=E2B_API_KEYS='<你的 API key>' \
  --from-literal=E2B_INTERNAL_API_KEY='<随机 internal key>' \
  --from-literal=E2B_REDIS_PASSWORD='<随机 redis 口令>'

# 3) 依赖服务
kubectl apply -f deploy/k8s/redis.yaml

# 4) control-plane（含 gateway）
kubectl apply -f deploy/k8s/control-plane.yaml
kubectl -n $NS rollout status deploy/control-plane

# 5) ★ seccomp 安装器：必须在 worker 之前，且等每个节点 Ready
kubectl apply -f deploy/k8s/seccomp-installer.yaml
kubectl -n $NS rollout status ds/seccomp-installer     # 每个节点一个 Ready

# 6) worker（缺 profile 的节点会起来失败 —— 这是 fail closed，不是 flake）
kubectl apply -f deploy/k8s/worker.yaml
kubectl -n $NS rollout status sts/e2b-worker

# 7) autoscaler（可选）
kubectl apply -f deploy/k8s/autoscaler.yaml
```

**第 5 步不能省、也不能并行**：kubelet 在**节点宿主**上解析 `Localhost` profile，节点上没有那个
文件时 pod 根本起不来。**N4 已于 2026-09-17 在 main 集群验证**：安装器在 5 个真节点全部写入成功
（`installed /var/lib/kubelet/seccomp/sandlock-worker.json (13147 bytes)`），带 `Localhost` profile 的
pod 实测 `Seccomp: 2 / Seccomp_filters: 1`（即 worker 启动自检所需的谓词为真）。
附带两条实测结论：① profile 里含**该内核并不存在**的 syscall 名（`landlock_*`、`fchmodat2`、
`mount_setattr` …）**不影响加载** —— containerd 2.1.6 能解析，profile 跨内核可移植；
② 安装器原来**容忍全部污点**（为了覆盖控制面节点），因此在 ACK 的 virtual-kubelet(ECI) 节点上
被判 `NotSupport`、DaemonSet 停在 5/7 **永不收敛**，让本步的“等每个节点 Ready”永远等不到。
已加 `nodeAffinity: type NotIn [virtual-kubelet]` 修复，复验 `successfully rolled out` / 5-5-5。worker 自己还有一条启动
自检：profile 没生效就拒绝服务（`SECCOMP_PROFILE_NOT_APPLIED`），所以"pod 起来了但 profile
没真加载"这条不会静默通过。

### 镜像与升级

清单里的镜像 tag 目前是占位的 `:0.1.0`。发布流程与 compose 同源：`build-and-push.sh` 把
worker / control-plane-gateway / autoscaler / quota-agent 推到 ACR，并**把版本写进
`deploy/stack/.version`（gitignored）**；compose 侧 `upgrade.sh` 直接读它来 pin tag，
k8s 侧没有等价的自动机制，所以要显式把 tag 换成当次构建的版本。
**2026-09-18 的最近一次发布**：**`0.1.0-350-g212850d-20260918-152008`**（N12/N19/N20/N21/N22-N24 与
`reconcile` 解耦那批都在里面）。**两套栈现在跑同一个 tag**：compose 生产栈（`.140`）与这台
k0s 集群都指到它，`deploy/stack/.version` 重新成为唯一权威 —— §13.3 里那个"k8s 侧 tag 漂移"
已经消掉。升级时：

```bash
kubectl -n $NS set image sts/e2b-worker worker=<REGISTRY>/byteplan/e2b-sandlock-worker:<VERSION>
kubectl -n $NS set image deploy/control-plane control-plane=<REGISTRY>/byteplan/e2b-sandlock-control-plane-gateway:<VERSION>
kubectl -n $NS set image deploy/autoscaler autoscaler=<REGISTRY>/byteplan/e2b-sandlock-autoscaler:<VERSION>
kubectl -n $NS set image ds/seccomp-installer installer=<REGISTRY>/byteplan/e2b-sandlock-worker:<VERSION>
```

⚠ 老集群（worker 还是 Deployment 的）切到 StatefulSet 要多一步：`worker.yaml` 换了 `kind`，
`kubectl apply` 只会新建 `e2b-worker` StatefulSet，**旧的 Deployment 还在**（同名不同 kind，
两者会各自跑副本、各自注册成 worker）。顺序：

```bash
kubectl -n $NS delete deploy/e2b-worker        # 先停旧的（它的沙箱随之结束）
kubectl apply -f deploy/k8s/worker.yaml
kubectl -n $NS rollout status sts/e2b-worker
```

`seccomp-installer` 的镜像只用来跑 `cp`（profile 本体在 ConfigMap 里），跟着 worker 版本走是
为了少一个变量；profile 内容变了它自己会滚（ConfigMap 名字带内容哈希时更省事，当前是名字不变
+ `checksum` 注解，见清单注释）。

---

## 3. compose ↔ k8s 差异表（2026-09-17 更新）

| 面 | compose（线上） | k8s（本清单） | 影响 |
|---|---|---|---|
| per-sandbox netns | **开** | **开**（2026-09-17 对齐，N5 关闭） | 一致：沙箱自有 netns，只见 `lo` |
| 低端口 sysctl | 已撤 | **已撤**（2026-09-17，N5 的配套） | 两边都没有了；顺带关掉了「pod 内任何进程都能绑低端口」这个与沙箱无关的口子 |
| pid_ns | **开**（2026-09-16 全量） | **开**（2026-09-17 对齐，N10 关闭） | 一致：`kill(pid,0)` 不再是同 pod 进程的存在性探针 |
| 沙箱身份 | 每沙箱独立 host uid（两 worker 用不重叠段 10000/11000） | 每沙箱独立 host uid（段默认相同，但**共用一个 base ⇒ 共用一个分配器**：`uid_pool.acquire` 先 flock `<base>/.uid_pool.lock`，再按全部 `sandbox.json` + 预约标记重算空闲集 ⇒ 副本之间**不会**发同一个 uid） | 已验（§13）：真集群上 4 个沙箱分布在两个副本、磁盘读出的宿主 uid 互不相同。**前置是锁跨节点**（NAS 上只有 NFSv4.0 成立），autoscaler 上限 2026-09-17 起放开到 16 |
| route B（槽位） | 每沙箱一个 `sandlock-supervise --uid <槽位>` | 同（`E2B_PRIV_HELPERS=auto`，非 root pod 用镜像里的 file-cap broker） | 一致；broker 依赖上面那 4 个 cap 在**bounding set** 里 |
| 配额 | stack 内 quota-agent（`E2B_QUOTA_AGENT_URL`），XFS prjquota 已开 | **无 agent → 降级**（无 per-sandbox 磁盘硬限，一条 WARNING）。⚠ **compose 停用后（2026-09-18）这是 k8s 主线上唯一缺的实能力**，见 §20 | 口径写在 §2.4.4；这台集群的共享卷是**托管 NAS**，agent 必须跑在 NFS 服务端 ⇒ 落不下来；真要硬限得换方案（§20） |
| seccomp | 容器 `seccomp=<deploy/seccomp/sandlock-worker.json>`（compose 直接引用文件） | `Localhost` profile + DaemonSet 安装器 | 语义相同；k8s 多了"每节点装文件"这一步（§2） |
| 卷 | 命名卷 `sandbox-shared`（宿主 XFS，支持 prjquota） | RWX PVC（NFS/CephFS） | XFS 项目配额只在 XFS 上；NFS 走 agent 那套 |
| 镜像缓存 | 卷内 `_images`，属主 65534 | 同（initContainer 建/验） | 一致 |

---

## 4. 配置与密钥

**必须先替换的占位值**（清单里现在是明文占位，别直接上生产）：

| 位置 | 现在 | 要改成 |
|---|---|---|
| `control-plane.yaml` | `E2B_API_KEYS: "local-key"`、`E2B_INTERNAL_API_KEY: "internal-key"` | `secretKeyRef` → `e2b-secrets` |
| `worker.yaml` | `E2B_INTERNAL_API_KEY: "internal-key"` | 同上（worker↔控制面） |
| `autoscaler.yaml` | `E2B_AS_INTERNAL_API_KEY: "internal-key"` | 同上 |
| `redis.yaml` + `control-plane.yaml` | `redis://redis:6379/0`，redis 无口令 | `--requirepass` + `redis://:<password>@redis:6379/0`（否则沙箱可读写全集群账本） |
| 各 Deployment | `image: ...:<版本>` | 当次构建的真实版本（§2） |

worker 侧关键 env（语义见 `deploy/stack/.env.example` 的同名键）：
`E2B_WORKSPACE_BASE`、`E2B_IMAGE_CACHE_DIR/MAX_BYTES/EVICT_MIN_AGE_S/OWNER_UID`、
`E2B_ROUTE_B_TMP_ROOT`（必须在 broker 白名单内：工作区或卷根之下）、`E2B_PRIV_HELPERS=auto`、
`E2B_NODE_{MEMORY_MB,CPU_PERCENT,DISK_MB,PROCESSES}`（容量声明，autoscaler 与调度都看它）。

---

## 5. 把 k8s 切到与 compose 相同的形态（✅ 2026-09-17 已落地：N5 / N10）

**本节已按下列步骤落地（`deploy/k8s/worker.yaml`，2026-09-17）**，但**尚未在真实集群复跑 §6** ——
本地无法验证 k8s 层（无集群）。落地内容与理由：两条开关都加进 worker env，pod 级 sysctl 块删除，
`tests/unit/test_worker_manifest_permissions.py` 改为钉住新形态（窗口消失 + 两条开关成对 + pid_ns 在）。

**netns（N5）**——worker 容器 env 里两条（**必须成对**，`create_app`
会拒绝单开：单开会把每个沙箱变成"只有 lo"，即静默断网）：

```yaml
            - name: E2B_ENABLE_NET_ISOLATION
              value: "true"
            - name: E2B_FD_INJECT_CONNECT
              value: "true"
```

然后**撤掉 pod 级 `securityContext.sysctls`**（`ip_unprivileged_port_start=0`）——它的唯一用户是
wildcard-DNS 的 `:53`，而切了 netns 之后那个 bind 发生在沙箱自己的 netns 里，root-in-userns 自带
`CAP_NET_BIND_SERVICE`（fork `context.rs`）。（`deploy/k8s/worker.yaml` 的注释与
`tests/unit/test_worker_manifest_permissions.py` 都按"k8s 仍是共享 netns"钉住，切换时要一起改。）

**pid_ns（N10）**——加一条即可（没有配对守卫；pid_ns 不会让沙箱离线）：

```yaml
            - name: E2B_PID_NS
              value: "true"
```

两条都已加进清单，**仍需按 §6 在真实集群复跑**；pid_ns 的 k8s 特有失败模式是 **fail closed**：节点不允许非特权 userns
（内核开关 / LSM）时，中间进程 `unshare(CLONE_NEWUSER)` 失败 ⇒ **建箱报错**，而不是静默退回
共享 pid ns。

---

## 6. 验证清单

**A. 起没起来 —— k8s 层**

```bash
kubectl -n sandlock get pods -o wide
kubectl -n sandlock logs sts/e2b-worker | grep -E "seccomp self-check|route-B instance ready"
#   seccomp self-check: filter mode active, user namespaces allowed    ← 自检通过
#   route-B instance ready … uid=<池位> … guest-uid=uid-0-in-userns    ← 槽位起来了
kubectl -n sandlock get pods -l app=e2b-worker \
  -o jsonpath='{range .items[*]}{.metadata.name}{" restarts="}{.status.containerStatuses[0].restartCount}{"\n"}{end}'
```

**B. 应用层冒烟**（与 compose 同一套脚本，只换 URL）

```bash
python3 deploy/scripts/deployment_smoke.py     # 需要 E2B_API_URL / E2B_SANDBOX_URL 指向 gateway
python3 deploy/scripts/multinode_smoke.py
```

覆盖：命令/文件/健康经网关、stdin、kill 后预留归零、迁移保留文件、网络配置、卷挂载与隔离、
模板构建→registry→worker 拉取→镜像 rootfs、箱内 MCP 经代理。

**C. 形态证据（单看 `id -u`=0 会假绿，必须配对）**

| 观测 | 怎么读 | 期望 |
|---|---|---|
| 客人身份 | 箱内 `id -u` + 宿主侧落盘文件属主 | `0` / 池内 uid（不是 0） |
| pid ns（N10 开启后） | 箱内 `kill(1,0)`：`ok`=pid 1 是自己；`EPERM`=容器 init，即共享 pid ns | 开启后 `ok`；另可探一个容器内外来 pid，期望 `ESRCH` |
| netns（N5 开启后） | 箱内 `socket.if_nameindex()` 只见 `lo` | 开启后只有 lo |
| 槽位 | worker 日志 `route-B instance ready` 的 `guest-uid=uid-0-in-userns` | 每箱一条 |
| 端口带水位 | `GET /nodes`（`X-API-Key`）里 `mcpPortsInUse`/`mcpPortsCapacity` | 随 MCP 沙箱数涨落；接近 4535 才需要动作（§2.9） |
| 配额 | 有 agent 时：`/detect` 的 `prjquota`；`/report` 每项目 `hard_blocks` | 无 agent（本清单默认）时是**降级**，只有一条启动 WARNING |

**D. 多副本（autoscaler 提到 >1）之前必须做的**：~~解决 §3 表里那条 uid 池重叠（N13）~~
✅ 2026-09-17 已收口（§13）：真集群验证了两副本共用一份 base 不互相破坏，清单上限已放开。
仍待确认的是 PDB `minAvailable: 1` 与 `terminationGracePeriodSeconds: 120` 对滚动更新的行为。

---

## 7. 回滚

* 单pod 形态开关回滚：把 §5 加的那几行删掉（或改 `false`）再 apply，容器重建即回到共享
  netns / pid ns；**没有**数据回滚。
* 版本回滚：`kubectl -n sandlock set image ...:<旧版本>`（旧镜像要在 ACR 里）。
* seccomp：profile 文件留着不影响任何东西（没有 pod 引用它）；删 pod 不会删节点上的文件，
  卸载要自己 `rm /var/lib/kubelet/seccomp/sandlock-worker.json`（或删 DaemonSet 后再删文件）。

---

## 7.5 main 集群（ACK，kernel 5.10）能验什么

2026-09-17 在这台集群上实测：**沙箱数据面在此不可验证**，两条硬前置都不满足 ——
`landlock_create_ruleset(VERSION)` 返回 **ENOSYS**（`sandlock.landlock_abi_version()` = -1，
Landlock 要 5.13+），且 `user.max_user_namespaces = 0` ⇒ `unshare -U` 直接 ENOSPC
（非特权 userns 被关，per-sandbox uid / route-B 槽位同样不成立）。
因此 §6 的 B（应用冒烟）、C（形态证据）、逃逸套件、网络清单、配额、N13 的多副本树归属
**一项都做不了**；沙箱侧验收仍须在有 Landlock 的机器（compose 那台 ABI 8）上进行。

**这台集群适合做的**：部署机制与平台层 —— 清单 apply/调度、RBAC、**多架构镜像**（实测三个
镜像都是 amd64+arm64，而 5 个真节点里 4 个是 arm64，这条不成立就是部署拦路虎）、seccomp 安装器
（N4，已验证）、以及“worker 在不能约束的内核上是否正确拒绝”（已用它抓到并复验 `auto` 的静默降级）。

---

## 8. 已知缺口（登记在 `docs/task-backlog.md`）

| # | 缺口 | 下一步 |
|---|---|---|
| ~~N4~~ | ~~seccomp 安装器未在真实集群验证~~ | ✅ **2026-09-17 已在 main(ACK) 验证**：安装器 5 个真节点全部写入、带 Localhost profile 的 pod 实测 `Seccomp: 2`。两条附带结论：profile 里含该内核没有的 syscall 名**不影响加载**（跨内核可移植）；虚拟节点会 `NotSupport` 导致 DS 永不收敛 —— 已加 `nodeAffinity` 修复（commit `235fc34`） |
| ~~N5~~ | ~~k8s 是否切 per-sandbox netns~~ | ✅ 2026-09-17 已切（§5），**待真实集群按 §6 复验** |
| ~~N10~~ | ~~k8s 是否开 pid_ns~~ | ✅ 2026-09-17 已切（§5），同上待复验；节点仍需允许非特权 userns（这依赖本来就存在） |
| ~~N13~~ | ~~**多副本形态整体未验证**~~。~~uid 池重叠~~ 已更正：两个 pod 共用同一 base 时**共用同一个分配器**（`uid_pool.acquire` flock + 按全部 `sandbox.json`/预约标记重算），不会发同一个 uid。真正的未知是**同一 base 上各副本的 reconcile/GC 权限** | ✅ **2026-09-17 已收口（§13）**：`deploy/scripts/multiworker_interference.py` 在真集群跑通 —— 重启一个 worker 后它把舰队仍拥有的 4 棵活树全判 `protected_elsewhere=4`、`deleted=0`，4 棵树都在，幸存 worker 的沙箱照常读写。清单从 `replicas: 1`+MAX=1 改为 `replicas: 2`+MIN=2/MAX=16；前置是锁跨节点（NAS 只有 NFSv4.0 成立）。路上另修「死节点仍被派活 ⇒ 502」。衍生缺口见 N20/N21 |
| — | k8s 无 quota-agent 清单（口径=降级，§2.4.4） | 有真实 k8s + XFS/NFS 环境时补清单（agent 形态对 NFS 才是唯一可行路径） |

---

## 9. 自建集群：验证沙箱数据面与多副本（计划登记 2026-09-17）

**为什么**：§7.5 已确认 main 的 ACK 集群（kernel 5.10）既没有 Landlock 也没有非特权 userns，
沙箱数据面与多副本（N13）在那里**物理上不可验** —— §6 的 B/C 至今没有在真实集群上跑过。
新增节点 **172.18.80.94** 让一台自建集群成为可能，本节把方案与判据先登记下来。

### 9.1 前置闸门（不合格就不必建）

| 判据 | 通过线 | 为什么是硬闸门 |
|---|---|---|
| `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` | **≥ 6** | 权威判据，比看版本号可靠（Landlock 5.13+ 才有，ABI 6 要 6.12+） |
| `uname -r` | ≥ 6.12（参考） | 同上 |
| `user.max_user_namespaces` | **≠ 0** | ACK 上是 0：`unshare -U` → ENOSPC，per-sandbox uid / route-B 槽位一起失效 |
| `kernel.apparmor_restrict_unprivileged_userns` | 不为 `1`（或给 kubelet/containerd 配 AppArmor profile） | Ubuntu 24.04+ 会掐掉 userns；表现是**建箱报错**（`auto` 已 fail closed，不会静默降级） |

> **换发行版不会影响这张表。** ACK 就是对照：同一批镜像在 k3s/k0s/kubeadm 上结果一样。

### 9.2 两个必须先定的问题

1. **"已有的线上节点"指哪台？**
   - **ACK 的节点**（172.18.93.x / 94.x）：**不可 join**。它们由 ACK 托管，kubelet/containerd 配置属于 ACK，
     强行 join 会破坏 ACK 的节点生命周期；ACK 集群也不可能被"收编"成自建集群的一部分。
   - **172.18.80.140**（compose 生产栈所在机，Landlock ABI 8）：可以但风险明确 —— 引入
     containerd/kubelet/CNI 后，**iptables 规则与 Docker 冲突是这类混合部署的经典故障**，且资源会与新集群互相挤。
     建议第二个 worker 用**备用机**；非用不可则 taint 隔离 + 端口/网段避开 + 明确接受风险。
2. **用途定级**：只验"单节点 k8s 形态"（§6 A/B/C）→ 1 台足够（用 k3s 自带 local-path 存储）；
   要验**多副本 N13** → 必须 ≥2 个可用 worker **且**共享 **RWX**（自建集群得自己起 NFS）。

### 9.3 方案选择

| 方案 | 起量 | 自带 | 结论 |
|---|---|---|---|
| **k3s** | 最快，单二进制 | containerd + flannel + Traefik + ServiceLB + local-path | **推荐**；必须 `--disable traefik --disable servicelb`，否则 ServiceLB 的 DaemonSet 会去抢宿主 80/443 |
| **k0s** | 快，单二进制 | containerd + kube-router，**不带** ingress/LB | **备选**：自带行为最少；`k0sctl` 一份 YAML 管多节点 |
| kubeadm | 慢 | 几乎不带 | 只在需要完全贴上游时 |
| kind / k3d | 快 | 容器当节点 | **不用于验隔离**：内核仍是宿主内核，但多了一层 userns/seccomp，会让结论归因不清；只适合验清单/调度 |
| microk8s | 中 | snap 全家桶 | AL8/非 Ubuntu 上不一定顺，不推荐 |

### 9.4 E2B 侧三个前置（比选发行版更容易踩）

1. **seccomp**：k8s **不会**默认给 pod 加 seccomp profile，而 worker 有启动自检
   （profile 没生效就 `SECCOMP_PROFILE_NOT_APPLIED` 拒服务）⇒ §2 的 `seccomp-installer` 照样要先跑。
   已在 ACK 验证可用（含 virtual-kubelet 的 `nodeAffinity` 修复，commit `235fc34`）。
2. **共享存储**：单节点 → k3s `local-path` 即可；**多副本 → 必须 NFS**。
   `deploy/k8s/pvc.yaml` 已预留 `# storageClassName: nfs`，仓库里已有 `deploy/scripts/nfs-probe`、
   `nfs_quota_probe.sh` 与相关文档口径 —— 是先例，不是新坑。
3. **镜像**：直接用当前发布 `0.1.0-350-g212850d-20260918-152008`（值见 `deploy/stack/.version`；
   含 `auto` 在无 Landlock 内核上 fail-closed 的修复，已在 ACK 复验过）。

### 9.5 最短路径（内核闸门通过后）

```bash
# 节点 1 = 控制面 + worker
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -
# 节点 2 = worker
K3S_URL=https://<node1>:6443 \
K3S_TOKEN=$(cat /var/lib/rancher/k3s/server/node-token) \
curl -sfL https://get.k3s.io | sh -

# 清单适配（自建集群版）：
#   pvc.yaml      -> local-path（单节点）或 NFS PV（多副本）
#   *.yaml 镜像 tag -> 0.1.0-330-g235fc34-20260917-142808
#   其余照 §2 顺序：ns -> secret -> redis -> control-plane -> seccomp-installer(等 Ready) -> worker
```

### 9.6 验收与解绑

| 步 | 判据 | 解绑 |
|---|---|---|
| 建集群 | 三个闸门判据达标；`kubectl get nodes` 全 Ready | —— |
| §6 A | worker `seccomp self-check` + `route-B instance ready` 两条日志 | k8s 形态自检 |
| §6 B | `deployment_smoke.py` / `multinode_smoke.py` 全绿 | 应用层在 k8s 上首次真跑通 |
| §6 C | 箱内 `id -u`=0 / 宿主落盘属主=池内 uid / 只见 `lo` / `kill(1,0)`=ok | N5/N10 在自建集群上复核（此前只验了清单，没验运行时） |
| 多副本 | 2 worker + 共享 RWX；观察一个 pod 的 reconcile/GC 是否会动到另一个 pod 的活树 | **N13**（多副本形态首次验证） |

### 9.7 待确认（卡在这三件上，先不动手）

1. **172.18.80.94 的内核**（权限：从开发机直连不通、bastion 拒了本机公钥，需你给输出或访问）。
2. 第二个 worker 用**哪台**（备用机 or 172.18.80.140）。
3. 是否需要**多副本**（决定要不要上 NFS）。

---

## 10. k0s 自建集群（2026-09-17 执行）

§9 的三个待确认项已经定下来并落地：**用 k0s**（不是 k3s）、节点 `172.18.80.94` +
`172.18.80.140`、两者都走跳板机访问（与 compose 那条线同一拓扑）。集群**已建成双节点**，
清单**已在真实集群上跑起来**，并抓出 8 条只有真集群才会暴露的问题（§10.3）。

### 10.1 集群事实

| 项 | 值 |
|---|---|
| k0s | `v1.36.4+k0s.0`（二进制 240 MB，arm64） |
| 节点 | `izuf697v12g31dyz4uvsjlz` = 172.18.80.94（controller + worker）；`izuf6d1usviqv6x9qk1hpcz` = 172.18.80.140（worker） |
| OS / 内核 | Rocky Linux 10.2 / `6.12.0-211.34.1.el10_2.aarch64`（两节点同规格：4 vCPU / 7 GB / 100 GB） |
| 存储后端 | etcd（k0s 默认；**不要用 `--single`**，那会切 kine 且拒绝加入） |
| CNI | kube-router（k0s 默认，原生路由、不带封装） |
| 网段 | podCIDR `10.244.0.0/16`（每节点 /24）、svcCIDR `10.96.0.0/12`；与 `.140` 上 docker 的 `172.17/172.19` 不冲突 |
| 共享存储 | 静态 NFS PV → 阿里云 NAS `347d748090-ihu74.cn-shanghai.nas.aliyuncs.com:/sandlock`，**NFSv4.0** |
| §9.1 三道闸门 | **全过**：Landlock ABI `6`、`max_user_namespaces=30519`、无 AppArmor、`unshare -U -m -p -f --mount-proc` 可用 |

`.140` 与 compose 生产栈的共存已实测：加 worker 后 6 个 compose 容器照常运行、gateway 仍在
`:3000` 应答、iptables 里 `KUBE-*` 与 `DOCKER-*` 链共存（FORWARD policy 是 DROP，但
kube-router 的 `in/out kube-bridge ACCEPT` 与 `KUBE-FORWARD` 在 DOCKER-* 之前）、
k0s 的 containerd socket（`/run/k0s/containerd.sock`）与 docker 的互不相干。

### 10.2 部署形态

用 `deploy/k8s-k0s/`（kustomize overlay，`kubectl kustomize` 即可渲染，不需要额外装 kustomize）。
它的四类差异与理由写在 [`deploy/k8s-k0s/README.md`](../deploy/k8s-k0s/README.md)；镜像与密钥的
对齐（redis 镜像到 ACR、`E2B_BASE_IMAGE` 固定 digest、密钥改走 Secret）落在基线清单里，
见 §10.3 F8/F9。

### 10.3 真集群才会暴露的问题（每条都有实测证据）

| # | 问题 | 证据与处置 |
|---|---|---|
| **F1** | **kubelet 的 seccomp 根不是固定路径**：它解析 Localhost profile 时用的是 `<kubelet --root-dir>/seccomp`。k0s 的 `--root-dir=/var/lib/k0s/kubelet`，而清单写的是 `/var/lib/kubelet/seccomp` | 实验：把 `probe-a.json` 只放 `/var/lib/kubelet/seccomp`、`probe-b.json` 只放 `/var/lib/k0s/kubelet/seccomp`，前者报 `cannot load seccomp profile "/var/lib/k0s/kubelet/seccomp/probe-a.json"`，后者 Running。**处置**：安装器的脚本/挂载/hostPath 三处参数化（`E2B_SECCOMP_ROOT`，默认仍是 kubeadm 路径），k0s overlay 一起改三处，并加了「三处必须一致」的用例 |
| **F2** | **Deployment 的 pod 在 headless Service 下没有 per-pod DNS 名**，`E2B_NODE_ADDRESS: http://$(POD_NAME).worker-headless...` 永远解析不了（`docs/SCALING.md` §8.1 的设计来自 compose，那里靠 Docker 内嵌 DNS 解析容器名） | EndpointSlice 里 endpoint 的 `hostname` 为空（hostname 来自 `pod.spec.hostname`，Deployment 不设），`Sandbox.create()` 全部报 `502: Node ... unavailable: [Errno -2] Name or service not known`。**处置**：地址改用 pod IP（`fieldRef: status.podIP`），pod 重启后重新注册即更新 |
| **F3** | **跨节点 pod 流量被云网络拦掉**（kube-router 不做封装，跨节点包带的是 `10.244.x`）。两个独立机制叠加：① ENI 的**「源/目的地址检查」**只放行源/目的属于本实例的报；② 安全组规则是 **`172.16.0.0/12` 全通**，而 `172.16.0.0/12 = 172.16–172.31`，**不含 pod 网段 `10.244.0.0/16`** | 干净复测（上一轮的「零收包」是抓包过滤器被 `.140` 上 compose redis 的 `172.19.0.2:6379` 流量填满导致的假象，已纠正）：<br>• `.94 → .140` **自身 IP**：到达（`.140` eth0 抓到 echo request）——节点链路正常，与「172.16/12 全通」一致；<br>• 入包二层源 MAC 是 `ee:ff:ff:ff:ff:ff`，不是 `.94` 的真实 MAC `00:16:3e:6f:a2:a4` ⇒ VPC **代理 ARP、按 IP 转发**；<br>• `.94 → 10.244.1.6 / 10.244.1.1`：不到达；<br>• **`.94 → 172.18.94.250`**（手动加在 `.140` eth0 上、也在 172.16/12 内、但非平台分配）：**也不到达** ⇒ 这不是安全组能解释的，ENI 检查存在；<br>• `.140` 用**外来源** `10.244.1.1` ping `.94` 自身 IP：包离开 `.140` 网卡，`.94` 抓包 **0 个**（`.94` 的入向规则只授权 172.16/12，而源是 `10.244.x`）。<br>**处置（两条路）**：<br>**A. 保留原生路由**：安全组加 `10.244.0.0/16`（或 `10.0.0.0/8`）放行 + 关掉两块 ENI 的源/目的地址检查；因为该 VPC 是「代理 ARP + 按 IP 转发」，**很可能还需要给 pod 网段加 VPC 自定义路由**（下一步指向对应 ENI），否则路由器查不到 `10.244.x`；<br>**B. 不动云配置**：把 CNI 换成带封装的（k0s `network.provider: calico` + `calico.mode: vxlan`，或 ipip），节点间只出现 `172.18.x`（已在放行范围内）。**这条已实测可行**：手工建 VXLAN(UDP/4789) 与 IP-in-IP(proto 4) 隧道，两节点双向 ping 均 0% 丢包、亚毫秒（`tmp/k0s/overlay-probe.sh`）。代价：多一层封装、MTU 要降、Pod 网段重建。<br>未修之前多副本与冒烟都跑不了（见 §10.4） |
| **F4** | **网络文件系统 + 非 root worker 做不了 chown**：c1/route B 要把沙箱树交给池 uid（`0770 owner=<沙箱 uid> group=<worker gid>`），这一步由镜像里带 `cap_chown` 的 broker 执行——但 **CAP_CHOWN 不过网**，NFS 只看 AUTH_SYS 凭据里的 uid，而「把文件让给别的 uid」只有 root 能做 | 实测：worker（uid 65534，broker `cap_chown,cap_dac_override=ep`）`chown 10000:65534` → `Operation not permitted`；同一挂载上 root 做同样 chown → 成功。**处置**：worker 以 `runAsUser: 0` + `runAsGroup: 65534` 跑（保留 worker 组才能进出 `0770 group=<worker gid>` 的沙箱树）。**这条不是 k0s 特有**：基线的「非 root worker + RWX PVC」组合在任何 NFS/CephFS 上都不成立，只在本地盘（compose 命名卷）上成立 |
| **F5** | **NAS 的锁语义决定多副本能不能成立**：uid 池靠 `flock(<base>/.uid_pool.lock)` 在副本之间排他 | 三种挂载实测：v3+服务端锁 → flock/fcntl 全 `ESTALE`；v3+`nolock`（`.140` 现用参数）→ 锁正常但**只在单机内有效**；**v4.0 → 跨节点互斥成立**（`.94` 持锁时 `.140` 抢锁被挡）。另：这台 NAS 只支持 v4.0，`vers=4.1/4.2` 客户端直接 `EPROTONOSUPPORT`。**处置**：PV 用 `vers=4.0`，不要 `nolock` |
| **F6** | **卷根必须对 worker 可写**：worker 直接在卷根下建 `sbx_*`（compose 的约定是 `1777`），而新供给的 RWX 卷通常是 `root:root 0755` | 第一个 `Sandbox.create()` 直接 `[Errno 13] Permission denied: '/var/lib/e2b-sandboxes/sbx_<id>'`。**处置**：worker 加 `workspace-root-init`（只动卷根自身的模式，不动下面的沙箱树），修完**校验**属主/模式并在修不动时报一次性修法——与 `image-cache-init` 同一套路 |
| **F7** | **worker 的 node id 是 pod 名**，每次重建都是新节点：死节点的预留永不回收，fleet 视图累积僵尸 | 一次会话内换了 6 个 pod 名 → `/internal/nodes` 出现 6 个 `unhealthy` 僵尸，其中一条还挂着 1024 MB 预留（`deployment_smoke.py` 的「kill 后预留归零」断言因此失败）。另：心跳在 `register` 之前会打一条 `404`（compose 是 `204`），无害但会误导。**未解**：要稳定 id 得换 StatefulSet，而 autoscaler 现在按 Deployment scale |
| **F8** | **默认容量只放得下 1 个沙箱**（README 的 F8 早有记载，这里是 k8s 侧的复现）：`E2B_NODE_PROCESSES=256` == 单沙箱默认 256，`can_fit` 按整箱预留 | 第 3 个 `Sandbox.create()` 报 `503: No resources available`。**处置**：overlay 把 `E2B_NODE_*` 抬到 4096/400/8192/1024（与生产 `.env` 同值），并把 pod 的 requests/limits 显式拆开（基线把 `limits` 当 requests=2 CPU，滚动更新因此停在 `0/2 nodes are available: 1 Insufficient cpu`；基线的 `replicas: 1` 也补了 `maxSurge: 0`，见下） |
| **F9** | 部署主机**不通 Docker Hub**（`registry-1.docker.io` / `auth.docker.io` 全关），而基线 redis 引用的是 `redis:7-alpine` | 清单里的 redis 改成 ACR 镜像（多架构 arm64+amd64，redis 8.10.1，与 compose 用的是同一大版本）；`E2B_BASE_IMAGE` 也从 `python:3.14-slim` 换成生产同款 digest-pinned ACR 镜像。ACR 支持**匿名拉取**（实测 token 流程 200），所以 pod 不需要 imagePullSecret |

顺带修进基线的两条（不在上表）：① `worker.yaml` 当时把 `replicas` 钉在 1 并加
`strategy.maxSurge: 0`——默认滚动更新会先起第二个 pod，那正是当时「未验证」的
双副本共享 base 形态，而且 4 核节点上也排不下。**N13 收口（§13）后 `replicas` 放开到 2**，
**N20 收口（§15）后整个对象从 Deployment 换成 StatefulSet**——后者的更新本来就是
delete-then-create、没有 surge pod，`maxSurge` 那条也就随之消失了（这个 workload 没有
`requests`，Kubernetes 会把 2 CPU 的 limit 复制成 request）；② 明文占位密钥（`local-key` /
`internal-key` / 无口令 redis）改成 `e2b-secrets` Secret + `--requirepass`，缺 Secret 时
pod 停在 `CreateContainerConfigError` 而不是静默用一个公共 key。

### 10.4 验证状态

| 项 | 状态 | 证据 |
|---|---|---|
| §6 A 起没起来 | ✅ | worker 自检 `seccomp self-check: filter mode active, user namespaces allowed`；pod 内 `/proc/1/status` `Seccomp: 2`、`Seccomp_filters: 1`；安装器在 `.94` 写入 `/var/lib/k0s/kubelet/seccomp/sandlock-worker.json (13147 bytes)`；PVC `Bound`（50Gi RWX，静态绑定 NAS PV）；seccomp DaemonSet 2/2 |
| §6 C 形态证据 | ✅（单节点） | 箱内 `id -u`=0；`socket.if_nameindex()` 只有 `lo`（N5 per-sandbox netns）；`kill(1,0)`=ok（N10 per-sandbox pid ns）；宿主侧落盘 `drwxrwx--- 10000:nogroup`（owner=池 uid 10000、group=worker gid 65534、0770，即 c1 模型） |
| §6 B 应用冒烟 | ✅ **2026-09-17 全绿** | `deployment_smoke.py`：跨节点分布、经 gateway 的命令与文件、**跨节点迁移且共享 workspace 文件保留**、网络配置回显与原子更新、远端卷挂载与兄弟卷隔离、**模板构建 → worker 拉取 → 镜像 rootfs**、**箱内 MCP 经代理**、kill 后预留归零 —— 整轮 **20.6 秒**。`multinode_smoke.py`（4 个沙箱 2+2 跨两节点）**8.7 秒** 通过。两条冒烟都要求在途 ≥2 个健康 worker |
| 多副本 N13 | ✅ **2026-09-17 已收口（见 §13）** | 判据脚本 `deploy/scripts/multiworker_interference.py` 在真集群跑通：4 个沙箱 2+2 跨两个 worker、**磁盘上读出的宿主 uid 互不相同**（`[10000, 10001, 10002, 10006]`）、**重启一个 worker 后 4 棵树全在**（起手 reconcile 把舰队仍拥有的 4 棵全判 `protected_elsewhere=4`、`deleted=0`）、幸存 worker 的沙箱文件读写照旧、两侧预留归零。清单已解除 pin（worker `replicas: 2`、autoscaler MIN=2/MAX=16），前置是共享存储的锁必须跨节点（NAS 上只有 NFSv4.0 成立）。路上另修「控制面把死节点当活节点派活 ⇒ 502」（放置改用 15 秒的独立窗口） |

### 10.5 下一步

1. **修 F3**（二选一）：
   - **A（原生路由，云侧要动三处）**：安全组加 `10.244.0.0/16` 放行；关掉两块 ENI 的
     「源/目的地址检查」；**并很可能**再给 pod 网段加 VPC 自定义路由（该 VPC 代理 ARP、
     按 IP 转发，路由器查不到 `10.244.x`）。
   - **B（不动云配置，已实测可行）**：k0s 换 `network.provider: calico` +
     `calico.mode: vxlan`（或 ipip）。VXLAN 与 IP-in-IP 在这两个节点之间都通
     （手工隧道实测 0% 丢包），且节点间只出现安全组已放行的 `172.18.x`。
     代价：封装开销、MTU 降、Pod 网段重建。
2. ~~撤掉临时 taint，把 `e2b-worker` 拉到 2 副本，按 §6 复跑 A/B/C 并开始 **N13**。~~
   ✅ 已做（2026-09-17）：Calico 换完（§11）后 A/B/C 全绿，N13 在 §13 收口。
3. 收尾遗留：worker 的稳定 node id（F7）、v3/nolock 与 v4 的锁语义差异要不要写进
   存储选型门槛、以及 F4 那条「非 root worker 与网络文件系统不兼容」是否要升级成
   基线的显式约束。

---

## 11. 换成封装型 CNI（Calico VXLAN），跨节点打通（2026-09-17 执行）

§10.5 的路线 B：不动云配置，把 k0s 的 CNI 从 kube-router（原生路由）换成
**Calico VXLAN**。节点之间只出现 `172.18.x`（已在安全组 `172.16.0.0/12` 的放行范围内），
pod 网段不再出现在云网络上。**跨节点 pod 流量已打通**，冒烟的多节点阶段通过。

### 11.1 为什么 B 可行（先验证再动手）

在改 CNI 之前先手工建了两条隧道做判定（`tmp/k0s/overlay-probe.sh`）：

| 隧道 | `.94 → .140` | `.140 → .94` |
|---|---|---|
| VXLAN（UDP/4789） | 0% 丢包，0.26 ms | 0% 丢包，0.21 ms |
| IP-in-IP（proto 4） | 0% 丢包，0.21 ms | 0% 丢包，0.18 ms |

两类封装都能穿过这套 VPC，所以 Calico 的 `vxlan`（或 `ipip`）都能用；选了 VXLAN
（`spec.network.calico.mode: vxlan` + `overlay: Always` + `mtu: 1450`）。

### 11.2 k0s 不允许给已有集群换 CNI

改 `/etc/k0s/k0s.yaml` 的 `network.provider` 再重启控制面，k0s 会明确拒绝：

```
level=error msg="Failed to reconcile cluster configuration" component=clusterConfig-reconciler
  error="cannot change CNI provider from kuberouter to calico"
```

所以是**按 Calico 重建的集群**（`k0s stop` → `k0s reset` → `k0s install controller …` →
重新加 worker）。这也意味着：**CNI 是建集群时定下来的量，之后改不了**——新集群要先选好。
重建清单上没有额外代价（集群里只有验证负载），但要清三样旧 CNI 残留：上一轮的 pod 网段
路由、`/etc/cni/net.d/10-kuberouter.conflist`、以及 kube-router 的 iptables 链
（`.140` 上还跑着 compose，所以只删 `KUBE-ROUTER-*`/`KUBE-POD-FW-*`，docker 的
`DOCKER-*` 规则保持不动——清理后复查过 FORWARD 里的 DOCKER 引用仍在）。

Calico 起来后的事实：`vxlan.calico`（vxlan id 4096，local 172.18.80.94:4789，MTU 1450）、
到对端 IP 池的封装路由（`10.244.192.192/26 via 10.244.192.192 dev vxlan.calico onlink`）、
pod 从 Calico IPAM 拿到 `10.244.140.x` / `10.244.192.x`。镜像不需要镜像到 ACR——
k0s 用的三个 calico 镜像都在 **quay.io/k0sproject**（`calico-node|cni|kube-controllers:v3.32.1-3`），
quay 在这里是通的（只有 docker.io 不通）。

### 11.3 换 CNI 过程中暴露的四条

| # | 问题 | 证据与处置 |
|---|---|---|
| **F11** | **control-plane 不能多副本**：节点注册表是**进程内**的（`NodeRegistry._nodes` 是内存 dict，Redis 只承载配额台账与沙箱记录），而心跳被 Service 轮询到某个副本 | 直接分别问两个副本：副本 A 连续 12 次报 `fxf2j: unhealthy`，副本 B 同时报 `fxf2j: healthy`。后果：过时副本上 `/internal/routes` 返回 `502 Node ... unavailable`，放置只在它认为有容量的节点上发生，`reap_unhealthy`（E6.1）会把「不健康」节点上的活沙箱当孤儿回收——冒烟里那条 `404 Sandbox ... not found` 就是这么来的。**处置**：`control-plane` 回到 **1 副本**（与 compose 生产栈一致），并加用例钉住；要开多副本必须先把节点视图（健康+地址）共享出去 |
| **F12** | **清单里根本没有 buildkit**，而 `Template.build` 必须有它 | 冒烟模板阶段报 `BuildException: buildkit build exited with code 1`。**处置**：按 compose 的形态补 `buildkit`——但它不能用独立 Deployment，因为 buildkit 是**走 unix socket** 访问的（compose 把同一个 socket 卷只读挂到 control-plane 的 `/run/buildkit`），而 emptyDir 只在**同一个 pod 内**共享 ⇒ 做成 control-plane pod 的 **sidecar**，共享 `buildkit-data` emptyDir，`E2B_BUILDKIT_ADDR=unix:///run/buildkit/buildkitd.sock`，配置用 ConfigMap 承载并有「与 `deploy/stack/buildkitd.toml` 字节一致」的用例（docker.io 镜像加速链就写在里面）。`moby/buildkit:rootless` 在 Docker Hub ⇒ 镜像到 ACR（arm64+amd64）|
| **F13** | buildkit sidecar 加 `allowPrivilegeEscalation: false` 会让它起不来 | 报 `[rootlesskit:parent] error: failed to setup UID/GID map: newuidmap ... failed: newuidmap: Could not set caps` —— kubelet 因此给进程加了 `no_new_privs`，内核忽略 newuidmap 的 setuid 位（与 worker 清单里 file-capability broker 那条注释同一机制）。compose 的 buildkit 用的是默认值，所以这里也不设 |
| ~~**F14**~~ | ~~失败的 `Template.build` 会留下可解析的残骸~~ | ✅ **2026-09-18 已修（§16）**：失败时删掉 buildctl 半写的 tar/link，并让记录 `discard`（名字不再解析、盘上不留记录、`list` 不再展示；**内存里保留**，因为 SDK 正是靠 `…/builds/{id}/status` 拿到失败原因）。另外名字解析从「目录扫描最后一条」改成 **`created_at` 最新者优先**，这条治的是**存量**残骸（老集群上已经有）。实测：失败构建后共享卷上**零残留**，同名重建 1.3 秒成功并能按名字建箱跑命令；`smoke-template` 7 条同名记录时，重启控制面后按名字解析取到最新那条 |

### 11.4 现在的验证状态

`deployment_smoke.py` 在多节点 k8s（Calico VXLAN + NAS RWX）上：

```
NODE DISTRIBUTION: {'http://10.244.140.8:49983', 'http://10.244.192.203:49983'}
OK: commands + files through gateway
OK: migrated … -> …, files kept
OK: network config echo + atomic update
OK: volume mounted remotely + sibling volume isolated
after kill reservations: {…: 0, …: 0}
```

即 §6 B 的**多节点数据面阶段全部通过**（含跨节点分布、经 gateway 的命令与文件、
**跨节点迁移且共享 workspace 文件保留**、网络配置回显与原子更新、远端卷挂载与兄弟卷隔离、
kill 后预留归零）。剩下的第 5/6 阶段：模板构建现在能跑通（buildkit 生效、产物 49 MB 落到
共享 OCI 缓存、模板沙箱建得出来），但**模板沙箱内的 `commands.run` 超时**
（`connectrpc … Code.DEADLINE_EXCEEDED: Request timed out`）——这是下一轮要查的独立问题
（模板 rootfs/chroot 形态的命令路径；worker 同时会打
`E2B_PER_SANDBOX_UID is enabled on a root worker without CAP_SYS_PTRACE` 的告警，
root worker 形态可能需要补 `CAP_SYS_PTRACE`）。

**后续（2026-09-17）**：上面那条模板超时在 §12 收口；多副本形态（N13）在 §13 收口 ——
清单已从"钉死单副本"改成 `replicas: 2` + autoscaler MIN=2/MAX=16，并有专门判据脚本。

---

## 12. 收口 N18：(c) 解到节点本地 + (a) 不在事件循环上解（2026-09-17）

§11.4 之后 N18 剩三层（慢 240 倍 / 跑在事件循环上 / 预热协议对 SDK 不通）。这轮做了前两层，
第三条按设计保留（它对 SDK 调用方本就不通，见 §12.3）。

### 12.1 (c) OCI tar 留在共享卷，**解出来的 rootfs 放到节点本地**

新设置 `E2B_IMAGE_OCI_DIR`（未设 = 与 `E2B_IMAGE_CACHE_DIR` 同一个目录，即拆分前的行为）：

* **控制面**：`Template.build` 仍然把 OCI layout tar 导出到共享卷
  （`<E2B_IMAGE_OCI_DIR or E2B_IMAGE_CACHE_DIR>/_oci/<slug>.oci.tar`），因为它要发给每个节点；
* **worker**：`E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images`（`hostPath`，节点本地、跨 pod 重建保留），
  `E2B_IMAGE_OCI_DIR=/var/lib/e2b-sandboxes/_images`（共享 PVC）。解析时先看自己缓存里的 link，
  再看自己缓存里的 tar，**再看生产方目录里的 tar** —— 命中就从共享卷读 tar、解到本地缓存并在
  本地写 link；缓存里没有 `_oci` 的 tar 时，prune 会额外扫一遍生产方目录，分享出来的 tar 不会无界增长。

效果（同一台 worker、同一份 python-slim rootfs、2111 个文件）：

| | 拆分前（解到共享 NAS） | 拆分后（解到节点本地） |
|---|---|---|
| 层 blob → rootfs | 61.4 s | **0.26 s** |
| `deployment_smoke.py` 整轮 | 分钟级并在模板阶段超时 | **20.6 s 全绿** |

### 12.2 (a) 把 context（含镜像解析）的构建搬出事件循环

`SandboxRuntimeContext`（以及它内部的 `create_executor` → `resolve_image_rootfs`）原本由沙箱的
**首个 RPC** 惰性创建（`envd_service/rpc.py::_context`）——那正是 SDK 用 60 秒默认超时包住的
请求，而且跑在事件循环上，所以解析多慢，worker 就多久发不出心跳。现在 `POST /agent/sandboxes`
在**建箱成功之后**用 `asyncio.to_thread` 预建 context（`_prime_runtime_context`）：首个命令因此
直接命中，而且构建过程不再占用事件循环。预建失败**不影响建箱契约**——记一条 warning，首个命令像
以前一样报它自己的错（这也是 §10.3 F7 那条 `E2B_PER_SANDBOX_UID … without CAP_SYS_PTRACE` 告警
不再致命的原因）。

兜底仍在：`E2B_NODE_HEARTBEAT_TIMEOUT`（`1a17059`，默认 15 秒不变；k0s overlay 先设 300 秒，
2026-09-18 收到 **30 秒**——推导与实测见 §14）。有了 (a) 它是**安全网**而不是必需——
心跳不再会被解析拖住（§14 里用一次冷解析验证了这一点）。

### 12.3 保留、没有改的那条

控制面「冷镜像必须先预热」需要客户端带 `X-Sandbox-Id`（否则 `428 warm_required`），而
**e2b SDK 2.46.0 不发这个头也不处理 428**。模板创建之所以能工作，是因为
`Settings.resolve_template_image()` 对本地构建的模板返回 `None` ⇒ 预热被跳过 ⇒ 代价落在首个
命令上。曾经试着把模板的 image 补进去，冒烟立刻变成 428，所以撤回（`721dead` 记录了这段）。
现在 (c) 把那份代价从 61 秒压到 0.26 秒，**这条协议不一致就不再是拦路虎**；要不要让冷镜像预热
对不带头的客户端透明，仍是一个需要单独设计确认的问题（`docs/task-backlog.md` N18 的三条修法里
的 (b)）。

**2026-09-17 收口（c + a，见 §12）**：上面那条模板超时已解决，两条冒烟在真集群上全绿：

```
deployment_smoke.py  20.6 s  OK: 跨节点分布 / 命令+文件 / 迁移保留文件 / 网络配置 /
                               远端卷隔离 / 模板构建→worker 拉取→镜像 rootfs /
                               箱内 MCP 经代理 / kill 后预留归零
multinode_smoke.py    8.7 s  4 个沙箱 2+2 跨两节点，命令+文件+健康+stdin 全通过
DEPLOYMENT SMOKE OK / MULTI-NODE SMOKE OK
```

---

## 13. 收口 N13：多副本 worker 共用一份 base 不互相破坏（2026-09-17）

### 13.1 N13 到底在问什么

`E2B_WORKSPACE_BASE` 是**所有 worker 副本共用**的一份存储，所以每个 pod 的 reconcile 都会
走到**别人建的树**上。要证明的只有一件事：**一个 worker 的 reconcile/GC 不会动到另一个
worker 的活树**。判据（不再靠"看起来没出事"）：

1. 至少两个**真有 Running pod 撑着**的 worker；
2. 建的沙箱要**跨 ≥2 个 worker**分布，否则测的还是一条流水线；
3. 每个树的宿主 uid 从**磁盘上**读出来，**互不相同**、且都不是 worker 自己的身份；
4. 重启一个 worker（逼它做一次**起手 reconcile**，而共享 base 上全是别人的活树），
   事后断言 **4 棵树都还在**、幸存 worker 的沙箱**仍能读写自己的文件**、
   重启者的 reconcile 摘要 **`deleted=0`**；
5. 全杀掉后两个 worker 的预留都归零。

### 13.2 判据脚本与证据

新增 `deploy/scripts/multiworker_interference.py`（需要一个集群的 kubectl 来重启 pod 与读日志；
`--no-restart` 只跑 1/2/5 阶段，不碰集群）。真集群一整轮的输出：

```
OK: 2 workers healthy and pod-backed
SANDBOX DISTRIBUTION: {'85-qfmwd': 2, '85-xd4tf': 2}
OK: 4 sandboxes spread over 2 workers
OK: distinct pooled host uids, none of them the worker's: [10000, 10001, 10002, 10006]
== restarting e2b-worker-799b78bc85-qfmwd (forces a startup reconcile)
OK: both workers healthy and pod-backed again
OK: all 4 trees still on the shared base (no cross-deletion)
OK: every sandbox on a surviving worker still runs with its file intact
NOTE: 2 sandbox(es) lost their route when their worker restarted (N20, not an N13 failure)
OK: reconcile summaries show deleted=0 (protected_elsewhere=4, unmaterialised=2,
    disk_sweep_skipped=0 across 1 round(s))
after kill reservations: {'85-qfmwd': 0, '85-xd4tf': 0, '85-q8p75': 0}
MULTI-WORKER INTERFERENCE OK        # 3 分 23 秒
```

读法：重启的那个 worker 换了 node id（= pod 名），于是它**看不到任何一份自己名下的记录**，
共享 base 上 4 棵活树对它全都"无主"。它的起手 reconcile 把这 4 棵全部判为
`protected_elsewhere=4`、**一棵没删** —— 这正是 N13 要的结论：护栏认的是**舰队全集**
（`deletable = candidates - fleet_owned`），不是本 pod 的记忆。

上面这轮是 2026-09-17 的记录（当时 node id 还会变，所以带那条 `NOTE`）。**N20 修好后
（§15，worker 换成 StatefulSet）同一脚本的输出里那条 `NOTE` 消失了**，`protected_elsewhere`
也从 4 降到 2 —— 重启的 worker 现在会用**同一个 node id** 回来，把它名下的记录认回去，
另外 2 棵（对端的）才需要保护：

```
SANDBOX DISTRIBUTION: {'worker-0': 2, 'worker-1': 2}
OK: both workers healthy and pod-backed again
OK: all 4 trees still on the shared base (no cross-deletion)
OK: every sandbox on a surviving worker still runs with its file intact
OK: reconcile summaries show deleted=0 (protected_elsewhere=2, unmaterialised=3,
    disk_sweep_skipped=0 across 1 round(s))
MULTI-WORKER INTERFERENCE OK        # 2 分 37 秒
```

同一轮的两条冒烟（清单已是 `replicas: 2` 的多副本形态）：

```
deployment_smoke.py  45.9 s  OK: 跨节点分布 / 命令+文件 / 迁移保留文件 / 网络配置 /
                               远端卷隔离 / 模板构建→worker 拉取→镜像 rootfs /
                               箱内 MCP 经代理 / kill 后预留归零
multinode_smoke.py    7.8 s  4 个沙箱 2+2 跨两节点，命令+文件+健康+stdin 全通过
DEPLOYMENT SMOKE OK / MULTI-NODE SMOKE OK
```

### 13.3 清单解除 pin

N13 之前基线是钉死单副本的（`deploy/k8s/worker.yaml` `replicas: 1` + autoscaler
`E2B_AS_MAX_REPLICAS=1`，由单测钉住）。现在改回与 compose 相同的形态：

* `worker.yaml` `replicas: 2`，注释写明前置（跨节点锁）而不是"隐藏多副本"；
* `autoscaler.yaml` `E2B_AS_MIN_REPLICAS=2` / `E2B_AS_MAX_REPLICAS=16`；
  MIN 提到 2 是因为 **`draining: true` 是粘性的**（只有该节点重新注册才会清），
  空闲时缩到 1 会让"下一次扩容"先撞上一个被 drain 掉的节点，等于悄悄丢半个舰队；
* `strategy.maxSurge: 0` 当时**保留**，理由换成**容量**（这个 Deployment 没有 `requests`，
  Kubernetes 会把 2 CPU 的 limit 复制成 request，surge pod 在 4 核节点上根本排不下：
  实测 `0/2 nodes are available: 1 Insufficient cpu`）。**这一条后来被 §15 取代**：换成
  StatefulSet 后更新策略本身就是 delete-then-create，没有 surge pod 可排。
* 单测相应改名并改断言：`test_k8s_runs_the_verified_multi_replica_worker_shape`、
  `test_k8s_worker_is_a_statefulset_so_its_node_ids_survive_a_restart`。

**前置是存储的锁必须跨节点**：uid 池靠 `<base>/.uid_pool.lock` 的 flock 互斥，
阿里云 NAS 上只有 **NFSv4.0** 成立（v3+`nolock` 只是**本地**锁，跨节点不互斥 ⇒ 两个副本
可能发出同一个 uid）。这条已写进清单注释，别在 v3/`nolock` 的存储上照抄这份形态。

⚠ **镜像 tag 的坑（收口那轮踩到，2026-09-18 已消）**：`deploy/k8s-k0s/apply.sh` 会把**所有**
`byteplan/e2b-sandlock-*` 的 tag 统一替换成 `deploy/stack/.version` 里的那一个字符串，
所以收口 N18/N13 时 worker 与 control-plane 各推了**不同**的临时 tag 再用 `kubectl set image`
指过去 —— 而任何一次 `apply.sh` 都会把它们复位成 `.version`（当时是个更早的版本）。
**现在不用再这么做了**：`./deploy/scripts/build-and-push.sh` 会为每个组件推同一个版本号，
compose 栈与这台 k0s 集群都指到它（当前 `0.1.0-350-g212850d-20260918-152008`），
`apply.sh` 渲染出来的 tag 与线上一致。部署顺序就是「build-and-push → upgrade.sh（compose）
→ apply.sh 或 set image（k8s）」。

### 13.4 收口过程中另外修掉/新发现的问题

**修掉（真集群暴露，本轮改）**：控制面把**已经死掉的节点**当健康节点继续派活 ⇒ 建箱返回
`502 Node <id> unavailable`。根因是**孤儿判定窗口被复用成了放置窗口**：overlay 为了让共享
存储上的慢心跳不至于误判孤儿，把 `E2B_NODE_HEARTBEAT_TIMEOUT` 设得很宽（当时 300 秒，后来
按 §14 收到 60 秒），而这个窗口同时决定"还要不要往它上面放"。修法是在
`control_plane/registry/nodes.py` 里**另立**一个短窗口
`PLACEMENT_MAX_HEARTBEAT_AGE_S = 15.0`（worker 每 5 秒心跳 ⇒ 三次缺席即停派；`local://`
在进程内节点上豁免，和 sweep 一致），只影响**放置**，不影响孤儿判定（错误孤儿会抢走活沙箱的
槽位，那是 N18 的教训）。单测见 `tests/unit/test_node_registry.py`。

**新登记（未修，见 `docs/task-backlog.md`）**：

* ~~**N20** —— worker 重启后 node id 变了（pod 名），它承载的沙箱记录仍指向旧 id，
  控制面路由不到~~ ✅ **2026-09-18 已修（§15）**：worker 换成 StatefulSet，pod 名（= node id）
  跨重启稳定，E6.1 那条「分区恢复」路径终于能生效。
* ~~**N21** —— reconcile 轮次压在 worker 的事件循环上~~ ✅ **2026-09-18 已修**（轮次改成
  独立单飞 task，扫描与逐树校验挪到线程）：见 §14.4 与 `docs/task-backlog.md` N21。

---

## 14. 心跳窗口：300 秒 → 60 秒 → 30 秒，以及它背后的 N21（2026-09-18）

### 14.1 这个窗口管什么

`E2B_NODE_HEARTBEAT_TIMEOUT` 决定「多久没心跳就把节点当作没了」，而「没了」会让它名下的
**活沙箱**被 `reap_unhealthy`（E6.1）当孤儿回收、route-B 槽位被释放。所以它不只是活性判据，
还是一条**误判就杀活沙箱**的线 —— 这就是当初把它从默认 15 秒抬到 300 秒的原因。

300 秒是为 N18 抬的：worker 当时在**事件循环上**解冷镜像 rootfs，阿里云 NAS 上实测 61 秒
（本地盘 0.26 秒），本地构建的模板更久（3 分 35 秒），这期间发不出心跳 ⇒ 15 秒判 unhealthy。
N18 (c)+(a) 之后这条**理由**没有了，但「已经异步了」只覆盖**镜像解析**这一条路径：

* worker 的心跳与 reconcile 轮次在**同一个协程**里
  （`envd_service/agent.py::NodeAgent._loop`：心跳 → 可能跑一轮 reconcile → `sleep(5)`），
  所以**空档 = 5 秒 + 一整轮**，而不是 5 秒；
* 一轮的长度随共享 base 上的树数增长（扫描 `_scan_workspace_runtimes` 仍是事件循环上的
  同步调用，逐棵树读 `sandbox.json`），**没有上界**（N21）。

所以窗口只能先收一档（300 → 60），结构性的那条留在 N21 里 —— 见 §14.4 的修法与验证。

### 14.2 实测

新增 `deploy/scripts/heartbeat_gaps.py`：控制面的访问日志里每次心跳一行，用
`kubectl logs --timestamps` 就能拿到到达时刻，于是心跳空档可以**反复测量**而不必猜。

```
$ KUBECONFIG=... python deploy/scripts/heartbeat_gaps.py --since 15h
control plane control-plane-…: 21238 heartbeat(s) over --since=15h
configured window: 60s

e2b-worker-…-q8p75   beats=10566  median=5.01s  p95=5.01s  p99=5.03s  max=5.07s
  implied worst reconcile round: 0.07s
e2b-worker-…-xd4tf   beats=10694  median=5.01s  p95=5.01s  p99=5.03s  max=5.15s
  implied worst reconcile round: 0.15s
worst gap across nodes: 5.15s (of which up to 0.15s is a reconcile round)
```

两个 worker、14.7 小时、各约 1.06 万次心跳，控制面侧**最大空档 5.15 秒**，而且那时的 base
只有 10 个条目 / 2 棵树。另做了一次「N18 场景」的定向验证 —— 把 worker 节点本地的 rootfs
缓存挪走制造冷解析：

| 动作 | 客户端看到 | 同期心跳空档 |
|---|---|---|
| `POST /agent/images/…/warm`（冷解析：拉取 + 解到本地） | **18.1 s** | 最大 **5.13 s** |
| 带 `X-Sandbox-Id` 的冷建箱（预热后建箱） | **19.3 s** | 最大 **5.05 s** |

即那两段 18~19 秒的解析**完全没有碰到心跳路径** —— N18 那类伤害只剩 N21 那条（reconcile
轮次）了。

那一条是这样量的：把共享 base 灌到 **3002 棵树**（实测走一遍 ≈ 15 秒，一轮 reconcile 实测
**7.84 秒**），再分别用两条真实路径触发一轮：

| 触发 | 轮次时长 | 该 worker 同期心跳间隔 | 修之前会是多少 |
|---|---|---|---|
| 重启 worker（起手 reconcile） | 7.42 s | **5.00 / 5.02 / 5.01 s** | 5 + 7.4 ≈ **12.4 s** |
| 重启控制面（worker 重注册，**手里有活沙箱**） | 7.84 s | **5.00 / 5.01 s** | 5 + 7.8 ≈ **12.8 s** |

第二条同时量了 N21 的另一半 —— 扫描期间那个 worker 还答不答沙箱的文件 API：346 次探测
**最差 0.30 秒，超过 1 秒的 0 次**（17 次 `TimeoutException` 落在控制面重启、gateway 不可用
的那几秒里，不是 worker 的循环）。

### 14.3 取值

`deploy/k8s-k0s/control-plane-nfs.patch.yaml` 现在设 **30 秒**：

* **6 倍**实测空档（5.0 秒），而空档已经不再包含 reconcile 轮次（§14.4）；
* 比 300 秒**快 10 倍**回收死节点的预留 —— 宽窗口的代价是实的：死节点的
  `reservedMemoryMB` 会挂到超时为止，两副本的舰队里那等于半个集群的容量被冻住；
* **不能直接回 15 秒默认值**：那会和放置窗口 `PLACEMENT_MAX_HEARTBEAT_AGE_S`（15 秒）相等，
  而这条顺序（先停止派活、再宣布节点没了）是有意义的 —— 反过来会把新沙箱放到一个刚被回收的
  节点上。要回 15 秒，得连放置窗口一起下调，`tests/unit/test_worker_manifest_permissions.py`
  里有一条单测钉着这个顺序。

改这个值之前先跑一遍 `deploy/scripts/heartbeat_gaps.py`：它会拿**当前配置的**窗口当判据，
任何一次空档达到窗口就报错退出（那意味着控制面把一个**活着的**节点判成了 unhealthy）。

### 14.4 N21 的修法

`envd_service/agent.py`：

* **轮次独立成 task**（`_reconcile_round`，单飞：`_start_reconcile_if_due` 在一轮在跑时
  *不消费*触发条件，留到后面的心跳再跑，既不丢也不并发）。心跳循环因此只剩
  「注册/心跳 → 可能要起一轮 → sleep(5)」，空档不再包含轮次。轮次用自己的 HTTP client
  （心跳循环那个每个 pulse 结束就关掉了）；`NodeAgent.stop()` 现在把在跑的轮次一起取消。
* **扫描与逐树校验挪到线程**：`_scan_workspace_runtimes`（`iterdir` + 每棵树读一次
  `sandbox.json`）与 `_verified_teardown_plan`（每棵待删树一次 `lstat`/`resolve`）都是
  文件系统工作，现在走 `asyncio.to_thread`，所以扫描期间 worker 的文件 API 与命令 API
  照常应答（上面那张表第二行就是这条的直接证据）。
* 一个附带的行为变化：启动时现在只跑**一轮**恢复 reconcile。以前注册分支先跑一轮、而
  `_reconcile_pending` 没被消费，于是 5 秒后又跑一轮 —— 同一份 base 被白扫两遍。

契约测试在 `tests/contract/test_orphan_tree_gc.py::test_heartbeats_keep_their_cadence_while_a_round_scans_the_base`：
它把扫描**故意卡住**（用事件而不是 sleep，避免时序巧合），然后断言三轮心跳照发 ——
老形态下这条测试会卡死超时（已实测确认过 RED）。

---

## 15. 收口 N20：worker 换成 StatefulSet，node id 跨重启稳定（2026-09-18）

### 15.1 问题的形态

worker 的 node id 就是它的 pod 名（downward API `metadata.name`）。**Deployment 的 pod 名是随机的**，
所以每次重启都是一个「新节点」：

* 控制面把沙箱记录挂在**旧 id** 下，路由解析不到 ⇒ 客户端拿到
  `Node unavailable: All connection attempts failed`；
* 旧 id 会以僵尸节点的形式留在舰队视图里（死节点的预留也不回收）；
* 更要命的是，它让 E6.1 那条**本来就设计好的分区恢复**路径失效了 —— 那条路径的前提是
  「worker 用同一个 id 回来」：控制面把断开期间的记录标成 orphaned（**不删 workspace**），
  worker 重连后由恢复轮次报告「这些还在我这」，控制面再 un-orphan。随机 pod 名把「分区」
  变成了「永久丢节点」。

### 15.2 为什么是稳定 id，而不是让控制面做认领

一个诱人的替代方案是：让控制面把旧 id 的记录迁移给新 id。**不能这么做**，因为唯一能拿到的
「归属证据」是不可信的 —— worker 上报的本地运行时列表含共享 base 上**所有**树（每个 worker
都看得见别人的活沙箱，`_reconcile_with_control_plane` 的注释写明这个坑），拿它认领就是
**偷别人的沙箱**。干净的做法是让「这个 worker 是不是同一个 worker」有确定答案，而那正是
pod 名在表达式里承担的角色：把它变稳定，比事后猜要简单也要安全。

顺带一提，**compose 那边一直就是这样**（`E2B_NODE_ID: worker-1` / `worker-2`，容器名稳定）。
这次只是把 k8s 侧补齐到同一个语义。

### 15.3 改了什么

* `deploy/k8s/worker.yaml`：`kind: Deployment` → **`StatefulSet`**，pod 名变成
  `e2b-worker-0` / `e2b-worker-1`（跨重启不变）；`serviceName: worker-headless` 指向文件里
  本来就有的 headless Service；`podManagementPolicy: Parallel`（两个副本是对等的，不需要
  OrderedReady 串行启动）。原来的 `strategy.maxSurge: 0` 随之删除 —— StatefulSet 的更新
  本来就是 delete-then-create，没有 surge pod 可排。
* `deploy/k8s/autoscaler.yaml`：新增 `E2B_AS_K8S_KIND=statefulset`，Role 补上
  `statefulsets` / `statefulsets/scale`（缺了是硬 403，**实测踩到过**，不是静默无操作）。
* `autoscaler/backends/k8s.py`：按 kind 拼 REST 路径（`deployments` / `statefulsets`），
  未知 kind 在构造时就报错；`has_node`/`remove_node` 按 pod 名工作，两种 kind 通用。
  单测 `tests/unit/test_autoscaler_k8s_backend.py` 钉住两条路径与拼写错误的拒绝。
* `deploy/k8s-k0s/kustomization.yaml`：两个 worker patch 的 `target.kind` 跟着改成
  `StatefulSet`。**这一步漏了会很隐蔽**：patch 不生效，worker 就以镜像里的 uid 65534 跑，
  于是每个建箱都失败在 `<base>/.uid_pool.lock`（NAS 不让 65534 打开那个 root 建的 0600 文件）。
  单测因此改成**按对象解析**（`_rendered_workload`），只断言 worker 容器自己的
  `runAsUser`/`runAsGroup` —— 之前那句 `"runAsUser: 0" in out` 会被 control-plane 与
  init 容器里的同名键满足（新断言对这个 bug 已实测确认 RED）。

### 15.4 真集群验证

一个沙箱建在 `e2b-worker-0` 上，写一个文件，然后删掉这个 pod：

```
sandbox sbx_42a05737ec545b7a on e2b-worker-0
== deleting pod e2b-worker-0: the node id must come back as the same name
route after:  e2b-worker-0            ← 路由没变，记录没被回收
recovered in: 125.9s                  ← pod 重启（init + seccomp 检查）后文件 API 恢复
CP still lists it: 200
```

worker 日志给出的恢复路径正是 E6.1：

```
registered node e2b-worker-0 at http://10.244.140.49:49983
reconcile: restored sandboxes sbx_42a05737ec545b7a
reconcile summary: deleted=0 delete_failures=0 unmaterialised=3 protected_elsewhere=0 …
```

即：**同一个 node id 回来 → 恢复轮次把记录认回去 → 文件原样还在**。改之前这条链是断的
（旧 pod 名的记录谁也认不回，等窗口超时后连同 workspace 一起被回收）。

同一形态下的三条验证全绿：`multinode_smoke.py`（4 箱 2+2）、`deployment_smoke.py`
（含跨 worker 迁移 `e2b-worker-0 -> e2b-worker-1`、模板构建、箱内 MCP）、
`multiworker_interference.py`（N13；并且它那条 N20 的 `NOTE` 自己消失了，见 §13.2）。

### 15.5 这条修法覆盖不到的那一半（N22）

稳定 id 让**重启**不再是丢节点，但**真的不再回来**的节点仍在：`mark_orphaned`（E6.1）
把断开节点的记录标成 `orphaned`，而 `_ttl_reapable` **故意**让它（和 `paused`）跳过 TTL
过期 —— 因为那个 worker 可能还握着那些 inode，删掉 workspace 会制造"活着的孤儿 inode"。
代价是：节点行、沙箱记录、共享 base 上对应的树，在 worker 永不返回时**都没有回收路径**
（autoscaler 缩容掉的副本、下线或换机的节点）。这台集群上就还留着两条旧 Deployment 时代的
`e2b-worker-555d875875-*`，只有控制面重启才会清。登记为 **N22**：要给它一个远长于心跳窗口的
宽限（例如可配 `E2B_ORPHAN_RECORD_TTL`，默认关），而那是**有数据损失的取舍**，先定值再动。

---

## 16. 收口 N19：失败的模板构建不再留下可解析的残骸（2026-09-18）

### 16.1 两个独立的小毛病，合起来是一个大坑

`POST /v3/templates` 的 `create()` 会**先把记录写盘并绑定名字**，然后才构建。于是构建失败时：

* `_templates/<tpl_id>/template.json` 留在盘上 ⇒ 这个名字**仍然可解析**；
* 无 registry 形态下 buildctl 的 `--output type=oci,dest=<tar>` 会**先建出输出文件**，
  失败时留下一个 **0 字节**的 `_images/_oci/e2b-local_<tpl_id>.oci.tar`；
* 名字解析是「扫盘时最后走到的那条赢」（`_by_name` 在 `_scan_disk` 里被逐条覆盖），
  所以一条残骸完全可能在之后**抢回**这个名字。

拼起来就是 F14 那个症状：worker 去解一个空 tar，报
`Code.INTERNAL: file could not be opened successfully: … empty file` —— 报的是症状，
和真正的原因（上一次构建失败）没有半点关系。当时的绕法是 `tmp/k0s/reset-smoke-template.sh`
手工清残骸。

### 16.2 改了什么

`control_plane/registry/templates.py` 新增 `discard(record)`，并在**每一条失败路径**上调用
（`control_plane/api/templates.py::_discard_failed_build`，覆盖：构建上下文准备失败、
没有 buildctl、buildctl 退出码非 0、以及 `_run_build_with_slot` 的兜底 except 与
`_steps_to_dockerfile` 的入参错误）。它做三件事，**顺序和边界都是刻意的**：

1. 删掉 buildctl 半写的 tar 与它的 link（这些文件里没有任何有效数据）；
2. `record.discarded = True` 并解绑名字 ⇒ **名字不再解析**，盘上的记录目录也删掉，
   `list()` 不再展示它（`_write_record` 对 discarded 记录直接返回，所以任何后续
   `save`/上传都不能把它复活）；
3. **但记录留在内存里**。因为 SDK 正是靠 `GET …/builds/{id}/status` 拿失败原因的：
   把它一并删掉，用户看到的就从「buildkit build exited with code 1」变成
   「template not found」—— 一个困惑换成另一个困惑，不算修好。

另外把名字解析从「扫描最后一条」改成 **`created_at` 最新者优先**（`_bind_name`）。
这一条治的是**存量**：改动之前留下的残骸还躺在老集群的盘上，光靠「以后不再产生」是治不了的，
而「最新者赢」恰好等价于 backlog 里的第二个方案「名字解析绑定最近一次成功构建」——
失败的记录被 discard 之后，名字自然回落到上一次成功的那条。

### 16.3 真集群验证

```
== build 1: must fail (RUN false)
   failed as expected: BuildException: buildkit build exited with code 1   ← 失败原因照样报出来了
== 失败之后共享卷上：
   records: 没有新增（n19-probe 一条都不在）
   zero-byte tars: 0
== build 2: same name, must succeed
   built tpl_2bffa26f9eeed7a2 in 1.3s
== create from the NAME 并跑命令
   cat /n19-marker -> 'n19-ok\n'
N19 PROBE OK
```

第二条验证针对**存量**那条规则：这台集群上本来就积了 **7 条同名 `smoke-template`**
（历代冒烟留下的，正是 F14 的原始形态）。用最新那条打一个独占标记
（`RUN echo newest-wins > /n19-newest`），**重启控制面**（内存注册表清空，只能靠扫盘），
再按名字建箱：

```
cat /n19-newest -> 'newest-wins\n'
NEWEST-WINS OK
```

即解析取到的是最新那条，而不是「目录里 id 排序最后」的那条。

---

## 17. 收口 N12：删除沙箱时把那行配额也带走（2026-09-18）

### 17.1 症状与机制

删掉一个沙箱后，`xfs_quota -x -c "report -p"` 里那行还在，形如「0 used + 非零
`hard_blocks`」。原因是删除路径只做了两件事：`release_project`（`project -C`，清掉**目录**上的
项目态）和删目录（用量随之归零）。XFS 只在**用量与限额同时为 0** 时才丢掉记录，而限额没人复位
—— 于是行一直挂到下一次孤儿 reconcile。实测一次创建/删除潮留下 **40 行**，`POST /reconcile`
清掉 36 个（剩下 4 行是 worker 按设计留的 unmaterialised 树的载体）。

### 17.2 改了什么

* `xfs_quota.clear_project_limits(mount_point, projid, via_agent=...)`：把块限额复位
  （`limit -p bsoft=0 bhard=0` / `xfs_quotactl.clear_limit`），孤儿清理也改用它（去重）。
* **顺序是刻意的**：`_delete_sandbox_runtime` 在**树确实删掉之后**才调它（`SandboxTreeNotRemoved`
  的两条检查之后）。先复位限额会留下一个**没有任何磁盘限额的活沙箱**（树还在、行还在），
  而且删除若随后失败，这个沙箱就再也受不到限额约束。卷切片同理
  （`volumes.cleanup_volume_projects` 在每个切片删除成功后复位）。
* NFS/agent 形态补一个 op：`POST /project_limits {projid, mount} -> {cleared}`，客户端
  `QuotaAgentClient.clear_limits`（配额 agent 的 worker 侧本来就只有 `provision`/`release`/
  `report`/`reconcile`）。
* 顺带修一处**误导性记账**：`_reclaim_quota_rows` 原来把「本趟没报 cleaned」当成「没收回来」，
  而删除路径已经drop 掉的行永远不可能出现在那个列表里 —— 于是每次删除都会多出一条
  `quota row(s) not reclaimed` 警告。现在它问**配额表**（行还在不在），而不是信任那一趟的清单。
  这也是为什么那张表还留着：**延迟记账**（XFS 的 dquot 尚未结算，用量还没归零）是它唯一
  还在服务的场景 —— 那种行删除路径确实掉不了，只能等它结算。
* 另修一个脆弱点：`xfs_quotactl.available()` 在取不到 `libc.so.6` 的主机（macOS/musl）会抛
  `OSError`，而 `_use_quotactl` 是**每次配额操作**都会走的**后端选择**步骤 —— 一条带着旧
  project id 的记录就能让一次普通删除炸成未捕获的 OSError。现在 `_use_quotactl` 把
  「连问都问不了」归到 `subprocess` 回退（那里会给出更清楚的 `xfs_quota … failed`），
  `available()` 自身的契约保持不变（能力问题与可诊断性问题不是同一个问题，那条也有测试钉着）。

### 17.3 真 XFS 验证

用文档里的本地 lane（特权容器 + loop 设备，真实 XFS + prjquota）跑 XFS 门控契约：

```bash
docker run --rm --privileged --network host -v "$PWD:/workspace" -w /workspace \
  -e TMPDIR=/workspace/tmp_pytest -e E2B_REQUIRE_SECCOMP_FILTER=0 \
  e2b-sandlock-test:latest bash -c 'pytest tests/contract/test_xfs_project_quota.py tests/contract/test_volume_quota.py -q'
```

两条 N12 断言在**真 XFS** 上通过：

* `test_agent_delete_clears_project_and_dir`：删除后 `projid not in _report_rows()`；
* `test_delete_sandbox_cleans_only_its_own_slice`：A 的行没了，B 的行与用量照旧，
  volume root 不受影响（引用计数）。

`test_reconcile_removes_zero_usage_orphan_entry` 也改写成了它现在真正要覆盖的场景：**绕过删除
路径**（直接 `rmtree` 那棵树）留下的零用量行，仍然由 reconcile 收掉。

⚠ 这条 lane 在**改动前后一样**是 `5 failed / 5 passed`（用干净 HEAD 的 worktree 跑了同一命令
对比过），所以那 5 个不是这次带进去的。它们共用一个签名：`(soft, hard) == (8192, 8192)`
而测试期望 `(0, 8192)` —— 即 **fd 后端把 soft 也设成了 hard**（`xfs_quotactl.set_limit` 里
`<Q` 两处都写 `blocks`），而 subprocess 路径只设 hard。两条后端语义不一致，已登记为 **N23**。

### 17.4 生产目标机（`.140` compose 栈）验证

升级到 `0.1.0-350-g212850d-20260918-152008` 之后，直接在真机上量了一遍「行跟着沙箱走」：

```
rows before      : ['#0 19201056 0 0']
created          : sbx_29c04381dbd87bf5
rows while alive : ['#0 19201060 0 0', '#700308939 4 1048576 1048576']
rows after kill  : ['#0 19201064 0 0']
gone after kill  : ['#700308939 4 1048576 1048576']
N12 ON THE TARGET OK
```

即那一行随建箱出现、随删除**立即消失** —— 不再有「0 用量 + 非零 hard_blocks」的残留行。

⚠ 同一份输出也是 **N23 在生产机上的复现**：`#700308939` 的 soft 与 hard 都是 `1048576`
（1 GiB），而契约要的是「只设 hard」。（此前只在本地 lane 上见过，所以 N23 的证据现在
从"测试环境"升级成"生产栈"。）另外这次升级本身也顺手清掉了一批旧行：两个 worker 重启时
的启动 reconcile 把上一版留下的残留行收掉了。

---

## 18. N23 + N24：fd 后端与 `project -s/-C` 的语义对齐（2026-09-18）

### 18.1 一条线索带出的两个问题

§17.4 里那行 `#700308939 4 1048576 1048576` 有两个"不对劲"：soft 不该等于 hard（N23），
而一个**刚建好的**沙箱就占了 4 KiB —— 于是顺手量了一下"写进去的东西算不算数"：

| | 结果 |
|---|---|
| 沙箱项目自己的行 | 写 50 MiB 前后：**4 KiB → 8 KiB** |
| 默认 project 0 的行 | 同期 **+50 MiB** |

也就是说 **每沙箱磁盘限额在生产上完全没有生效**：限额挂在一个永远不涨用量的项目上，
而沙箱写的文件全部记在默认项目里。`quota_maintenance` 的 near/over-limit 也永远不会触发。

### 18.2 根因：fd 后端只碰"它拿到的那一个 inode"，而 `project -s/-C` 是递归的

实测（特权容器里，真 XFS）：

```
$ xfs_quota -x -c 'project -s -p $V/probe 4242' $V
 4242 -------------------P-- probe
 4242 -------------------P-- probe/sub        ← 设之前就存在的子目录，也被打上
 4242 ---------------------- probe/sub/f      ← 之后创建的文件，靠继承
$ xfs_quota -x -c 'project -C -p $V/probe 4242' $V
    0 ---------------------- probe / probe/sub / probe/sub/f   ← 三层全清
```

而 fd 后端（`xfs_quotactl.assign_projid` / `clear_projid`）用的是
`FS_IOC_FSSETXATTR`，**只作用于传入的那一个目录**，靠 `PROJINHERIT` 让*之后*创建的东西继承。
建箱顺序恰好踩中这个差别：`<base>/<id>` 先建、`<id>/workspace` 再建、**然后**才 provision ——
于是 `workspace/` 永远停在 project 0：

```
<tree>                    projid 1436317985  flags P
<tree>/workspace          projid 0            ← 先建后设，不继承
<tree>/workspace/probe.bin projid 0           ← 沙箱写的文件
```

两边同样偏窄的还有清理方向：`cleanup_orphan_project` 只清目录，而"记录丢了、文件保留"的
孤儿场景要求把**整棵树**去项目化（否则用量还挂在那个项目上，行永远掉不掉）。

### 18.3 改了什么

* `xfs_quotactl.assign_projid_tree(root, projid)` / `clear_projid_tree(root)`：深度优先走一遍
  **已存在**的条目 —— 目录用 `assign_projid`（projid + PROJINHERIT），文件用新的
  `assign_file_projid`（只写 projid，文件上 PROJINHERIT 没有意义）。两者都用
  `O_NOFOLLOW` 并跳过软链接：链接指向的 inode 属于别人，不能被拉进本项目的计量。
* `provision_project` 的 fd 分支改用它（等价于 `project -s`），失败清理改 `clear_projid_tree`。
* `cleanup_orphan_project` 的 fd 分支改 `clear_projid_tree`（等价于 `project -C`）。
* `release_project` **保持单目录**：它的调用方（删箱、删卷切片）紧接着就删掉整棵树，
  走一遍纯属浪费 —— 这一点写进了注释，免得以后有人误以为它也是全树语义。
* `xfs_quotactl.set_limit` 只设 hard、soft 显式写 0（N23）。单测里那条
  `(projid, hard, soft)` 断言原来是 `(10001, 8192, 8192)`，它的主题本是 hard 的**字节数**，
  soft 是顺带带上的；现在按契约改成 `0`。

### 18.4 验证

**XFS 契约 lane**（特权容器 + 真 XFS + prjquota）：从"5 failed / 5 passed"变成
**10 passed / 1 skipped** —— 那 5 条正是被这两个问题挡住的（用量不计入 / 限额不生效 /
soft≠0）。新增单测 `test_assign_projid_tree_reaches_entries_that_predate_provisioning`
钉住走树的覆盖面（每个既有目录、每个文件都覆盖，软链接跳过）。

**生产栈（`.140`，升级到 `0.1.0-349-g8b8ae49-20260918-150140` 之后）**：

```
created: sbx_f9d233e1f10f8e2f -> new rows: [442880324]
row at create : used=4 KiB soft=0 hard=1048576 KiB     ← N23：soft 是 0
after 50 MiB  : used=51204 KiB (+50.0 MiB)             ← N24：算在沙箱自己头上
dd past the limit: ["dd: error writing 'workspace/fill.bin': No space left on device", 'exit=1']
row after kill: False                                  ← N12 没有回归
```

即：限额真的开始生效了（`dd` 写到 1 GiB 被 ENOSPC 挡住），而行的生命周期仍然正确。

### 18.5 ⚠ 运维影响（这一条要提前说）

**从这一版起，每沙箱磁盘限额是真的了。** 之前"看起来有配额、实际无限"，
现在默认 `E2B_DEFAULT_DISK_MB`（这台是 **1 GiB**）会真的把超限写入挡下来。
如果现有工作负载本来就会写超过 1 GiB，升级后它们会开始报 `No space left on device`
—— 要按需调 `E2B_DEFAULT_DISK_MB`（或建箱时传 `diskMB`）。

---

## 19. 收口 N22：不再回来的节点，记录终于有回收路径（2026-09-18）

### 19.1 为什么它一直被留着

E6.1 的设计是「断开 ≠ 删除」：`mark_orphaned` 把丢失节点的沙箱记录标成 `orphaned`，
而 `_ttl_reapable` **故意**让它（以及 `paused`）跳过 TTL 过期 —— 那个 worker 可能还握着这些
inode，删掉 workspace 会制造"活着的孤儿 inode"。这个保守选择是对的，但代价是：
**一个再也不会回来的节点，会把它的记录、配额行、以及共享 base 上对应的树永久留下**。

N20 修好之后（worker 换 StatefulSet，node id 稳定），"重启"那一类已经自愈 —— 同一个 id
回来，恢复轮次把记录 un-orphan。剩下的是**真的不再回来**：autoscaler 缩容掉的副本、
下线/换机的节点。这台 k0s 集群上就曾留着两条旧 Deployment 时代的
`e2b-worker-555d875875-*`。

### 19.2 改了两半，其中一半是无条件的

**① 孤儿记录的宽限（可选，默认关）**：新增 `E2B_ORPHAN_RECORD_TTL`（秒；`0` = 永不回收，
即今天的行为）。开启后，记录在**孤儿化**超过该时长后可以被 TTL 扫走：

* 时间戳取自记录**状态翻转的那一刻**（新增 `orphaned_at`，随记录持久化），而不是每次扫描时
  刷新 —— 否则节点一直不回来，宽限永远不流逝；
* 恢复（`recover_node` 把记录翻回 `running`）会**清掉**这个戳，所以下一次断开有它自己的宽限。
  否则一个"很久以前孤儿过、又活过来"的沙箱，会在再次断开的瞬间被回收 —— 期限属于**这次
  断开**，不属于这个沙箱；
* 旧版本留下的孤儿没有这个戳，只在**下一次**断开被打上后才开始计时（保守）。

**默认关闭是有意的**：这是有数据损失的取舍 —— 那个 worker 真的在宽限之后回来，它的沙箱
已经被删了。要启用的话，宽限应当取得**远大于心跳窗口**（心跳窗口是秒级，管的是"暂时联系
不上"；这里管的是"这台机器退役了"），例如一天（`86400`）。

**② 空节点行的清理（无条件，安静超过 `10` 个心跳窗口）**：一个
**unhealthy + 预留全为 0 + 名下无记录**的节点行，既不参与放置（它不 healthy）、不影响记账
（没有预留）、也不影响 worker 用来围栏磁盘扫描的舰队枚举（它贡献不了任何 id）——
它只是让运维读舰队视图时多跳过一行。这一半不需要开关：它不丢任何东西，而且正是它把
pod-名时代残留的那种行收掉。

### 19.3 验证与现状

单测覆盖四件事：宽限到期才回收、默认之下**永不**回收、恢复清戳（下一次断开重新计时）、
以及节点行的清理条件（有记录 / 有预留 / 刚安静下来的行都保留）。全量 **1325 passed**。

这一批（`0.1.0-350-g212850d-20260918-152008`）已推到两套栈：compose 栈两条冒烟全绿，
k0s 集群两条冒烟全绿、舰队视图回到 2 个健康节点。那两条旧 id 的行是随控制面重启消失的；
**清理逻辑本身针对的是"以后不会重启控制面"的场景**，所以它由单测覆盖，而不是靠现场观察。

---

## 20. compose 线停用（2026-09-18）：k8s 成为唯一部署形态

目标机 `.140` 上的 compose 栈已 `docker compose down --remove-orphans`（**卷保留**：
`sandlock_sandbox-shared` 2.7 G、`sandlock_redis-data`、`sandlock_buildkit-{data,sock}`），
本机到它的那条 13000 转发也已关闭。之后的常态流程只有一条：

```
./deploy/scripts/build-and-push.sh     # 所有组件同一个版本号，写 deploy/stack/.version
deploy/k8s-k0s/apply.sh                # 或 kubectl set image，两者都用那个版本号
```

`deploy/scripts/` 里其余的（`bootstrap-target.sh` / `upgrade.sh` / `smoke.sh`）保留作参考与
应急，不再作为常态流程 —— 见 `deploy/scripts/README.md` 顶部的告示。

### 20.1 这次切换带来的一个能力缺口（要记账）

**k8s 形态没有每沙箱磁盘硬限。** compose 那套之所以有，是因为它的工作区在宿主 **XFS** 上，
而且配额 agent 可以跟栈一起跑；k8s 这边的共享卷是**阿里云托管 NAS（NFS）**，而 NFS 上的
每沙箱配额只能由**NFS 服务端**执行 —— agent 得跑在那台我们碰不到的存储服务器上。所以：

* 这条不是"忘了写清单"：`deploy/k8s/` 本来就没有 quota-agent 清单是有原因的，
  现在 compose 停用后它变成**主线唯一的实能力缺口**；
* 现在 k8s 侧还剩的**软**信号：worker 心跳带 `usedDiskMB`、控制面的 disk-warn/error 计数
  （`§2.4.4` 的口径）、以及节点层的容量记账（`E2B_NODE_DISK_MB`，避免把节点塞满）；
* 一个跑飞的沙箱**可以**把 50 GiB 的共享卷写满，进而影响同一 base 上的所有 worker ——
  这是这条缺口的实际风险面。

**方案的完整评估已单独成文：[`docs/disk-quota-options.md`](disk-quota-options.md)（2026-09-18）**。
要点：① 阿里云 NAS 的**目录配额就是硬限**（通用型 NFS，服务端执行、worker 零成本；
代价是 500 目录/文件系统、GiB 粒度、需要 RAM 凭据）；② 不受存储类型限制的方案里
「硬 + 便宜」不存在（写路径记账要给最高频 I/O 加中介往返，loop 镜像要特权且 NFS 上不稳，
cgroup 没有空间配额）；③ 无论走哪条，都要补**卷级水位闸门** —— 共享存储下磁盘准入应当是
**卷级台账**，而不是现在这种每节点一份的声明预算。

原来的三条路线（供对照，细节见上文档）：

1. **换共享存储**：自建 XFS + NFS 服务端（或 CephFS），在服务端跑 agent —— 能力对齐 compose，
   代价是运维一台存储；
2. **worker 侧软执行**：定期按 `usedDiskMB` 检查每棵树，超 `diskMB` 就 kill/pause 沙箱 ——
   不需要 XFS，但语义从"写不进去"变成"超了就被处理"，且需要定阈值与宽限；
3. **工作区改节点本地 XFS**：硬限天然成立，但丢掉共享 base 的多副本/迁移语义（N13 那套），
   等于换架构。

在这三条里挑之前，先明确一个口径问题：**k8s 主线上，"每沙箱磁盘配额"是必须的硬需求，
还是可以接受的降级？**

---

## 21. 卷级磁盘闸门上线并复验（2026-09-18，N25 / L1）

`E2B_MAX_TOTAL_DISK_MB` 现在**显式**写在 `deploy/k8s-k0s/control-plane-nfs.patch.yaml`
（= 10240，与代码默认值和 compose 时代同口径 ⇒ **不改行为**，只是把它从"藏在默认值里"变成
"写在清单上、能被 `workspaceDisk` 看到"）。完整取舍见 [`docs/disk-quota-options.md`](disk-quota-options.md) §7。

**给运维的两个查询**（唯一的卷级磁盘信号；节点视图里的 `diskTotalMB` 是整台 NAS，永远不准）：

```
kubectl -n sandlock get deploy control-plane -o jsonpath=\
  '{.spec.template.spec.containers[0].env[?(@.name=="E2B_MAX_TOTAL_DISK_MB")].value}{"\n"}'
curl -s -H "X-Internal-Key: $E2B_INTERNAL_API_KEY" \
  http://172.18.78.49:3000/internal/fleet/metrics | python3 -m json.tool
# -> workspaceDisk {reservedMB, limitMB, warn(>=85%), saturated(>=100%)}
```

**拒绝时会说明白是工作区，而不是"没有资源"**：

```
POST /sandboxes -> 503
{"code":503,"message":"shared workspace disk budget exhausted: 1024 MiB reserved of 10240 MiB"}
```

非磁盘维度（内存/CPU/进程/并发数）**保持** `No resources available` —— E9.3/E9.4 的重试与
排队路径按那句话写的。**两条闸门都会说这句**（fleet 台账、以及节点
`E2B_NODE_DISK_MB` 聚合），因为单节点形态下先触发的是节点那条。

**实测（2026-09-18，`0.1.0-362-g6f94522`）**：临时把 fleet 预算设到 1100 MiB，第 1 个沙箱
201、第 2 个 503 且文案如上（节点侧聚合是 8192×2 = 16384 ⇒ 能报出 1100 的只可能是 fleet
闸门）；复验后已回滚到 10240，`workspaceDisk.limitMB` 确认 = 10240、`activeSandboxes=0`、
两个 worker 的预留都回到 0。`deployment_smoke.py` 与 `multinode_smoke.py` 均通过。

> 跑 smoke 时记得 `E2B_INTERNAL_API_KEY` 也要导出（脚本默认值 `internal-key` 与集群不符，
> 否则会在 `/internal/routes` 上 401）。

### 21.1 每沙箱超限会被暂停（N25 / L2b，2026-09-18）

卷级闸门管的是**卖出去多少**；这一条管的是**实际写了多少**（前者看不见后者）。

* **谁测**：worker 周期性 walk 每棵沙箱树（它拥有这个挂载；控制面侧
  `record.workspace_dir` 对远端沙箱**故意为 `None`**，所以它不做这件事）。间隔
  `E2B_DISK_ENFORCE_INTERVAL_S`（默认 **30 s**，`0` = **关闭这条闸门**）。
* **怎么判**：实测 > 该沙箱创建时的 `diskMB`（默认 `E2B_DEFAULT_DISK_MB=1024`）。
* **怎么办**：**暂停**（不是 kill）：状态保留、预留释放（global/tenant/节点 slice 都还回去）、
  worker 上的进程被冻结。日志：`sandbox <id> over its workspace budget (N MiB used of M MiB): pausing it`。
* **恢复路径**：删掉超额文件后 `POST /sandboxes/{id}/connect` 恢复。**仍超预算就会在下一个心跳
  （≤30 s）内被再次暂停** —— 这是"暂停而非 kill"的代价，换来的是现场不丢。
* **成本**：整树 walk 在这台 NAS 上是 **10.9 ms / 2 000 文件、35.7 ms / 10 000 文件**
  （单文件亚线性，贵在目录：≈2.5 ms/目录）。不需要 inotify（实测推翻了"walk 太贵"这个前提），
  见 `docs/disk-quota-options.md` §5.2。扫描另有 1 s/轮预算 + 游标轮转，不会因为一棵巨树饿死其他树。
* **节奏与开销（N25，2026-09-19，`0.1.0-391-g6d8b084`）**：扫描从**事件循环上**挪到 worker 线程（单飞 + 心跳只读缓存，与 N21 给 reconcile 的形状一致）⇒ 间隔不再受"轮次占循环多久"约束，默认从 30 s 收到 **5 s**。实测：心跳 median 5.01 s / max **5.08 s**（改动前 5.15 s），同一次写入 2.7 GiB 后超预算→**暂停 6 s**（此前最坏 30 s）。
  仍然存在的下限：报告**搭心跳**，所以比 5 s 更密的扫描只让数字比"被读取的那一刻"更新鲜；而整树 walk 下陈旧度其实是 `间隔 × ⌈树数 ÷ 每轮扫到的树数⌉`（每轮只有 1 s 预算）。**脏目录记账（L2c）就是让第一项占主导的那一步。**
  顺带量到并记账：`files.write` p50 **131 ms**、`files.read` 44 ms、`commands.run("/bin/true")` **92 ms** ⇒ **统一写者身份让每次上传多付一条命令的钱（≈90 ms）**；要压回去只有"一次 exec 写多个文件"或"常驻 helper"（后者会牵动 RLIMIT 语义，见 §22 C）。
* **用量有第二处落点（N28/D，2026-09-19）**：worker 报的每个实测值都会写进控制面的沙箱记录
  （`workspace_disk_used_bytes`），于是 `GET /sandboxes/{id}/metrics` 的 `diskUsed` 在 k8s 上
  **是真实值**了（`sample_metric()` 以前只在 `workspace_dir` 非空时 walk，而远端记录永远为
  `None` ⇒ 恒为 0）。实测值**每次上报都记**（不是只在要暂停时记），否则舰队的每沙箱磁盘数字
  只会存在于"刚被冻住的那些"身上。只在数值变化时才回写存储，稳态一棵树不等于每次心跳一次写。
* **平台主动暂停会说明原因（N28/D）**：日志行是
  `sandbox paused: its workspace grew past its budget (N MiB used of M MiB)`，并且同一个原因会
  随暂停推送给 worker，被**拒绝写入/命令时的文案**引用：
  `Sandbox is paused: its workspace grew past its budget (1340 MiB used of 1024 MiB); ...`。
  调用方自己 `pause()` 的沙箱不会带这段（没有原因可讲）。

**可伸缩性（已记录，未实施）**：上面这版每轮走整树，成本随"树里目录数 × 沙箱数"线性涨（实测
venv 形状 ≈1.27 s/棵）。要把它降成"只重扫脏目录"（预期稳态 **2.4 ms/轮**且不随树增长）需要改
mediator（Rust），方案、盲区与测试计划见
[`docs/disk-accounting-dirty-dirs.md`](disk-accounting-dirty-dirs.md)；为什么不用 inotify / COW
见 [`docs/disk-quota-options.md`](disk-quota-options.md) §5.3（实测）。

---

## 22. 统一写者身份 + 暂停真的冻结（N28，2026-09-19）

上一条（L2b）是"**事后**发现超了就把你冻住"，它成立的前提是"暂停 = 不再消耗"。这个前提当时
**不成立**：实测暂停状态下 `files.write`（新建/覆盖）、`files.make_dir`、`files.remove`
**全都成功**，连 `commands.run` 都能执行。于是"暂停"只做了一半 —— 释放了准入预留，却没停住
实际消耗。

这一节落地四件事，缺一不可：

**① 暂停门控（A）**：`state != running` 时，

| 入口 | 结果 |
|---|---|
| `POST /files`（SDK `files.write` / 上传） | **409** `Sandbox is <state>; its files can only be modified while it is running (resume it first)` |
| `MakeDir` / `Move` / `Remove` | **400** `failed_precondition`，同样带原因 |
| `process.Process/Start`（SDK `commands.run`） | 同上（流式应答以 EndStream error 结束） |
| `Stat` / `ListDir` / `GET /files` | **照常**：只读不消耗，且运营方要能在 resume 前看一眼 |

判定点是**同一个** `state`，两条入口各读一次：`connect/router.py::_require_running`（RPC）与
`http/auth.py::require_http_sandbox(mutating=True)`（HTTP）。

⚠ 修这条时发现并补上了一个**真缺口**：`/agent/sandboxes/{id}/pause|resume`（远程交付路径）
以前只冻结进程组、**从不改 worker 自己的运行时记录**。远端沙箱的 worker 记录永远写着
`running`，所以门控在分离部署下等于不存在。现在这条路径会 `set_state` 回写（顺便带上原因）。
契约：`tests/contract/test_pause_write_gating.py`（可移植，含 agent 路径）、
`tests/contract/test_pause_resume_sandlock_multinode.py`（真 sandlock，用"暂停时 marker 写不进去"
证明冻结）。

**② 统一写者身份（B）**：工作区树的写**只有沙箱自己**。worker 不再 `open()`/`mkdir()`/`rmtree()`
沙箱树 —— 它把同一件事作为**沙箱内的命令**跑（`/bin/sh -c`，路径走 argv 不做字符串拼接，字节走
stdin），然后只做**读**回填响应。路径语义不变（`/foo` 仍然落在 `<树>/foo`）。

* 实现：`envd_service/filesystem/writer.py`（`SandboxWriter`）；
  `FilesystemOps` 退化成"读 + 写前判断"（`require_creatable/movable/removable`）。
* 这些内部命令**不占**每沙箱命令闸门（默认并发 1，用户起一个 `sleep` 就会把写卡 30 s 后 429），
  也**不进**命令日志（`command_logs` 是给 SDK 看的"我跑过什么"）。见 `ProcessManager.start(internal=True)`。
* 上传仍保留"临时文件 + rename"（E4.2），只是搬进沙箱内做；超限时杀进程并清理临时文件。
* 前提：镜像里有 `/bin/sh`。本舰队所有镜像（`python-mcp`、`python:3.11-slim`）都有；scratch
  形态的镜像会**明确报错**而不是偷偷换回 worker 身份写。
* **写与暂停赛跑（实测发现并已修）**：暂停若落在写已过闸门之后，沙箱内的 helper 会被
  `SIGSTOP` **冻住** ⇒ 调用方的请求一直挂在里面（实测入口 nginx **60 秒后 504**），临时文件也留在
  树上；而"暂停了却还有一个写在长"正是暂停要阻止的事。现在 `pause_all` 对**内部写 helper**
  改为中止（用户命令仍然冻结/解冻，它的输出要留着），写者把"被信号杀掉 + 记录不是 running"
  翻成**同一个 409**：`Sandbox is paused[: 原因]; the Upload was interrupted by the pause rather
  than frozen (resume it and retry)`。
* **上传 metadata 在这套存储上是 no-op（实测，非本次引入）**：E2B 的 `x-metadata-*` 会以
  `user.e2b.*` xattr 落到文件上，而**阿里云 NAS 对该命名空间返回 `ENOTSUP`**（沙箱内
  `os.getxattr` → `[Errno 95] Operation not supported`）。所以接口照旧返回调用方给的 metadata
  （那是回显请求），但**不落盘**；写者只在每个沙箱上告警一次（以前是静默吞掉）。要真正支持得
  换存储或改存侧记录。

**③ 单文件硬限（C，`RLIMIT_FSIZE`）**：fork 侧新增 `max_file_size`（builder + profile
`[limits].file_size` + supervise policy + C ABI + Python/Go 绑定），在子进程里同时压低软硬限，
并把 `SIGXFSZ` 设为忽略 —— 这样越界的写返回 **EFBIG**（程序看得懂的"文件太大"），而不是
默认动作**把进程杀掉**。

取值 = **这个沙箱被卖过的最大额度**（`diskMB` 与各挂载卷 `perSandboxQuotaMb` 取大），
所以它**永远不会拒绝一个合法大小**；任何一维是"不限"（0）时不设限（编不出一个诚实的数）。
因为它是 per-process，口径上还覆盖"沙箱写的任何文件"；在本形态里可写面就是**工作区树 + 挂载的卷**
（镜像 rootfs 对沙箱是只读的：实测写 `/tmp` 得到 `Permission denied`）。

**④ 记账收口（D）**：见 §21.1 的两条（`diskUsed` 真实值 + 暂停原因进文案）。部署清单里
显式写上 `E2B_DISK_ENFORCE_INTERVAL_S=30`（`deploy/k8s/worker.yaml`）。

> 这一组做完，"磁盘"这条线是：**卖多少**（L1 卷级台账）→ **写了多少**（L2b 实测 + 现在有落点）
> → **暂停真的停住**（A）→ **谁在写只有一个身份**（B）→ **单文件不可能越过被卖的额度**（C）。
> 仍然没有的是**写到一半的 ENOSPC**（per-write），它需要写路径中介记账，见
> `docs/disk-accounting-dirty-dirs.md` §13。

### 22.5 fork 侧 push 上报 + 运行中进程 prlimit（N25，2026-09-19）

§22.4 的结论是"再往下压延迟，要换『数从哪来』"。这一条就是换：**中介（fork）把自己的写者视角推给平台，
同时让"剩下的额度"直接由内核执行到正在写的进程上**。

两条路各自解决一半，合起来才成立：

| | 解决的问题 | 机制 |
|---|---|---|
| **push 上报** | 平台的数字**看不到正在写的文件**（§22.4：按路径 `stat` 连续 2~4 秒 ENOENT，然后在写完那一刻跳到最终大小） | 中介在 `openat` 时就拿到了"这个文件即将被写"的那个 fd，之后**读它自己的 offset**，把增量推给 worker |
| **prlimit 下压** | "一条命令内部的循环"本来就没人拦（per-exec 上限在 execve 时就定死了） | worker 把"还剩多少"下发给 slot，slot 把它 `prlimit` 到**正在跑的整棵进程组**上 |

#### 22.5.1 push 上报：为什么是 offset，而不是 size

一个反直觉但决定设计的实测（沙箱内、同一个 inode）：

```
持有 fd 的 lseek(SEEK_END)      写 900 MiB 的过程中 0.28 s 就已经是 900 MiB
从 worker 按路径 stat            连续 ENOENT 2~4 s，然后一次跳到 900 MiB
```

也就是说：数据早就进了页缓存（写者只是被回写节流），但**worker 的挂载点看不到**。而中介不一样——
它**就是**执行这次 `openat` 的进程：它自己打开文件、把 fd 注入给沙箱，所以它手上那个描述符指向的
inode 与沙箱写入的 inode 是同一个，offset 也就是写者自己的视角。

落地：

* `dirty.rs::WriteFds`：只登记**写意图**的 `openat`（与 §22.2 的脏目录同一个判定），键是
  `(pid, fd)`，值是**路径 + 打开瞬间的文件大小**（在那个 fd 上 `lseek(SEEK_END)`，读完立刻恢复
  原偏移，不干扰沙箱共享的 file description）。登记发生在内核 ADDFD **回包之后**
  （`InjectFdSendTracked`），所以沙箱拿到 fd 之前账就已经记上。
* `append_watch.rs`：周期性读 `/proc/<pid>/fdinfo/<fd>` 的 `pos:`，把差值累加成
  **"至少追加了多少字节"**。四条规则**全部倾向于少报**：从 baseline 起算（所以"打开→写完→关闭"
  这种短命 fd 只要被采到一次就能整笔记上）、offset 回退不倒扣、同一文件多个 fd 只取最大、
  没采到的 fd 就是没算（那部分仍会由文件系统 walk 在稍后补上）。
* `sandlock-supervise --events-fd N`：**单独一个单向描述符**，不是复用控制通道——控制通道是
  request/response，往里插非应答帧会让所有读者（含旧 wheel）把"下一帧"当成自己的应答。
  帧格式是 **NDJSON**（一行一个 JSON 对象），Python 侧 `readline` + `json.loads` 就够，不需要任何
  分帧代码。发布线程独立于 serve 线程：`wait_child` 会阻塞 serve 线程整条命令的时长，而**那正是
  事件最该发的时候**。

worker 侧的契约只有一条，而且是保守方向的：

* 一轮上报 `walk 值 + 自那一轮以来 push 到的追加量`，**并把它消费掉**（walk 值已经包含当时已提交的部分，
  不消费就会重复计）；
* per-exec 上限那条路径**只读不消费**（`refresh_disk_usage` 是 peek），否则同一笔字节会被两边各算一次；
* 报告的仍是一个**下界**：可能早（好事），不会晚，也不会比真实值大——除了"同一窗口里删了同样多的数据"
  这一个已知边界（这种沙箱会被判超限，而超限的动作是**可恢复的暂停**）。

#### 22.5.2 prlimit：把"还剩多少"交给内核执行

`update_file_size_limit {bytes}`（新 verb）：

* **只能收紧**：对每个目标进程取 `min(当前, 上限)`，软硬限一起压；传一个更大的值是 no-op 而不是授权。
* **整组**：`RLIMIT_FSIZE` 是 per-process 的，所以扫的是每个活子进程的**进程组**（与 `kill_child`
  同一个单位），shell 循环里 fork 出来的 `dd` 因此也在内。
* **不能吃自己的尾巴**（实测发现并修）：worker 给的是"**整树**还剩多少"，而被写文件**自己已经写进去的字节
  已经算在 used 里**，直接按这个数施加会让文件把自己卡死 —— 树里有 900 MiB 的合法文件，剩余掉到 324 MiB
  的那一刻它就被拒绝。所以施加的上限是 `下发值 + 该进程组内被跟踪 fd 已增长量`（中介本来就逐 tick 在
  读这些偏移）。语义因此变成"**这个文件可以一直长到整树到达预算**"，而**之后**新开的文件拿到的仍是
  干净的"剩余"。（这一条只在 E2B 形态里才暴露：测试夹具用了 pure 形态，没有 `openat` 通知，也就没有
  可跟踪的 fd —— 所以它先在 chroot 形态的用例里才被抓住。）
* **没有天花板就拒**：实例的 `max_file_size` 正是装 `SIGXFSZ=SIG_IGN` 的那一步（`context.rs` 13d）；
  没有它，越界的写会按默认动作**把进程杀掉**，那不是可用的配额语义。所以没天花板的代直接拒绝。
* **一代之内单调**：本代已下发过的值不会被更高值覆盖；新 exec 重新拿到按当时剩余算的新上限。

worker 侧三条克制：只在**实质下降**时下发（默认 4 MiB 步长）、**每 0.5 s 最多一次**、**永不放大**。

> 这一条让"一条命令内部的循环"从"只能等暂停闸门"变成"内核在预算处直接拒绝下一次写"
> （EFBIG，程序看得懂的错误），而暂停闸门退化成最后一道兜底。

#### 22.5.3 集群实测（`0.1.0-404-g01fa514-20260919-134431`）

| 项 | 改前（只有 walk + 冻结） | 改后（push + 收紧） |
|---|---|---|
| 冻结时"平台测到的使用量"（3×900 MiB / 1024 预算） | **1800 MiB（超 776）** | **1025 / 1148 / 1148 MiB（超 1~124）** |
| 冻结延迟（同上，5 次） | 3.7~4.0 s | 2.8 / 2.9 / 3.3 / 3.3 / 3.9 s |
| "跨过预算"→"暂停"的间隔 | — | **约 20~60 ms**（日志时间戳） |
| 一条命令里死写一个文件 | 一直写到被暂停 | **`dd: error writing …: File too large`，命令自己以 rc=1 结束，沙箱仍在 running** |
| 合法的大文件 | — | 900 MiB 文件在 1024 预算下**正常写完**（rc=0） |
| 增量账本 | 稳态 `ledger=13 rebuilt=0 walk=0` | 稳态 `ledger=46 rebuilt=4 walk=8`（同量级） |
| per-exec 上限 / 账本 / 冒烟 | 324/1/1、树 1026 MiB、逐字节相等、两个 smoke OK | **完全一致**（无回归） |

延迟数字要这样读：**剩下的 2.8~3.9 s 是"写者真的写到 1024 MiB 需要多久"**，不是平台的反应时间——
平台一旦看见跨过预算，20~60 ms 内就冻上了（旧代码里那一段是 1 s 级）。真正的收益在"超支"那一行：
从"超 776 MiB 才被发现"变成"超 1~124 MiB"。

#### 22.5.4 边界（都是设计选择，不是缺陷）

* **只有 chroot（route-B）形态有这条通道**：pure 形态根本不 trap `openat`（`chroot_path_syscalls()`
  只在 chroot 形态进计划表），所以没有可登记的 fd。E2B worker 走的就是 chroot 形态。
* **打开→写完→关闭都落在同一个采样间隔内的 fd 会被漏掉**（默认 100 ms）。这一格不影响正确性，
  只是"早"变成"晚"：那些字节仍由文件系统 walk 记到。
* **同一窗口内大量删除**会让下界偏大，导致提早暂停（可恢复）。

#### 22.5.5 开关

| 变量 | 侧 | 默认 | 作用 |
|---|---|---|---|
| `SANLOCK_APPEND_INTERVAL_MS` | fork（slot 进程继承 worker 环境） | 100 | 采样 open write fd 的间隔；就是"平台多久能看见新增写"的上界 |
| `E2B_DISK_APPEND_TRIGGER_MB` | worker | 8 | 自上一轮以来追加超过这么多，就**立刻叫起一轮**（不等节拍） |
| `E2B_DISK_APPEND_MIN_INTERVAL_S` | worker | 0.2 | 两次"叫起"之间的最短间隔（轮次是贵的那一半） |
| `E2B_DISK_TIGHTEN_STEP_MB` | worker | 1 | 剩余额度**至少**降这么多才值得发一次 verb |
| `E2B_DISK_TIGHTEN_INTERVAL_S` | worker | 0.1 | 两次收紧之间的最短间隔；**就是"新 fork 出来的进程继承到的那份剩余额度有多旧"**（§22.5.7） |

几个都为"少说话"服务：push 只在**有增长**时发帧（空闲沙箱一个字节都不发），收紧只在**实质下降**时发。
但 `E2B_DISK_APPEND_TRIGGER_MB` 是反方向的：它让**字节数**而不是时钟决定什么时候重新测量 ——
这一条是集群实测补上的，见 §22.5.5。

#### 22.5.6 超支为什么不是零：轮次间隔就是超支

第一版 push 上线后，集群仍测到 1~124 MiB 的超支（冻结在 1148 MiB / 1024 预算）。日志把原因指得很清楚：

```
13:58:45.032  tightening … (used 1073741824 of 1073741824)   ← 正好等于预算，不算"超过"
13:58:45.959  budget crossed → pause                          ← 927 ms 之后才看到 >1024
```

也就是说：**数字是实时的（push 做到了），但 worker 只在 `E2B_DISK_ENFORCE_INTERVAL_S=1 s` 的节拍里去看它**。
写者 ~130 MB/s，927 ms ≈ 124 MiB —— 超支 = `轮次间隔 × 写速`，与"看得见看不见"无关。

改法就是让**字节数**触发那一轮：追加超过 `E2B_DISK_APPEND_TRIGGER_MB`（默认 8 MiB）就叫起一轮，
最快每 `E2B_DISK_APPEND_MIN_INTERVAL_S`（默认 0.2 s）一次。于是超支变成
`触发阈值 + 一轮 + 上报 ≈ 8 MiB + 几十 ms`，而空闲沙箱仍然一个字节都不发、`ENFORCE_INTERVAL_S`
仍然是"没人写时的兜底节拍"。
另外 `update_file_size_limit` 的下限与 per-exec 上限共用 `E2B_DISK_EXEC_LIMIT_FLOOR_MB`（默认 1 MiB）——
一个文件总得能装下一点东西。

#### 22.5.7 那 124 MiB 真正的来源：额度是按"每个文件"发出去的（N25，2026-09-19）

§22.5.6 把超支解释成"轮次间隔 × 写速"，能解释一部分，但不是**这次**测到的那个数。
同一 shape 连跑两次，日志给出的数字完全一致：

```
16:59:00.988  disk tightening: sbx_e3a9… limited to 130023424 bytes (used 943718400 of 1073741824)
16:59:02.009  disk tightening: sbx_e3a9… limited to 1048576  bytes (used 1203765248 of 1073741824)
16:59:02.030  agent pause sandbox sbx_e3a9…            ← 冻结在 1148 MiB
```

`900 MiB 已写 → 剩余 124 MiB`，冻结时是 **1148 = 900 + 124 + 124**。命令是
`for i in 1 2 3; do dd of=part$i.bin count=900; done`：`part1.bin` 写完之后，
**剩下的两个文件各自被允许再写满这 124 MiB**。

原因是 `RLIMIT_FSIZE` 按**进程**生效、并且被继承：收紧把 124 MiB 写进 shell 的 limit，
之后每 fork 一个 `dd` 都**各自继承一份完整的 124 MiB**。所以"剩余额度"被当成每文件一份发出去，
总超支 ≈ `剩余的额度 × 之后新开的文件数`。

同一个 bug 还有一个更危险的方向。为了让"合法的大文件不被自己的尾巴切掉"，
收紧原本会把该进程组已增长的量加回额度（`allowance = 剩余 + group_grown`）。
但那一步在这套部署里**从来没生效过**：watch 的 key 是通知里的 pid，而收紧遍历的是宿主 pid，
两者在 `pid_ns` 下永远不相等（见下），于是 `group_grown` 恒为 0。
如果它"修好了"却仍按进程组发放，shell 会拿到 `124 + 900 = 1024 MiB`，
后面每个文件都能写到 1 GiB —— 超支反而会从 124 MiB 变成约 900 MiB。

**根因：通知里的 pid 是沙箱自己命名空间的 pid。**

`SeccompNotif.pid` 由内核按**触发线程自己所在**的命名空间给出，不是按 listener 的。
集群实测：writer 的 `NSpid: 121 7`，mediator 收到的是 `7`，于是

```
sandlock-supervise: fdinfo read failed: /proc/35/fdinfo/5: No such file or directory
route-B slot …: watch state {'bytes': 0, 'dropped': 0, 'watching': 0}   ← 每一个 tick 都是 0
```

两个 worker 累计 **1515 个 tick 全是 `watching: 0`** —— 也就是说这条 push 通道
**从上线起就没工作过一次**，数字一直只靠 walk，收紧只能落在轮次上。

顺带纠正一个曾经写进代码注释、但**实测不成立**的假设：不能靠
`/proc/<沙箱宿主 pid>/root/proc/<ns pid>/…` 换个 `/proc` 看。沙箱里的 `/proc` 就是
**pod 的 procfs**（实测 `/proc/<pid>/root/proc/self` 解析到 pod 级 pid，
而 `/proc/<pid>/root/proc/<ns pid>` 根本不存在），所以只能**翻译** pid。

改法是复用仓库里已有的翻译表 `procfs::PidNsMap`（`/proc` 视图与 teardown 扫描用的同一张，
按 pid namespace inode 区分沙箱），`handle_chroot_open` 在登记 write fd 之前先把
通知 pid 翻成宿主 pid。这样三件事同时对：

1. watch 读得到 `/proc/<宿主 pid>/fdinfo/<fd>`；
2. 收紧遍历的宿主 pid 能和 watch 的 key 对上，"大文件不被自己的尾巴切掉"才真的生效；
3. 额度按**进程**归属而不是进程组最大值发放 —— 一个不写文件的 shell 只拿到"真正剩下的"，
   它后来 fork 的每个文件都从同一个数开始，而那个数会随着 push 立刻变小。

回归测试就落在 shape 上：`crates/sandlock-supervise/tests/supervise.rs` 的
`instance_policy_chrooted_pid_ns`（`pid_ns: true`）用在
`test_events_fd_reports_a_running_writers_growth` 上，断言**推送字节数精确等于写入字节数**。
修之前这个用例必然失败（`watching: 0` → 推送 0 字节），修之前的那版 fixture 没有 pid ns，
所以整个单测套件全绿而生产是死的。

超支的**残余**就是"上一次收紧时的剩余额度"，由 `E2B_DISK_TIGHTEN_INTERVAL_S`
决定它有多旧：0.1 s × 实测写速 ~250 MB/s ≈ 25 MiB，而不是 124 MiB。

#### 22.5.8 通道接上之后剩的三件事（N25，2026-09-19 晚）

§22.5.7 改完，集群上 `watch state` 第一次出现 `watching: 1`、worker 打出
`disk append +1048576 bytes (pending …, wakeup=True)` —— 通道真的通了。但同一版立刻暴露两个新问题，
而且**都不是 push 本身**：

**① 同一个字节被算了两次。** 一轮的上报原本是 `walk + pushed`。walk 是"已提交"，
push 是"沙箱自己看见的增量"，一旦回写落地，同一批字节**同时**出现在两者里。
实测：文件真实 287 MiB 时 worker 报 **647 MiB**，于是"剩余"被算成 377 MiB，
而收紧把"剩余 + 自身已写"发下去 = 664 MiB < 文件继续长到的 **696 MiB** —— 一个**合法的 900 MiB 文件被 EFBIG 切断**。

改成增量恒等式：

```
reported = max(walk, 上一次上报 + 本次 push)      # push 为空时就是 walk
```

一个字节无论被 push 看见、被 walk 看见、还是两者都看见，都只算一次。
每一次收紧的 `增长` 诊断把这条链路摊开了（`SANLOCK_EVENT_TRACE=1`）：

```
tighten targets=[25, 34, 40] grown={40: 300941312, ...} bytes=395313152 watch_entries=1
```

**② 沙箱会把注入的 fd 挪走。** `exec 9>>file` 把注入的描述符 dup 到 9 并**关掉原来那个**，
于是 watch 记录的 `(pid, fd)` 在几毫秒内就指向一个已关闭的描述符，每个条目在第一个 tick 就被丢弃
（这正是"刚修好 pid 翻译、`watching` 又是 0"的原因）。现在读不到精确 fd 时，按**路径**在该进程的
fd 表里回退查找（`/proc/<pid>/fd/<n>` 的解析目标 == mediator 打开的宿主路径，允许内核的 `" (deleted)"` 后缀）。

**③ PID 命名空间表可能是空的。** `PidNsMap::new` 在 leader **正在进入自己的用户命名空间**时读
`/proc/<leader>/ns/pid`，那一刻进程不再 dumpable，非特权读者会被拒；`.ok()` 把这个失败吞掉，
留下**一张空表**——沙箱自己 `ls /proc` 一个数字都看不到（`/proc/self` 都不存在），
watch 也翻不出任何 pid。现在：inode 读不到会在下次 refresh 重试，仍读不到时按**进程树**判定归属
（只需 world-readable 的 `/proc/<pid>/stat`），并且表为空时必然打一行 stderr，带上 leader pid 与 inode 状态。

**集群最终实测**（`0.1.0-405-g8ca0551-20260919-192955`，`tmp/k0s/probe_push_and_tighten.py`，三次）：

| 指标 | 值 |
|---|---|
| A. 1024 MiB 预算冻结时的超支 | 124 / 246 / 124 MiB（验收线 1/4 预算 = 256 MiB）→ **PASS** |
| B. 单命令内失控写者 | 内核 EFBIG 拦住、命令自己退出、沙箱仍在运行 → **PASS** |
| 合法的 900 MiB 单文件 | rc=0、`ls -l` = 943718400 → 不再被切断 |
| per-exec 上限 / ledger / smoke | 324+1+1 MiB、6144=6144、12144=12144、单机与多机 smoke OK |

**残余超支是采样粒度，不是记账：** 这类写者 ~250 ms 就写满一个 124 MiB 文件，而 mediator 每
`SANLOCK_APPEND_INTERVAL_MS`（100 ms）采一次描述符，所以整文件可能落在**一个采样内**；
两个这样的文件可以在两轮之间开完，各自继承"上一次收紧时的剩余"。实测阶梯：

```
52.837  used 900 MiB → limit 124 MiB
53.716  used 1148 MiB → floor → 冻结     # 中间两个文件各写了 124 MiB
```

要再压这一截，只有两条路：把 `SANLOCK_APPEND_INTERVAL_MS` 调到 10 ms 量级（采样更细，
代价是 slot 侧 syscall 变多），或者把"剩余额度"在 **open 时**按当前在写的描述符共享分配
（现在它是**每进程继承**的，而额度是"剩余"——这就是每个新文件各拿一份的来源）。
本轮把唤醒节拍调到 `E2B_DISK_APPEND_TRIGGER_MB=1` / `E2B_DISK_APPEND_MIN_INTERVAL_S=0.05`
（超支从 124–339 MiB 收敛到 124–246 MiB），并把这条残量写进清单注释。

#### 22.5.9 两条路都做了：open 时发放 + 中介自持 fd（N25，2026-09-20）

按 §22.5.8 结尾的两条路做完，`0.1.0-408-g98052a0-20260920-120350` 实测：

| 指标 | 之前 | 现在 |
|---|---|---|
| A 超支（3×900 MiB / 1024 预算） | 124–339 MiB | **1 MiB** |
| 合法 900 MiB 单文件 | rc=0 | rc=0 |
| B 单命令失控写者 | EFBIG 拦住、沙箱继续运行 | EFBIG 拦住；沙箱被冻（见下） |
| 单文件（700 已用） | 324 MiB | **324.0 MiB**（正好剩余） |
| 紧接着的第二个文件 | — | **1.0 MiB**（池已花光的下限） |
| 串行两次 900 MiB（新沙箱） | 324/1/1 | 324.0 / 1.0 MiB，树收敛 1025 MiB |

**B（open 时共享分配）**：`WriteFds` 现在保存一个**已花计数**（`observe` 逐步累加，
所以关闭的 fd 也保留它的贡献）和 worker 最后一次给的额度；`openat` 时按
`baseline + (额度 − 自那以后已花)` 发放，下限 1 MiB，且**不低于该进程现有描述符已有的最大授予**
（否则同一进程再开一个文件会把正在写的那个文件的额度压低——`RLIMIT_FSIZE` 分不开同进程的两个文件）。
判断发生在 `open` 那一刻，所以"每个文件各继承一份剩余额度"在结构上消失了。

**A'（中介自持 fd + 更快采样）**：内核的 fd 注入是把 listener 的描述符 **dup** 进沙箱，
两者是同一个 open file description、同一个偏移，所以中介直接读自己的
`/proc/self/fdinfo/<fd>` 就够了——不需要 pid 翻译、不需要别的进程的 `/proc` 权限，也不会被
`exec 9>>` 这类重定向搬走。它现在是第二选择（沙箱自己的 fd → 中介自持 → 按路径扫描），
`SANLOCK_APPEND_INTERVAL_MS` 因此调到 **20 ms**。

**在这两条路上又踩到三个坑，都各自补了测试：**

1. **发放必须是"值"，不能是单向棘轮。** 一个 shell 为 `>/dev/null` 开文件时池恰好为 0，拿到下限 1 MiB；
   之后它 fork 的每个 `dd` 都继承这 1 MiB ⇒ 合法文件在剩余 324 MiB 时只落到 256 MiB。
   现在 `open` 只设**软限**（`min(hard, 授予)`，可回升），**硬限**仍由 worker 的收紧单调下压。
2. **push 与 walk 是同一个量的两个估计，不能相加。** 一次 700 MiB 的填充被报成 777 MiB
   （中介的采样晚于 walk 落地），凭空多出的 77 MiB 让下一个文件只剩 247 MiB ⇒ 改成
   `max(walk, 自上一次"无事可推"的轮次以来累计推送量)`，并让"无事可推"的那一轮把推送侧重新锚定到 walk
   （这也是删除后数字能降回来的地方）。
3. **收紧时的"文件已写量"必须是当下的。** 之前用的是 watch 上一次采样的值，而"剩余"来自更新的 walk：
   实测文件已经 113 MiB 而 `grown` 还停在 54 MiB，于是 `剩余 + grown` 少了 59 MiB，
   合法文件停在 263 MiB。现在收紧前先**现读**中介自持的 fd（`WriteFds::refresh_held`）再算额度。

**超上限 = 不能写,不是冻结(2026-09-20 定,已实现)。** 下限 1 MiB 原本的意义是"超预算的沙箱还得能写小文件、
能删东西",但它会把树推到 `预算 + 1 MiB`,而控制面的判定是严格的 `used > budget`
（`control_plane/registry/manager.py`）⇒ 用满预算就被冻结,把"删文件自救"这条唯一的出路一起拿走了。现在的语义:

| 状态 | 行为 |
|---|---|
| 树 < 预算 | 正常:per-exec 额度 = `预算 − 已用`,写者只能用到预算为止 |
| 树 ≥ 预算 | **所有写失败**(`RLIMIT_FSIZE = 0` ⇒ EFBIG);**新建条目失败**(`O_CREAT`/`mkdir`/`symlink`/`link` 由中介返回 ENOSPC,因为建一个空名字不写一个字节也能把树撑大) |
| 树 ≥ 预算 | **读、exec、删除、rename、stat 全部照常**;一旦腾出空间(且记账看到),下一次命令立刻能再写 |
| 控制面 | 只**记录**数字并打一条穿越日志;不再暂停沙箱,也不释放它的预留 |

所以三个下限的残留问题一起解决了:①额度不再有 1 MiB 的余量 ⇒ A 的超支从 1 MiB 变成 **0 MiB**
（三次 run 全部正好停在 1024 MiB);②沙箱不会被冻结;③删除能把空间拿回来。

**这里踩到的最后一个坑是 NFS 的 `.nfsXXXX`。** A' 让中介持有 fd 副本以后,"删除"实际上**不释放空间**:
NFS 会把"已被 unlink 但仍被打开"的文件改名为 `.nfsXXXX` 并保留其块,而中介的副本就是最后一个持有者 ⇒
实测删掉 900 MiB 文件后平台上报的数字 12 秒都不下降,沙箱因此永远无法恢复写入。修法是让 watch 去问
**沙箱自己**还有没有描述符指向这个文件:没有就把持有副本能看到的最后偏移记一次、随后**放手**
（释放 fd ⇒ NFS 才真正回收空间)。`exec 9>>` 把 fd 搬走的情况走的是同一个问题,答案是"还有",于是继续跟。

**集群最终实测（`0.1.0-412-gd359196-20260920-135802`）**：

| 指标 | 之前 | 现在 |
|---|---|---|
| A 超支（3×900 MiB / 1024 预算） | 776 MiB | **0 MiB**（三次 run 全部 1024 MiB 整） |
| 合法 900 MiB 单文件 | rc=0 | rc=0 |
| 单文件（700 已用） | 324 MiB | **324.0 MiB**（正好剩余） |
| 紧接着的第二个文件 | — | **0 MiB**（ENOSPC，池已空） |
| 串行两次 900 MiB | 324/1/1 | **324.0 / 0 MiB**，树收敛 **1024 MiB** 整 |
| 超预算时新建文件 / 建目录 | 允许 | **ENOSPC 拒绝** |
| 超预算时删除 | 允许（但空间被 .nfs 钉住） | **允许且真正释放** |
| 腾出空间后再写 | — | **可以** |
| 沙箱是否被冻结 | 是 | **否** |

#### 22.5.10 天花板归平台、软限可回升、unlink 立刻归还（N25，2026-09-20）

**起于一个问题**：删除文件后空间立刻释放，走的是"实例级的限制"，那为什么 workload 还能自己调限制？

**先纠正前提：内核里没有"实例级文件空间限制"。** `RLIMIT_FSIZE` 是**进程级**的，而且只管**单个文件的大小**，
不是"这个沙箱总共能写多少"。这个部署里的"实例上限"一直是两样东西拼出来的：**硬限**（launch 时的 ceiling）
+ **中介侧的池**（预算 − 已用 + 已释放）。内核的 setrlimit 规则是：任何进程都能**下调**自己的 limit，
也能把 `rlim_cur` 抬到 `rlim_max`（不需要特权）；只有 `rlim_max` 抬不上去——沙箱里实测
`setrlimit((1<<40,1<<40))` → `ValueError: not allowed to raise maximum limit`。

**"workload 能自己调限制"的来源**就是这里：可动的那一半原本放在**硬限**上（收紧时下压 `rlim_max`，
单向、不可回升），而 workload 只要把自己的 `cur` 抬到 `max`，就拿到中介没打算发的那份额度。

**现在的分工**：

| 值 | 谁动 | 能不能回升 |
|---|---|---|
| `rlim_max`（硬限） | 只在 launch 时设定 = 实例上限 | 平台不动；guest 动不了（EPERM） |
| `rlim_cur`（软限） | per-exec 额度、open 授予、实时收紧、unlink 后归还 | 可以，且只有平台能抬 |

闸门：`setrlimit`/`prlimit64` 落在 `RLIMIT_FSIZE` 上、且**抬高** `cur` 或 `max` → `EPERM`；**下调放行**；
其他资源一概不碰；沙箱自己的 init 路径（supervise 自身，镜像里够不着）例外。沙箱内用 ctypes 直打 syscall
（绕开 CPython `resource` 自己的用户态检查，否则会误判成"闸门生效"）：

| 调用（树 1000/1024 MiB，`cur`=24 MiB，`hard`=1024 MiB） | 结果 |
|---|---|
| `setrlimit(soft=hard)` | `-1 EPERM` |
| `prlimit64(0, RLIMIT_FSIZE, soft=hard)` | `-1 EPERM` |
| `setrlimit(max=1 TiB)` | `-1 EPERM` |
| `setrlimit(soft=cur+64K)` | `-1 EPERM` |
| `setrlimit(soft=cur/2)`（下调） | `0` |
| 之后正常 `dd` | 正常（没有误伤） |

**"同一条命令里删了再写"可用**：`unlink`/`rmdir` 成功时中介按**删除前**的 `symlink_metadata` 把字节记进
`freed`（目录记 0），并**当场**把这份额度通过 `prlimit` 交还调用者（软限 = 剩余 + 该进程已写量），
不等下一轮记账。实测 `probe_delete_then_write.py`：填满 1024 MiB → 一条命令 `rm -f fill.bin; dd of=after.bin 1MiB`
→ `rc=0`、`after.bin=1048576`；下一条命令照样能写。

**代价**：单个文件的上限仍然等于实例上限（`RLIMIT_FSIZE` 分不开同一进程的两个文件），"总量"约束靠
**中介的发放入口 + 池**，而不是内核的总量限制——内核本来也没有。

**同一轮的最后一个坑：决策点用的账必须是当下的（`ec2921e`）。** 硬限不再下压之后，`probe_exec_limit.py`
立刻抓到一个此前被那条"棘轮"掩盖的漏洞：700 MiB 已用 + 一个 324 MiB 文件写完，**紧接着**第二个文件，
第二个文件落了 **48 MiB**。根因不是某一侧算错，而是**两侧都在滞后**：watch 按自己的 tick 采样，
worker 的账本由 dirty 事件驱动重扫，决策落在两次采样之间时，中介的 `remaining()` 只补偿"到达之后"的增长，
worker 的样本又比真实小 48 MiB。中介本来就为每个它中介过的文件**自持一份 fd**（内核 dup，同一 open file
description），所以"现在多大"只差一次 `fdinfo` 读——现在 `is_exhausted`（"这个 open 能建文件吗 /
这个 `mkdir` 能跑吗"）、open 发放、unlink 后的归还三处都先 `WriteFds::flush_held()` 再算。实测同一形状
**0/8** 越界（此前一次运行放过 48 MiB），`probe_exec_limit.py` 回到 324.0 / 0，串行 324.0 / 0，
树收敛 **1024 MiB 整**。

**集群验收（`0.1.0-414-g68b9196-20260920-154249`）**：

| 探针 | 结果 |
|---|---|
| `probe_push_and_tighten.py` A（3×900 MiB / 1024 预算） | **0 MiB** 超支（三次 run 全部 1024 整）PASS |
| 同 B（一条命令里的失控写者） | 拦住写 + 删除可用 + 腾空间后可写，且**不冻结** PASS |
| `probe_exec_limit.py` | 单文件 **324.0 MiB**；第二个文件 **0 MiB**；串行 324.0 / 0，树 1024 MiB |
| `probe_second_file_race.py`（同一形状 ×8，两命令之间不停顿） | **0/8** 越界 |
| `probe_delete_then_write.py` | 同命令删后写 `rc=0`，`after.bin=1048576` |
| `probe_guest_raise_ctypes.py` | 抬高全 `EPERM`、下调 `0`、之后正常写 |
| `probe_dir_ledger.py` | 6144=6144、12144=12144，逐字节相等 |
| `deployment_smoke.py` / `multinode_smoke.py` | 全绿；kill 后预留归零 |
| fork 测试 | core lib **887** 通过；supervise 28 通过 / 1 预存在失败（`test_supervise_path_serve_launches_instance_and_serves_verbs_until_shutdown`，未改动的树上同样失败） |
| 主仓单测 | 1190 通过 / 13 既有失败（macOS 上的 `test_priv_helpers` 11 + `test_xfs_quotactl_backend` 2） |

**还没解决的（这一轮量到了，记在 backlog N31）**：账本是**字节**账本，所以它看不见"条目"——
沙箱里建 2000 个空文件，平台 `diskUsed` 全程 **0 字节**，连 NFS 上每个目录至少 **16 KiB** 的目录块也不计
（`dir_size`/`dir_ledger` 只累加非目录条目）。唯一挡"建条目"的闸门是"池**恰好**为 0"，而空条目不消耗字节
⇒ **数量无上限**，真实风险是 inode/元数据耗尽（整卷故障）与整树 walk/GC/快照的成本随条目数线性上升，
不是容量。顺带量到建条目本身很贵：**200 个空文件 ≈ 4.2 s（≈21 ms/个）**，且这个速率在 200→2000 之间
不随目录变大而改善；`rm -rf` 2000 个文件 20.8 s；一条命令里建 20000 个文件会把命令的响应流打断。

**顺带记一条运维事实**：每次 worker 滚动重启都可能把 base image 预热打断，缓存目录里只剩
`…sha256_….lock`（没有实体目录），此时 `Sandbox.create()` 会回 `428 warm_required`，多机 smoke 因此失败。
预热端点是 **POST**（GET 只是查询）：`POST /agent/images/<urlencoded-ref>/warm`，带 `X-Internal-Key`
打在该 worker 的 `127.0.0.1:49983` 上。

#### 22.5.11 单文件天花板到底盖住了哪些写路径（N25，2026-09-20）

`RLIMIT_FSIZE` 只在 `generic_write_checks`（write/pwrite）、`inode_newsize_ok`（truncate/ftruncate）、
`vfs_fallocate` 里检查，**page fault 路径不查**——所以"mmap 越界扩容"一直被当成这套方案的已知裂缝。
在集群上逐个量过之后，结论是**它在这台存储上不是裂缝**（`docs/disk-quota-options.md` §6 已记过一半：
越 EOF 直接 SIGBUS，并注明"换存储要重新验证"；这一轮把它量全了）：

设置：预算 1024 MiB，先填 900 MiB（天花板只剩 ~124 MiB），再尝试落一个 300 MiB 的文件或扩展。

| 路径 | 结果 |
|---|---|
| `dd`（write） | EFBIG，**正好停在 124 MiB**（130023424 B） |
| `ftruncate`（300 MiB） | `Errno 27 File too large`，文件 0 字节 |
| `fallocate`（300 MiB） | `Errno 27 File too large`，文件 0 字节 |
| `copy_file_range`（逐 MiB） | EFBIG，停在 **62.2 MiB**（低于天花板，原因见下） |
| `sendfile`（逐 MiB） | EFBIG，停在 **123.1 MiB** |
| `splice`（经 pipe，逐 MiB） | EFBIG，停在 **124.0 MiB**（正好天花板） |
| mmap `MAP_SHARED`，在**最后一页内**越过 EOF 存储 | 子进程 exit 0、**文件大小不变**（`mmap(2)`："partial page 的修改不写回文件"） |
| mmap `MAP_SHARED`，存储落在**下一页**（含页对齐的 EOF） | **SIGBUS（exit -7）**、文件不增长 |

也就是："能让文件变大的字节"这条路全被天花板盖住；mmap 能做的只是**改已有区域内**的字节，既不改变
文件大小、也不占字节账。唯一要标注的是 `copy_file_range` 那次停在 62.2 MiB：那是"worker 的样本 +
中介的增量"两个估计之间的保守差（与 §22.5.10 那个 48 MiB 同一来源、方向相反），**少给而不是多给**。

**更严的一半：天花板恰好为 0（树正好压在预算上）。** 上面每条都是在"还剩 ~124 MiB"时测的；这一轮把
沙箱填到 1024 MiB 整（`cur=0`、`hard=1073741824` —— 顺带直接印证了 §22.5.10 的分工：硬限仍是实例上限，
动的是软限），并且**所有目标文件都在填满之前就建好**，这样"建条目被拒"就不会替别的路径背锅：

| 尝试（已存在的文件上） | 结果 | 文件大小 |
|---|---|---|
| 新建文件（`O_CREAT`） | **ENOSPC** | — |
| `write` 300 MiB | **EFBIG** | 4096 → 4096 |
| `ftruncate` 300 MiB | **EFBIG** | 4096 → 4096 |
| `fallocate` 300 MiB | **EFBIG** | 4096 → 4096 |
| `copy_file_range` 1 MiB × N | **返回 4096**（被钳到"当前大小"），不报错 | 4096 → 4096 |
| `sendfile` 300 MiB | **EFBIG**（4096 之后） | 4096 → 4096 |
| mmap `MAP_SHARED` 越 EOF（页对齐 EOF → 下一页） | **SIGBUS** | 4096 → 4096 |
| mmap `MAP_SHARED` 文件内 | store ok | 4096 → 4096 |
| 平台记账 before → after | **1024.0 MiB → 1024.0 MiB** | — |

两个注意点：① `copy_file_range` 在 `cur=0` 时不报错而是**每次返回 4096**（它只允许改已有字节、不允许
增长）——我第一版探针把返回值累加成了"搬了 300 MiB"，那是探针的 bug，不是绕过；② 沙箱里 `df` 报的是
**10 PB / 已用 557 G / 1%**（整个 NAS 文件系统），所以"看 statfs 判断还有没有空间"在沙箱内毫无意义——
这正是这条闸门必须建立在**我们自己的台账**上的原因。

复现：`tmp/k0s/probe_write_paths.py`、`tmp/k0s/probe_mmap_growth.py`、`tmp/k0s/probe_kernel_copy.py`、
`tmp/k0s/probe_ceiling_completeness.py`、`tmp/k0s/probe_copy_range_zero.py`。

**剩下三种形状也补了（同一形状：900/1024 已用、天花板 ~124 MiB、目标 300 MiB）：**

| 形状 | 结果 |
|---|---|
| `O_DIRECT`（对齐缓冲 + `pwrite`） | **EFBIG**，停在 **116.0 MiB**（≤ 天花板） |
| `io_uring`（`IORING_OP_WRITEV`） | **`io_uring_setup` → EPERM**：sandlock 自己的 `DEFAULT_BLOCKLIST_SYSCALLS`（`crates/sandlock-core/src/sys/structs.rs`）就禁了 `io_uring_setup/enter/register`，注释写明理由——"io_uring bypasses seccomp for I/O operations" ⇒ **这条路在这个部署里不存在**。把 syscall 放开后本地实测（dev 容器 `seccomp=unconfined`）：写得进去，且限额 1 MiB 时**第二块 1 MiB 立刻 EFBIG** ⇒ 万一谁把黑名单删了，天花板仍然管得住 |
| socket → pipe → file（`splice`，字节来自 socket） | **EFBIG**，停在 **124.0 MiB**（正好天花板），平台记账 1024.0 MiB |

三个探针坑，记下来免得下次重踩：① `O_DIRECT` 短写是常态，判停条件不能写成"第一次短写"（否则会把 84.9 MiB 误读成被拦）；② socket→pipe 的 `splice` 每次只搬 socket 缓冲区那点（~64 KiB），迭代次数上限会先于天花板触顶（我第一版用 `4×目标MiB` 次迭代，把 70 MiB 误读成被拦）；③ `files.write("/home/user/x")` 会落到 `/home/user/home/user/x`——SDK 的绝对路径按**树根**解析（N28 记录过的未修语义），要传相对路径。

**"换存储要不要重验"这件事本身也验了（2026-09-20）。** 之前这条写的是"这是**这台 NFS**上的事实、换存储要重新验证"——量过之后那句话**不准确**：同一份探针（`tmp/k0s/mmap-probe.py`，越 EOF 三页内/4 MiB 外、末页内、文件内对照各一次）在两个内核、六种存储/协议上**结果逐字一致**：

| 存储 | 内核 | 越 EOF 存储 | 末页内越 EOF |
|---|---|---|---|
| XFS（节点本地 nvme，`.94`） | 6.12.0-211 aarch64 | SIGBUS，不增长 | store ok，**不落盘** |
| tmpfs（`/dev/shm`） | 同上 | SIGBUS，不增长 | store ok，不落盘 |
| **ext4 on loop**（256 MiB 镜像，N30 那条路线） | 同上 | SIGBUS，不增长 | store ok，不落盘 |
| NFS **4.0**（沙箱真正用的那个 PV） | 同上 | SIGBUS，不增长 | store ok，不落盘 |
| NFS **3**（同一条 NAS，`.140:/mnt/sandlock`） | 同上 | SIGBUS，不增长 | store ok，不落盘 |
| XFS（本地，`.140`） | 同上 | SIGBUS，不增长 | store ok，不落盘 |
| overlayfs（Docker VM） | **7.0.14-orbstack x86_64** | SIGBUS，不增长 | store ok，不落盘 |

所以这是**内核的通用行为**（`filemap_fault` 里 `offset >= max_idx` → SIGBUS，末页那一段按 `mmap(2)` 的"partial page 不写回"处理），不是 NFS、也不是这台 NAS 的怪癖；顺带把 N30 的镜像路线也验了：**ext4-on-loop 上同样长不了文件**，换过去不会凭空多一个 mmap 洞。

**但仍然要按存储/内核重验的不是 mmap，而是这五项**（都是这次侦察里冒出来的）：

1. **NFS 协议版本**——现在挂的是 **vers=4.0**（`.94`/`.140` 的 PV 都是；`/mnt/sandlock` 那条是 v3）。服务端拷贝（NFS `COPY`）要 **4.2** 才有 ⇒ `copy_file_range` 现在**不可能**被服务端代理，所以它老老实实受 `RLIMIT_FSIZE`（这正是上面 EFBIG 的由来）。换到 4.2 或别的产品，**这一条必须重验**：服务端拷贝是唯一能绕过进程侧限额的形态。
2. `statx(STATX_SIZE)` 是否仍然避免回写（§22.4 的 1405 ms → 0.01 ms 完全依赖它）。
3. 稀疏/打洞支持（这台 NAS `fallocate -p` 不支持 ⇒ N30 的"峰值口径"问题）。
4. 目录配额 / `FileCountLimit` 是否存在（N31 第三条修法）。
5. 属性缓存与 `.nfsXXXX` 行为（§22.5.9 那条修法依赖"最后一个持有者放手后 NFS 才回收"）。

**验不了的**：另一个**内核版本**（生产目标是 ACK 5.10，我们手上只有 6.12 与 7.0.14），以及另一个 **NAS 产品**——分别需要一台 5.10 的机器（或 ACK 集群）和目标产品的 export。复现手段：`tmp/k0s/node-mmap-storage.sh` + `tmp/k0s/mmap-probe.py`，经 `tmp/k0s/tools.sh node-run <host>` 打到节点上（节点是 root，可 `losetup`/`mkfs.ext4`）。

### 22.4 冻结延迟的 3.7 秒花在哪：NFS 的 `stat` 会先等自己的回写（N25，2026-09-19）

**现象**：写 3×900 MiB（预算 1024 MiB），从"开始写"到"控制面记录变成 paused"是 **3.7~4.0 s**，
而把扫描间隔从 1 s 调到 0.25 s 或拉长到 3 s，结果**一模一样**。间隔不是杠杆，这本身就是线索：
延迟里有一段与间隔无关的时间。

**先排除的两段**（实测，不是推断）：上报链路只占 **20 ms**（worker 日志 `crossing 09:22:59.837`
→ 控制面 `…843` → agent 冻结 `…857`）；控制面到 worker 的 `stat` 可见性也没问题（慢速写时
worker 侧跟着线性涨）。所以这段时间在**扫描这一轮本身**：worker 自己的 trace 记着
`path=rebuilt total=1.800s apply=1.799s`、`path=ledger total=1.006s apply=1.006s`。

**找它的过程**（每一步都用一个反例把上一步的假设打掉）：

| 假设 | 怎么测的 | 结果 |
|---|---|---|
| 树太大/walk 本来就这么贵 | 同一个 NFS 路径，从**另一台** worker 上走同一棵树 | **4.6 ms**（树是 2 目录 3 文件）⇒ 不是 NAS 慢 |
| CPU 被限流或抢不到 | 线程内同时记 wall 与 `thread_time()`，并读 cgroup `cpu.stat` | wall **1722 ms** / CPU **2.2 ms**，`nr_throttled=0`、`throttled_usec=0` ⇒ 是**阻塞**，不是 CPU |
| 写入节点的所有元数据操作都慢 | 先 `find` 出沙箱真正落在宿主上的路径，再逐个计时 | `scandir(父目录)` **2.3 ms**、`stat(父目录)` **0.03 ms**，只有 **`stat(正在写的那个文件)` 1389 ms**（另一台 worker 同一次测量 **0.03 ms**） |
| 是 RPC 排队/协议层 | 看阻塞线程的内核状态 | 线程状态 `D`、`wchan=rpc_wait_bit_killable` 与 `folio_wait_bit_common`、当前 syscall = `newfstatat`(79) ⇒ 卡在**回写等待**上 |

内核源码把这条钉死（`fs/nfs/inode.c::nfs_getattr`）：

```c
/* Flush out writes to the server in order to update c/mtime/version.  */
if ((request_mask & (STATX_CTIME | STATX_MTIME | STATX_CHANGE_COOKIE)) &&
    S_ISREG(inode->i_mode)) {
        if (nfs_have_delegated_mtime(inode))
                filemap_fdatawrite(inode->i_mapping);
        else
                filemap_write_and_wait(inode->i_mapping);
}
```

也就是说：**在 NFS 上 `stat()` 一个"自己还有脏页"的文件，会先把脏页全部写出去再回答**。而
`os.stat`（`fstatat`）的请求掩码里**永远带着 ctime/mtime**，所以记账每问一次"这个文件多大"，
就等于顺手要求"顺便把它写完" —— 在写入节点上，这个等待正好等于那一轮写的时长。

**改法**：只在 **`statx` 里请求 `STATX_SIZE`**（不含时间字段），内核就不会安排那次 flush，而
size 缓存过期时它照样会去 revalidate。集群上同一时刻、同一个文件的对照测量：

| 问法 | 写入节点 | 另一台 |
|---|---|---|
| `os.stat()` | **1405 ms** | 0.04 ms |
| `statx(mask=STATX_SIZE)` | **0.01 ms** | 0.01 ms |
| `statx(AT_STATX_DONT_SYNC)` | 0.06 ms | 0.03 ms |

三者返回的**同一个数**（838860800）。Python 的 `os.stat` 没有 flags 参数，所以这个探针用
ctypes 直接调 libc 的 `statx`（`struct statx.stx_size` 在 256 字节记录的第 40 字节，且该结构
按设计是架构无关的；`os.stat` 保留为没有 `statx` 的平台的回退）。实现落在
`envd_service/runtime/brief_stat.py::entry_size`，被**两条**记账 walk 使用：
`dir_ledger.scan_subtree`（增量账本）与 `priv_helpers.dir_size`（整树回退，也是 `/metrics` 那个
数）。两条用同一个探针，所以"账本 == 整树 walk"的逐字节契约不变。

**为什么不是别的改法**：

* 调间隔没用：一轮里有一段与间隔无关的阻塞，间隔再短也得等它（本轮实测 0.25/1/3 s 三档一致，
  就是这个原因）。
* 事件上报（跨阈值立即报）只省掉"轮询发现"的那一段，它**仍然要先有一个数**，而那个数当时
  正卡在上面这 1.4 s 里。
* 换掉整树 walk 也不够：增量账本已经只重扫脏目录了，脏目录里的**那个文件**照样要问大小。

**上线实测**（`0.1.0-400-g37e8da3-20260919-103017`，`tmp/k0s/probe_brief_stat_live.py` /
`probe_freeze_latency_cp.py` 可复跑）：

| 项 | 改前 | 改后 |
|---|---|---|
| 写入节点 `stat` 正在写的文件 | 1395 ms（p50） | **0.08 ms**（`entry_size`） |
| 该节点整树 `dir_size` walk | 1.4 s 量级 | **4.9 ms**（p50） |
| worker 一轮（trace `round took`） | **1.0 / 1.6 / 1.8 s** | **0.010 / 0.011 s** |
| 冻结延迟（3×900 MiB 对 1024 预算） | 3.7 / 3.8 / 3.9 / 4.0 s | **1.9 / 3.1 / 3.2 / 3.2 / 3.4 / 3.4 / 3.7 s** |
| 三处返回值 | — | `os.stat` / `entry_size` / `dir_size` **逐字节相同** |

**剩下的 3 秒不是这条链路**。它现在分解成：一轮 10 ms + 上报 20 ms + **"沙箱写下去的字节什么时候对平台可见"**。
后者是存储侧的可见性，不是扫描频率：对**快速写**，不带 flush 的 `stat` 看到的是**已提交**状态，
所以观测序列是台阶（`0 → 900 MiB → 1800 MiB`，偶尔能抓到中间值 `1151 MiB`，那一次延迟就是 **1.9 s**）；
带 flush 的 `stat` 能拿到真实大小，但代价是等到这次写结束 —— 也就是改前那 1.4 s 在做的事。
两侧都一样：**沙箱自己的** `os.stat` 同样会 flush、同样被卡住（实测：探针从 0.01 s 的"不存在"直接跳到
0.85 s 的 900 MiB）。

**这条不放松实施口径**（同一次上线回归）：C 的"每次 exec 现算剩余"是在 **exec 边界**上取数的，
而那正好是 NFS `close` 把脏页刷完的时刻，所以那里的数字不受上面这段可见性影响 ——
`probe_exec_limit.py` 复跑结果与改前**完全一致**（树 700/1024 时单个 1024 MiB 文件落地 324.0 MiB；
连续三次 900 MiB 各为 324 / 1 / 1 MiB，树收敛 1026 MiB）。**受影响的只有"一条命令内部还在写、
从不 close"的长期写入**，那格本来就是设计上交给暂停闸门兜的。

> 所以再往下压延迟，要压的不是"扫描多快"，而是"**数从哪来**"：只有当数字来自"沙箱自己写了多少字节"
> （中介侧记账 / 事件上报）时，才不依赖存储的提交可见性。这件事与 §22.2 的脏目录记账是两条不同的
> 线：脏目录回答"哪个目录变了"，字节账回答"变了多少"，而后者正是现在唯一还没被消掉的那一段。

### 22.3 单文件硬限改成"剩余额度"（N25/C，2026-09-19）

C 原来的口径是"任何单个文件不得超过**整棵树**的预算"（免费、永不误伤，但也管不住"树已经用了 700、
再写一个 900 MiB 的文件"）。这一条把它改成**剩余**，并在**每次 exec 时现算**：

* fork：`ExecParams.max_file_size` —— per-exec **收紧紧额**（只能比实例天花板小；越界或 0 按
  `PolicyTooWide` 拒），在 `sandlock-init` **fork 出来的子进程里、execve 之前**应用
  （限额是进程级的，放 init 身上会把后续每个 exec 一起限死；降自己的限额不需要任何 capability），
  同时把 `SIGXFSZ` 置忽略 ⇒ 越界写返回 **EFBIG** 而不是杀命令
* worker：每个 exec 前**现算**剩余 = `diskMB − 账本当前值`，取 `max(剩余, 下限 1 MiB)`，并对每个挂载卷
  额度只**放宽**（卷有自己的配额且用量不在这个账本里）。下限的意义是"越了预算的沙箱仍然能跑命令
  —— 尤其是**能删文件**把自己救回来"
* **"现算"是这条能成立的关键**：L2c 让一次刷新降到毫秒级（整树 walk 是 1044 ms/400 目录），
  否则用 5 s 前的旧值，连续写会各自读到"还剩 1024 MiB"
* `E2B_DISK_EXEC_LIMIT=1`、`E2B_DISK_EXEC_LIMIT_FLOOR_MB=1`（代码默认为关）

**集群验收**（`tmp/k0s/probe_exec_limit.py`）：

```
树已用 700 MiB（预算 1024）
1. 单个 1024 MiB 文件              → 落地 324.0 MiB   （= 剩余，而非整预算）
2. 连续三次 900 MiB，各自独立 exec  → 324.0 / 1.0 / 1.0 MiB
   三次之后树 = 1026 MiB（预算 + 两个下限 MiB）⇒ 收敛在预算上
   （做之前：同样三次共 2701 MiB）
```

**它买不到的**：**一条命令里的循环**（`for i in 1 2 3; do dd …; done`）是一个进程、一个限额，
里面每个文件都只受同一个数约束 ⇒ 仍可能超；那一格由 5 s 的暂停闸门兜（§22.1），
要更硬就得上"周期 `prlimit` 下压运行中进程的软限"（记账一涨立刻收紧，尚未做）。

### 22.2 记账改成增量（N25/L2c，2026-09-19）

上面 §21.1 那一版每轮走**整棵树**。这台 NAS 上实测（`e2b-worker-0` 里直接跑）：

```
400 目录 / 2000 文件的一棵树：
  整树 walk（被替换的做法）:  1043.9 ms
  只重扫 1 个被报脏的目录:       2.34 ms      ⇒ 446x
```

于是把"哪些目录变了"交给**中介**：它本来就解析每一次写的路径（它就是执行策略的那个组件），
所以能说出"这些目录自上次问你之后被写过"。worker 只重扫这些目录。

| 层 | 落点 |
|---|---|
| fork | `crates/sandlock-core/src/dirty.rs`：写意图路径的**父目录**集合，上限 4096，超了置 `overflow`（上层改走整树 walk）；打点在 `handle_chroot_open` 的写意图分支与 `handle_chroot_write` 里 —— **不是** handler 链上的新 builtin（chroot 的写 handler 以 `ReturnValue` 短路，链后面的 handler 根本看不到） |
| 导出 | in-process 走 FFI `sandlock_instance_drain_dirty_dirs`；route-B（生产形态）走 slot 的 `dirty_dirs` verb。两者都是**新增**符号/动词，调用方能力探测 |
| worker | `DirLedger`：每目录 own bytes + 总量，只替换被报脏的子树；`E2B_DISK_ENFORCE_DIRTY`（默认关，清单里开） |

**两条边界（缺一不可，第二条是第一轮实测抓出来的）**：

* **grace**（`E2B_DISK_DIRTY_GRACE_S=120`）：被写过的目录在这段时间内持续复检。中介只在**路径**
  syscall 上打点，所以"开一个文件、过一会儿再追加"（日志）在 `open` 之后不再产生任何路径 syscall
  —— 实测：写 1000 B → 睡 12 s（超过 5 s 扫描间隔）→ 再写 5000 B，平台只报 7144/12144。
  加上 grace 后同一条用例逐字节相等。
* **reconcile**（`E2B_DISK_RECONCILE_INTERVAL_S=900`）：无论增量路径看起来多健康，账本每 15 分钟
  从一次**真实整树 walk** 重建。中介**结构上**看不见的东西（描述符开着超过 grace、别的信任域写的）
  只有这条兜得住 —— 换成增量之前，"永远错"不是可能，是没有东西会去发现。

**验收**：`tmp/k0s/probe_dir_ledger.py` —— 在**沙箱内**独立量出树大小（`os.walk`+`getsize`，与
`priv_helpers.dir_size` 同口径），与平台上报的 `diskUsed` **逐字节相等**：变异序列
（多层新文件 / 新目录 / rename / 整枝删除）**6144 = 6144**，追加写场景 **12144 = 12144**。
worker 同时打印每轮用了哪条路，避免"功能其实是空转"看不出来：
`disk accounting: ledger=13 rebuilt=0 walk=0`（连续 13 轮全部来自账本）。

**为什么这条是 ③ 的前提**：`max_file_size` 要取"剩余额度"，剩余就必须够新；整树 walk 的陈旧度是
`间隔 × ⌈树数 ÷ 每轮扫到的树数⌉`，增量之后第一项（5 s）才占主导。

### 22.1 集群验收（2026-09-19，`0.1.0-388-ge76d38e-20260919-010638`）

`tmp/k0s/probe_n28_acceptance.py`（可复跑，逐条打印证据）全绿，实测输出要点：

| 项 | 实测 |
|---|---|
| B：上传的身份 = 沙箱自己命令的身份 | `uploaded=10000:10000`、`by_command=10000:10000`（平台建的条目是 `10000:65534`，gid 即"谁创建的"） |
| B：写不排在用户命令后面 | 一个 `sleep 25` 占着命令闸门时，写 **0.14 s** 完成 |
| A：上传被拒 | `SandboxException: 409: Sandbox is paused; its files can only be modified while it is running (resume it first)` |
| A：新命令被拒 | `Code.FAILED_PRECONDITION: …`（SDK 对未映射 code 的渲染，前缀是它的） |
| A：读不受影响 | `files.read` / `files.list` 照常；`connect` 后同一个写成功 |
| C：单文件越界 | `dd … count=1200` → `rc=1`、`dd: error writing '…': File too large`、文件停在 **1024.0 MiB**、**沙箱还活着**（`echo alive` OK） |
| D：实测值进 API | `diskUsed` 从 0 → **169 B**（首次扫描）→ 超限后 **1200.0 MiB** |
| D：暂停原因 | 拒绝文案带 `its workspace grew past its budget (1200 MiB used of 1024 MiB)`；`GET /sandboxes/{id}/logs` 有同一行 |

另外 `deploy/scripts/deployment_smoke.py` 与 `multinode_smoke.py` 均通过（含命令、文件、迁移、
网络、卷、模板构建、MCP 网关六条路径 —— 也就是**写路径改走沙箱之后**的全链路）。
