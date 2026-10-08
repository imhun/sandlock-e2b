# k0s（自建集群）部署层

这是 `deploy/k8s/` 基线清单在**自建 k0s 集群**上的 overlay。基线与 compose 的关系见
[`docs/k8s-deployment.md`](../../docs/k8s-deployment.md)；本文件只讲怎么在这套集群上跑。

```bash
KUBECONFIG=... deploy/k8s-k0s/apply.sh          # 版本取自 deploy/stack/.version
VERSION=1.2.3 KUBECONFIG=... deploy/k8s-k0s/apply.sh
DRY_RUN=1 KUBECONFIG=... deploy/k8s-k0s/apply.sh   # 只渲染
SKIP_WARM=1 KUBECONFIG=... deploy/k8s-k0s/apply.sh # 不预热 base image
```

`apply.sh` 在 `kubectl apply` 之后会等 `rollout status`，再对每个 worker pod 预热 base image
（`deploy/scripts/warm_base_image.py`，经 `kubectl exec -i … python3 -` 送进容器：agent 监听的是
容器自己的 `0.0.0.0:49983`，宿主机上没有这个端口）。一次滚动重启可以打断正在进行的解包，缓存里只留
`…sha256_….lock`，之后该节点的 `Sandbox.create()` 会回 **428 warm_required**（e2b SDK 不发
`X-Sandbox-Id`、也不认识 428）。预热失败时 `apply.sh` 非零退出并点名还剩几个节点是冷的；
`docs/k8s-deployment.md` §22.5.10 末尾记了这条事实与两处部署脚本的分工。

## 部署顺序（k0s）：密钥先于业务清单

```bash
# 1) 命名空间 + 存储
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/pvc.yaml

# 2) 密钥：脚本幂等 —— Secret 不存在就全新建，存在就只补缺的键（已有的不覆盖）
KUBECONFIG=... deploy/k8s-k0s/secrets.sh

# 3) 其余清单一次 apply（redis / control-plane + buildkit / seccomp 安装器 / worker /
#    autoscaler / NodePort 入口），版本仍由 apply.sh 从 deploy/stack/.version 取
KUBECONFIG=... deploy/k8s-k0s/apply.sh
```

第 2 步取代了 `docs/k8s-deployment.md` §2 那份手工 `kubectl -n $NS create secret generic`
命令（这就是它在 k0s 上的版本）。`secrets.sh` 建/补的是清单里 `secretKeyRef` 读的四个键：
`E2B_API_KEYS`、`E2B_INTERNAL_API_KEY`、`E2B_REDIS_PASSWORD`、`E2B_SECRET_MASTER_KEY`
（第五个 `E2B_INTERNAL_API_KEYS` 是 internal key 的双窗列表，只在点名 `--rotate-internal-key` /
`--finalize-internal-key-rotation` 时写 —— 窗口之外它不在 Secret 里）。
它**只打印 `sha256(前16)` 指纹与长度**，不打印明文，也不开 `set -x`；除这四个键之外的键
（例如主 key 轮换窗口用的 `E2B_SECRET_MASTER_KEYS`）原样带过去，不会被 apply 抹掉。

`E2B_SECRET_MASTER_KEY` 是 secret-at-rest 加密的开关：没有它 `SecretRegistry` 退回
"内存 + 明文落盘"（只打一条启动告警），而 `_secrets/**` 就在共享 NAS 卷上、worker pod
整卷 RW 挂 —— 任何拿到 pod root 的人都能读。`control-plane` 那两个新 `secretKeyRef` 是
`optional: true`（刻意的过渡态：先 apply 清单还是先跑脚本都不会让 CP 起不来），所以键补上
之后要 `kubectl -n sandlock rollout restart deploy/control-plane` 才真正读到。

⚠ 开这个键**只影响之后的写入**：已经在卷上落盘的明文记录要跑一次
`deploy/scripts/cleanup-plaintext-secrets.py`（经 registry 重写为密文 + 清理残留明文副本 +
校验；无主 key 时拒绝执行）。操作步骤见 `docs/k8s-deployment.md` §4.5.1。

**C3 Task 2（N49）起不需要新的 Secret 键**：`control-plane` 改为用同名 ServiceAccount +
一条**只读 pods**（`get`）的 namespaced Role/RoleBinding，按 `node_id`（= StatefulSet pod 名）
查 pod 得到每个节点的**期望地址**（`E2B_NODE_ADDRESS_MODE=k8s`，都写在基线
`deploy/k8s/control-plane.yaml` 里，随 `apply.sh` 一起 apply）。没有这个权限时解析失败
⇒ node-scoped 内部请求**一律 503 点名**（fail closed），不会退回请求自陈的地址。
registry 表在这件事上不变。

## 凭据轮换（k0s）

`secrets.sh` 是唯一允许改值的入口：不点名 `--rotate <KEY>` 时，已有的键**一律不动**。

| 轮换的键 | 影响面 | 不可逆窗口 / 备注 |
|---|---|---|
| `E2B_REDIS_PASSWORD` | **10–30 s 中断**：redis 带着新口令重启、到 **control-plane** 滚动完拿到新口令之间，共享后端（配额/节点视图/限流/单飞，以及 autoscaler 的 tick 单飞与冷却标记）不可用 ⇒ 建箱与路由失败。沙箱本身不经过 redis，不受影响 | 2026-09-26 裁定**接受**这段中断，不做 ACL 双用户热轮换（`docs/superpowers/plans/2026-09-26-decisions.md` 第 5 条）。redis 是 `appendonly yes` ⇒ 数据不丢。顺序：`secrets.sh --rotate E2B_REDIS_PASSWORD` → `rollout restart deploy/redis` → `rollout restart deploy/control-plane`（**读 redis 的只有 control-plane** —— 它同时托管 autoscaler，这一步把扩缩容循环一并重起） |
| `E2B_API_KEYS` / `E2B_INTERNAL_API_KEY` | **双窗轮换**：新 key 与旧 key 并存 → 滚动 → finalize 摘旧 key，中间不断服。唯一掉东西的一步是 internal key 的 worker 滚动 = **杀光全部 running 沙箱**（树与卷数据保留） | `secrets.sh --rotate-api-keys` / `--rotate-internal-key`，完事用 `--finalize-api-key-rotation` / `--finalize-internal-key-rotation <旧 key 或它的 sha256 前 16 位>` 收口；两张表的 runbook 见 `docs/k8s-deployment.md` §4.5。⚠ `--rotate E2B_API_KEYS` / `--rotate E2B_INTERNAL_API_KEY` 仍是**单槽换值**（旧 key 立刻失效），要窗口别用它 |
| `E2B_SECRET_MASTER_KEY` | 脚本**拒绝**就地轮换：旧 key 必须先留在 `E2B_SECRET_MASTER_KEYS`，否则既有 `_secrets/**` 与 redis `e2b:secret:*` 的密文永远解不开 | 两窗三拍（rotate → 滚 CP → finalize）由 `deploy/k8s-k0s/rotate-secret-master.sh` 承担；"全副本已滚动"的三条判据与 runbook 见 `docs/k8s-deployment.md` §4.6 |

只读核对（不改任何东西，也不回显明文）：

```bash
KUBECONFIG=... deploy/k8s-k0s/secrets.sh --fingerprint   # 每个键的 sha256(前16) + 长度
```

## overlay 改了什么，以及为什么

| 文件 | 改动 | 为什么 |
|---|---|---|
| `seccomp-root.patch.yaml` | 安装器的三处路径 → `/var/lib/k0s/kubelet/seccomp` | kubelet 把 Localhost profile 解析到 `<--root-dir>/seccomp`；k0s 的 `--root-dir` 是 `/var/lib/k0s/kubelet`，kubeadm/托管集群才是 `/var/lib/kubelet`。只改一处会让 worker 起不来 |
| `storage-nas.yaml` | 静态 NFS PV（阿里云 NAS，**NFSv4.0**） | 基线只声明 RWX PVC、不自带 PV。用内置 `nfs` 卷插件即可，不需要阿里云 CSI 插件（这台 ECS 没挂 RAM 角色）。**必须 v4**：v3 上 flock 要么 ESTALE，要么只是本地锁 |
| ~~`worker-root.patch.yaml`~~ —— **已删除**（2026-09-27，C1 wave 2） | 曾经给 worker 加 `runAsUser: 0` + `runAsGroup: 65534` | 这张表里**不再有这一行**：worker 回到镜像自带的 `USER 65534:65534`，而网络文件系统那句理由（`CAP_CHOWN` 不过网）没有消失 —— 它成了**基线** `e2b-priv-broker` DaemonSet 的职责（`deploy/k8s/priv-broker.yaml`：每节点一个 root 容器 + unix socket，`chown`/`rm`/`walk` 由它代做）。broker **不是**这个 overlay 的差异 —— 它与发行版/存储类型都无关、跟基线那个共享 RWX PVC 一起走，所以写在 `deploy/k8s/kustomization.yaml` 里；放在 overlay 会让"不经 overlay 的非 root 部署"起不来 |
| `worker-capacity.patch.yaml` | `E2B_NODE_*` → 4096/400/8192/1024；pod requests 500m/512Mi、limits 4/4Gi | 基线默认 2048/200 只放得下 1 个沙箱（README 的 F8 就是这条）；且基线 `limits` 无 `requests` 会被当成 requests=2 CPU，滚动更新无处安放 |

另外基线自己已经按「节点本地」分开了两类镜像缓存（`deploy/k8s/worker.yaml`）：

C3 的 `e2b-c3-agent` DaemonSet 与 broker 同一处境：它**不在**这张 overlay 表里，
因为 overlay 不需要为它改任何东西 —— 它的 PVC claim 就是基线那个 `sandbox-shared`，节点本地
缓存的 hostPath 也是基线已有的 `/var/lib/e2b-images`（`deploy/k8s/c3-agent.yaml`）。

**Task 4 片 B 后**，这个 DaemonSet 的两个容器都有载荷（面 A 是身份授予、面 B 是文件操作），
它们用**两个端口**（49985/49986，D22 —— 两个进程的 uid 必须不同，共享 pod netns 下同端口会
`EADDRINUSE`），NetworkPolicy 也从一条端口列表扩成两条。同一片还让** worker 显式 pin
`runAsUser`/`runAsGroup: 65534`（CP 的可信身份来源读的就是 pod spec）。**C3 Task 7** 之后它是这台
节点上**唯一**的特权组件：C1 的 `e2b-priv-broker` 退役，它的两个属主 init 也搬进了这个 pod
（`storage-init` + `workspace-root-init`，都 `runAsUser: 0`）。

* `E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images` —— **解出来的 rootfs**，`hostPath` 节点本地。解到共享卷上
  要 61.4 秒、解到本地盘 0.26 秒（同一份 python-slim rootfs，2111 个文件，2026-09-17 实测）。
* `E2B_IMAGE_OCI_DIR=/var/lib/e2b-sandboxes/_images` —— **OCI layout tar**，仍在共享卷上，因为那是
  控制面 `Template.build` 导出的、每个节点都要读的东西。

两者的区别就是 N18 那 240 倍；拆开之后 `deployment_smoke.py` 从「分钟级挂在模板阶段」变成 **20.6 秒全绿**。

## 平台态属主迁移（C1 wave 2，**上线前置**）

worker 换成 uid 65534 之后，**今天卷上那些 root worker 写下的平台态**（`0600`/`0700`）它读不了 ⇒
第一次上线这个形态之前必须先跑一次属主迁移。它和 N27 的 `migrate-state-base.sh`（Job
`state-base-migrate`）是并列的两个一次性迁移，工具是 `deploy/scripts/migrate-state-owner.sh`
加 `deploy/k8s-k0s/state-owner-migrate.yaml`（一次性 Job，`runAsUser: 0` +
`runAsGroup: 65534`，`backoffLimit: 0`）：

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"

# 1) 停写：worker 缩到 0，并确认没有 worker pod 还在跑（脚本自己也会验一遍）
kubectl -n sandlock scale statefulset/e2b-worker --replicas=0
kubectl -n sandlock wait --for=delete pod -l app=e2b-worker --timeout=300s

# 2) 先只看不写（默认 dry-run）：经控制面 pod 读实物、打印路径计划与 stat
deploy/scripts/migrate-state-owner.sh

# 3) 真迁移：脚本建 configmap + Job，跑完收日志并清理
deploy/scripts/migrate-state-owner.sh --apply

# 4) 起 worker 并复核
kubectl -n sandlock scale statefulset/e2b-worker --replicas=2
```

它做的是**递归 `chown 65534:65534`，只改属主**：不改权限位、不删东西、不拷内容。范围是一张显式
的路径计划（`<export>` 下的 `state`、`workspaces/_migrate`、`workspaces/_snapshots`、
`_images`、`_secrets`、`_snapshots`、`_templates`、`_builds` 八条）—— **树根下恰放行
`workspaces/_migrate` 与 `workspaces/_snapshots` 这两条**（前者是 N27 之后控制面的迁移暂存
——工具曾在 export 根上找 `_migrate`，真机上因此恒 MISSING；后者是 **worker 的快照 payload
根**，`envd_service/agent.py` 硬编码为 `<workspace_base>/_snapshots`，2026-09-27 真机预检
发现它在树根下、属主 `root:0755`，C1 之后 65534 的 worker 写不进去），其余
`<export>/workspaces/**` **绝不进入** —— 那些树属于池 uid，不是 worker 的；计划里任何一条
落在它下面（包括 `workspaces` 本身、它的兄弟、那两条下面的东西、用 `..` 或符号链接绕过去的
拼写）脚本一律拒绝并点名。快照的两个根不同：控制面的记录根是 `<export>/_snapshots`
（`SnapshotRegistry` 建在共享 export 根上），worker 的 payload 根在树根下——两条都在 8 条计划
里。树根下这两条平台命名空间由 broker 的 `workspace-root-init` 在每次启动时保底。硬性质由
`tests/unit/test_state_owner_migrate.py` 逐条钉住。步骤、回退与判据的正文见
`docs/k8s-deployment.md` §24。

## CNI 必须建集群时定：这套集群用 Calico VXLAN

这套 VPC 会丢弃**源或目的不是本实例 IP** 的报文（ENI 的「源/目的地址检查」），而安全组
规则是 `172.16.0.0/12`（不含 pod 网段 `10.244.0.0/16`），所以 kube-router 那种原生路由
CNI 的跨节点流量会被云网络拦掉。节点链路本身没问题（ping/ssh/kubelet 都通）。

因此这套集群用 **Calico VXLAN**（`deploy/k8s-k0s/k0s-calico.yaml`）：

```yaml
spec:
  network:
    provider: calico
    calico: {mode: vxlan, overlay: Always, vxlanVNI: 4096, mtu: 1450,
             ipAutodetectionMethod: kubernetes-internal-ip}
```

要点：

* **CNI 只能建集群时定**。k0s 会拒绝给已有集群换 provider
  （`cannot change CNI provider from kuberouter to calico`），改了就得分重装集群。
* `overlay: Always` 是必须的——这里不是子网问题，是云网络不认识 pod IP。
* `mtu: 1450`（节点 eth0 是 1500，VXLAN 头 50 字节）。
* `ipAutodetectionMethod: kubernetes-internal-ip`：`.140` 上还有 docker0 与 br-*，
  默认的 first-found 可能选错接口。
* calico 的三个镜像都在 `quay.io/k0sproject`（本环境可拉），**不需要**镜像到 ACR。

## 集群怎么起的（一次性的）

节点走跳板机，和 compose 那条线同一拓扑：本机 → 跳板机 → 节点。要点：

1. **k0s 二进制**：`https://github.com/k0sproject/k0s/releases` 的 arm64 资产在本机/节点上都会被限速截断
   （实测跑到 4.8 MB 就停）。用国内加速前缀下载（`https://gh-proxy.com/https://github.com/...`，实测 3 MB/s），
   再经跳板机分发到各节点（`scp` 两跳约 20 秒 / 240 MB）。
2. **前置**：`modprobe overlay br_netfilter` + `net.ipv4.ip_forward=1`、
   `net.bridge.bridge-nf-call-iptables=1`、`net.ipv6.conf.all.forwarding=1`（写进
   `/etc/modules-load.d/k0s.conf` 与 `/etc/sysctl.d/99-k0s.conf`）；无 swap。
3. **控制面**：先把上面的 `k0s-calico.yaml` 放到 `/etc/k0s/k0s.yaml`，再
   `k0s install controller --enable-worker --no-taints --start`。
   ⚠ **不要用 `--single`**：它会切到 kine(SQLite) 并且拒绝任何 worker token
   （`refusing to create token: cannot join into a single node cluster`）。要加节点就
   必须不带 `--single` 重装（默认 etcd 存储）。
4. **加节点**：控制面 `k0s token create --role=worker --expiry=2h` → 传到节点 →
   `k0s install worker --token-file <file> --force --start`。
5. **本机 kubectl**：API 证书 SAN 含 `127.0.0.1`，所以用 SSH 本地转发即可
   （`ssh -L 16443:<node>:6443 <bastion>`，`kubeconfig` 的 server 改成
   `https://127.0.0.1:16443`）。认证走 SSH ControlMaster，口令只输一次。
6. **冒烟要打到 gateway**：用下面那个 NodePort（`gateway-nodeport`，固定 31907）——
   它就在这个 overlay 里，不再是临时对象。

## 从集群外面访问

三个入口，同一套鉴权（`X-API-Key`，即 `e2b-secrets` 里的 `E2B_API_KEYS`）：

| 从哪里 | 用什么 | 备注 |
|---|---|---|
| **本机 / 任何能到它的机器** | `http://172.18.78.49:3000` | **首选**。这是前置转发（→ `.140:31907`），本机可直达，无需跳板机 |
| VPC 内（跳板机、同 VPC 的 CI/ECS） | `http://172.18.80.94:31907`（或 `.140`） | 集群自己的 NodePort，两个节点都服务 |
| 经跳板机的备用路径 | `ssh -L 49983:172.18.80.94:31907 <bastion>` | 只在上面两条都不可用时才需要 |

```bash
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
```

`gateway-nodeport`（overlay 里的那个 Service）就是第二行的来源：**任意节点的 `:31907`** →
control-plane 的 `:3000`，与基线那个 ClusterIP `gateway` 同一个后端，只是换了个入口类型。
端口写死，因为访问它的东西（本机脚本、CI、前置转发）不该跟着 k8s 的随机分配漂。
`tmp/k0s/open-tunnels.sh` 会把 kubectl 那条（16443）建起来，并顺带 `kubectl apply`
这个 Service —— 重建集群后跑一次就够；**gateway 那条转发它不再建**（本机直连 78.49 即可）。

**实测（2026-09-18，本机直连，无转发）**：`/sandboxes` 带 key 200、不带/带错 key 401、
`/internal/*` 仍需内部 key；`multinode_smoke`（4 箱 2+2、命令/文件/stdin）与
`deployment_smoke`（跨节点迁移保留文件、远端卷隔离、模板构建→worker 拉取、箱内 MCP）
**从这个地址直接跑全绿**，整轮 9.6 s / 20.4 s。

⚠ **单点**：前置转发钉在 `.140` 这一个节点上。NodePort 本身在每个节点都服务（CP pod 跑到
`.94` 也照样通），但 `.140` 一旦重启/下线，这条入口就断了 —— 要抗这点就把它改成带健康检查的
双目标（`.94` + `.140`），或把入口交给真正的 LB。

⚠ 两点：**明文 HTTP**（认证靠 `X-API-Key`，即 `e2b-secrets` 里的 `E2B_API_KEYS`），
**不要**把 31907 直接暴露到公网；要 TLS 就在前面加 ingress/证书。`/internal/*` 需要
另一个 key（`E2B_INTERNAL_API_KEY`），所以它虽然同端口可达，但没有内部 key 打不进去。

## 清单侧的三个配套（不是 k0s 特有，但都是真集群跑出来的）

* `control-plane` **只能 1 副本**：节点注册表是进程内的，而心跳被 Service 轮询到某一个
  副本 ⇒ 另一个副本 15 秒后把健康的节点判成 unhealthy（实测两副本意见相反）。
* 清单里原本**没有 buildkit**，`Template.build` 无从执行 ⇒ 按 compose 的形态补成
  control-plane 的 **sidecar**（unix socket 要同 pod 才能共享 emptyDir）；镜像
  `moby/buildkit:rootless` 在 Docker Hub ⇒ 已镜像到 ACR 的 `byteplan/buildkit:rootless`。
* **worker pod 里没有任何 root 容器**（C1 wave 2，2026-09-27；**C3 Task 4 片 B，2026-09-29 再收敛**）：
  worker 容器**显式 pin `runAsUser: 65534` / `runAsGroup: 65534`**（C3 的 CP 从 pod spec 取"这个
  worker 是谁"的可信答案），**只有 `capabilities.drop: [ALL]`、没有任何 `add`**（BND 空集，字面
  成立 —— 收口评审把"省掉整块"改成显式 drop，否则继承的是 runtime 默认 BND；镜像里的
  file-capability 二进制已移出，见判据 2/15），**也没有任何 initContainer**（C3 Task 7 把唯一的那个非 root
  `wait-for-broker` 闸门与它服务的 `socket` 回退一起退役了）。网络文件系统的 chown 确实只有 euid 0
  做得到，但那个 euid 0 现在在 **`e2b-c3-agent` DaemonSet 的面 B**（基线；听 49986）里 —— worker
  通过 `E2B_PRIV_HELPER_TRANSPORT=agent` 把 `chown`/`rm`/`walk` 交给 CP，再由 CP 指令它；
  C1 的 **`e2b-priv-broker`** DaemonSet **已由 C3 Task 7 退役**（连同 `E2B_PRIV_HELPER_SOCKET`、
  worker 的 `wait-for-broker` 闸门与 `apply.sh` 的 broker rollout 闸门）；它原来的两个属主 init
  搬进了 agent pod（`storage-init` + `workspace-root-init`），见
  `docs/deploy-clusters.md` §7.9 与 `docs/production-deployment-requirements.md` §5.4(b)。
  * 历史口径（已作废，留档）：此前 worker 自己 `runAsUser: 0`、`runAsGroup: 65534` 读挂载上的树，
    会打 `E2B_PER_SANDBOX_UID … without CAP_SYS_PTRACE` 的告警，非 own-identity 路径的模板沙箱可能
    因此受影响（见 backlog N18）；own identity 与 per-sandbox host uid 在两种 transport 下都成立。

## 一组验证脚本（跑在真集群上）

三个都要 `E2B_API_URL` / `E2B_SANDBOX_URL` 指到本机转发的 gateway、`E2B_API_KEY`
与 `E2B_INTERNAL_API_KEY` 来自 `e2b-secrets`；多副本那个还需要 `KUBECONFIG`（它要重启
worker、读 pod 日志）。

| 脚本 | 验什么 | 参考耗时 |
|---|---|---|
| `deploy/scripts/multinode_smoke.py` | 4 个沙箱 2+2 跨节点，命令 + 文件 + 健康 + stdin | ~8 s |
| `deploy/scripts/deployment_smoke.py` | 跨 worker 迁移保留文件、网络配置、远端卷隔离、模板构建→worker 拉取→镜像 rootfs、箱内 MCP 经代理 | ~46 s（模板冷启动时更长） |
| `deploy/scripts/multiworker_interference.py` | **N13**：两副本共用一份 base 不互相破坏（重启一个 worker 后断言树都在、`deleted=0`、幸存者的沙箱照常读写） | ~3.5 min |
| `deploy/scripts/heartbeat_gaps.py` | 心跳空档：从控制面访问日志算每个节点的真实间隔，与配置的 `E2B_NODE_HEARTBEAT_TIMEOUT` 对比（空档打到窗口就说明活节点被判成 unhealthy）。定/改那个窗口前先跑它 | < 5 s |

跑 N13 那个之前**先把 autoscaler 停掉**：`kubectl -n sandlock set env deploy/control-plane
E2B_AS_ENABLED=false`（跑完再 `set env ... E2B_AS_ENABLED=true` —— 它自 2026-09-30 起是
**control-plane 里的一个任务**，不再是独立的 `deploy/autoscaler`），因为它会按需求缩容/
扩容，与"重启一个 worker 再看结果"互相干扰。

## 已知未完成项

* ~~跨节点 pod 网络不通~~ —— ✅ 2026-09-17 已解（Calico VXLAN，见上一节与
  `docs/k8s-deployment.md` §11）。
* ~~worker 的 node id 是 pod 名，每次重建都是一个新节点~~ —— ✅ 2026-09-18 已修
  （backlog **N20**）：worker 从 Deployment 换成 **StatefulSet**（`e2b-worker-0/1` 跨重启
  稳定），autoscaler 用 `E2B_AS_K8S_KIND=statefulset` 扩缩、Role 也已放开
  `statefulsets{,/scale}`。删 pod 后同名回来，沙箱记录被 E6.1 的恢复轮次认回
  （实测 126 秒后文件 API 恢复、内容完好、路由未变）。见 `docs/k8s-deployment.md` §15。
  ⚠ 仍然别拿 worker 上报的本地运行时列表当归属声明 —— 它含共享 base 上**所有**树。
* ~~reconcile 轮次压在 worker 的事件循环上~~ —— ✅ 2026-09-18 已修（backlog **N21**）：
  轮次独立成单飞 task、扫描与逐树校验挪到线程，心跳不再等任何一轮。base 灌到 3002 棵树
  （一轮 7.8 s）时心跳仍是 5.0 s，窗口因此从 60 s 收到 30 s（见 `docs/k8s-deployment.md` §14）。
* 控制面**仍只能 1 副本**（注册表在进程内，见上一节）。
