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
kubectl -n $NS rollout status deploy/e2b-worker

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
**2026-09-17 的最近一次发布**：`0.1.0-330-g235fc34-20260917-142808`（含 `auto` 在无 Landlock
内核上 fail-closed 的修复，已在 main 集群用该 tag 复验通过）。升级时：

```bash
kubectl -n $NS set image deploy/e2b-worker worker=<REGISTRY>/byteplan/e2b-sandlock-worker:<VERSION>
kubectl -n $NS set image deploy/control-plane control-plane=<REGISTRY>/byteplan/e2b-sandlock-control-plane-gateway:<VERSION>
kubectl -n $NS set image deploy/autoscaler autoscaler=<REGISTRY>/byteplan/e2b-sandlock-autoscaler:<VERSION>
kubectl -n $NS set image ds/seccomp-installer installer=<REGISTRY>/byteplan/e2b-sandlock-worker:<VERSION>
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
| 沙箱身份 | 每沙箱独立 host uid（两 worker 用不重叠段 10000/11000） | 每沙箱独立 host uid（段默认相同，但**共用一个 base ⇒ 共用一个分配器**：`uid_pool.acquire` 先 flock `<base>/.uid_pool.lock`，再按全部 `sandbox.json` + 预约标记重算空闲集 ⇒ 副本之间**不会**发同一个 uid） | 见 §8 的 N13 更正：分配器本身是跨进程安全的；**未验证的是同一 base 上多副本各自的 reconcile/GC 权限**，因此 autoscaler 上限先锁 1 |
| route B（槽位） | 每沙箱一个 `sandlock-supervise --uid <槽位>` | 同（`E2B_PRIV_HELPERS=auto`，非 root pod 用镜像里的 file-cap broker） | 一致；broker 依赖上面那 4 个 cap 在**bounding set** 里 |
| 配额 | stack 内 quota-agent（`E2B_QUOTA_AGENT_URL`），XFS prjquota 已开 | **无 agent 清单 → 降级**（无 per-sandbox 磁盘硬限，一条 WARNING） | 口径写在 §2.4.4；要真配额就把 agent 指到集群内/外（k8s 共享卷是 RWX/NFS，本地直连不可能） |
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
kubectl -n sandlock logs deploy/e2b-worker | grep -E "seccomp self-check|route-B instance ready"
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

**D. 多副本（autoscaler 提到 >1）之前必须做的**：解决 §3 表里那条 uid 池重叠（N13），
并确认 PDB `minAvailable: 1` 与 `terminationGracePeriodSeconds: 120` 对滚动更新的行为符合预期。

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
| N13 | **多副本形态整体未验证**。~~uid 池重叠~~ 已更正：两个 pod 共用同一 base 时**共用同一个分配器**（`uid_pool.acquire` flock + 按全部 `sandbox.json`/预约标记重算），不会发同一个 uid。真正的未知是**同一 base 上各副本的 reconcile/GC 权限** —— 一个 worker 会不会动到另一个 worker 的活树 | 已先**把上限锁住**：worker `replicas: 1` + autoscaler `E2B_AS_MAX_REPLICAS=1`，并有清单用例钉住；打开前需在真实集群验证（per-pod 子卷 + 段划分一起做） |
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
3. **镜像**：直接用本次发布 `0.1.0-330-g235fc34-20260917-142808`（含 `auto` 在无 Landlock 内核上
   fail-closed 的修复，已在 ACK 用该 tag 复验）。

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

顺带修进基线的两条（不在上表）：① `worker.yaml` 的 `replicas: 1` 现在是
`strategy.maxSurge: 0`——默认滚动更新会先起第二个 pod，那正是 N13 说「未验证」的
双副本共享 base 形态，而且 4 核节点上根本排不下；② 明文占位密钥（`local-key` /
`internal-key` / 无口令 redis）改成 `e2b-secrets` Secret + `--requirepass`，缺 Secret 时
pod 停在 `CreateContainerConfigError` 而不是静默用一个公共 key。

### 10.4 验证状态

| 项 | 状态 | 证据 |
|---|---|---|
| §6 A 起没起来 | ✅ | worker 自检 `seccomp self-check: filter mode active, user namespaces allowed`；pod 内 `/proc/1/status` `Seccomp: 2`、`Seccomp_filters: 1`；安装器在 `.94` 写入 `/var/lib/k0s/kubelet/seccomp/sandlock-worker.json (13147 bytes)`；PVC `Bound`（50Gi RWX，静态绑定 NAS PV）；seccomp DaemonSet 2/2 |
| §6 C 形态证据 | ✅（单节点） | 箱内 `id -u`=0；`socket.if_nameindex()` 只有 `lo`（N5 per-sandbox netns）；`kill(1,0)`=ok（N10 per-sandbox pid ns）；宿主侧落盘 `drwxrwx--- 10000:nogroup`（owner=池 uid 10000、group=worker gid 65534、0770，即 c1 模型） |
| §6 B 应用冒烟 | ⛔ **被 F3 挡** | `deployment_smoke.py` 断言 `len(nodes) >= 2`、`multinode_smoke.py` 要 4 个沙箱跨两节点分布；在跨节点网络修好之前，把 worker 钉在一个节点上时这两条断言必然失败（单/多节点建箱、执行命令、kill 都已单独跑通） |
| 多副本 N13 | ⛔ 未开始 | 前置同上：先修 F3，再撤 `.140` 上的临时 taint、把 worker 拉到 2 副本 |

### 10.5 下一步

1. **修 F3**（二选一）：
   - **A（原生路由，云侧要动三处）**：安全组加 `10.244.0.0/16` 放行；关掉两块 ENI 的
     「源/目的地址检查」；**并很可能**再给 pod 网段加 VPC 自定义路由（该 VPC 代理 ARP、
     按 IP 转发，路由器查不到 `10.244.x`）。
   - **B（不动云配置，已实测可行）**：k0s 换 `network.provider: calico` +
     `calico.mode: vxlan`（或 ipip）。VXLAN 与 IP-in-IP 在这两个节点之间都通
     （手工隧道实测 0% 丢包），且节点间只出现安全组已放行的 `172.18.x`。
     代价：封装开销、MTU 降、Pod 网段重建。
2. 撤掉临时 taint，把 `e2b-worker` 拉到 2 副本，按 §6 复跑 A/B/C 并开始 **N13**。
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
| **F14** | **失败的 `Template.build` 会留下可解析的残骸**：`_templates/<tpl_id>/template.json` + 一个 **0 字节**的 `_images/_oci/e2b-local_<tpl_id>.oci.tar`；之后再构建**同名**模板时名字解析可能取到这条 | 症状是 worker 解 rootfs 报 envd 的 `Code.INTERNAL: file could not be opened successfully: … empty file`。实测：`_images/_oci/` 里同时存在 `tpl_2b89f5…`（0 字节，失败那次）与 `tpl_82d03f…`（49 MB，成功那次），而 `smoke-template` 这个名字在两者上都注册着。**未修**：记录在案，验证时用 `tmp/k0s/reset-smoke-template.sh` 清残骸。正确修法二选一：失败的构建不落可解析记录；或名字解析绑定「最近一次成功构建」 |

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
