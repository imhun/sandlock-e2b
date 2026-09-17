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
文件时 pod 根本起不来（N4 记录，清单已就位但**尚未在真实集群验证**）。worker 自己还有一条启动
自检：profile 没生效就拒绝服务（`SECCOMP_PROFILE_NOT_APPLIED`），所以"pod 起来了但 profile
没真加载"这条不会静默通过。

### 镜像与升级

清单里的镜像 tag 目前是占位的 `:0.1.0`。发布流程与 compose 同源（`build-and-push.sh` 会把
worker / control-plane-gateway / autoscaler / quota-agent 推到 ACR），升级时**把清单里的 tag
换成当次构建的版本**（compose 侧现在跑的是 `0.1.0-316-g10bedf6-20260916-175558` 这种带
时间戳的 dev tag），再：

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

## 8. 已知缺口（登记在 `docs/task-backlog.md`）

| # | 缺口 | 下一步 |
|---|---|---|
| N4 | seccomp 安装器**未在真实集群验证** | `kubectl apply` → 等 DS 每节点 Ready → 滚 worker；缺文件节点 fail closed |
| ~~N5~~ | ~~k8s 是否切 per-sandbox netns~~ | ✅ 2026-09-17 已切（§5），**待真实集群按 §6 复验** |
| ~~N10~~ | ~~k8s 是否开 pid_ns~~ | ✅ 2026-09-17 已切（§5），同上待复验；节点仍需允许非特权 userns（这依赖本来就存在） |
| N13 | **多副本形态整体未验证**。~~uid 池重叠~~ 已更正：两个 pod 共用同一 base 时**共用同一个分配器**（`uid_pool.acquire` flock + 按全部 `sandbox.json`/预约标记重算），不会发同一个 uid。真正的未知是**同一 base 上各副本的 reconcile/GC 权限** —— 一个 worker 会不会动到另一个 worker 的活树 | 已先**把上限锁住**：worker `replicas: 1` + autoscaler `E2B_AS_MAX_REPLICAS=1`，并有清单用例钉住；打开前需在真实集群验证（per-pod 子卷 + 段划分一起做） |
| — | k8s 无 quota-agent 清单（口径=降级，§2.4.4） | 有真实 k8s + XFS/NFS 环境时补清单（agent 形态对 NFS 才是唯一可行路径） |
