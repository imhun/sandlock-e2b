# k0s（自建集群）部署层

这是 `deploy/k8s/` 基线清单在**自建 k0s 集群**上的 overlay。基线与 compose 的关系见
[`docs/k8s-deployment.md`](../../docs/k8s-deployment.md)；本文件只讲怎么在这套集群上跑。

```bash
KUBECONFIG=... deploy/k8s-k0s/apply.sh          # 版本取自 deploy/stack/.version
VERSION=1.2.3 KUBECONFIG=... deploy/k8s-k0s/apply.sh
DRY_RUN=1 KUBECONFIG=... deploy/k8s-k0s/apply.sh   # 只渲染
```

## overlay 改了什么，以及为什么

| 文件 | 改动 | 为什么 |
|---|---|---|
| `seccomp-root.patch.yaml` | 安装器的三处路径 → `/var/lib/k0s/kubelet/seccomp` | kubelet 把 Localhost profile 解析到 `<--root-dir>/seccomp`；k0s 的 `--root-dir` 是 `/var/lib/k0s/kubelet`，kubeadm/托管集群才是 `/var/lib/kubelet`。只改一处会让 worker 起不来 |
| `storage-nas.yaml` | 静态 NFS PV（阿里云 NAS，**NFSv4.0**） | 基线只声明 RWX PVC、不自带 PV。用内置 `nfs` 卷插件即可，不需要阿里云 CSI 插件（这台 ECS 没挂 RAM 角色）。**必须 v4**：v3 上 flock 要么 ESTALE，要么只是本地锁 |
| `worker-root.patch.yaml` | worker `runAsUser: 0` + `runAsGroup: 65534` | 网络文件系统按 AUTH_SYS 凭据授权，CAP_CHOWN 不过网 —— 非 root worker 的 file-capability broker 无法把沙箱树让给池 uid。保留 fsgid 65534 是因为沙箱树是 `0770 group=<worker gid>` |
| `worker-capacity.patch.yaml` | `E2B_NODE_*` → 4096/400/8192/1024；pod requests 500m/512Mi、limits 4/4Gi | 基线默认 2048/200 只放得下 1 个沙箱（README 的 F8 就是这条）；且基线 `limits` 无 `requests` 会被当成 requests=2 CPU，滚动更新无处安放 |

另外基线自己已经按「节点本地」分开了两类镜像缓存（`deploy/k8s/worker.yaml`）：

* `E2B_IMAGE_CACHE_DIR=/var/lib/e2b-images` —— **解出来的 rootfs**，`hostPath` 节点本地。解到共享卷上
  要 61.4 秒、解到本地盘 0.26 秒（同一份 python-slim rootfs，2111 个文件，2026-09-17 实测）。
* `E2B_IMAGE_OCI_DIR=/var/lib/e2b-sandboxes/_images` —— **OCI layout tar**，仍在共享卷上，因为那是
  控制面 `Template.build` 导出的、每个节点都要读的东西。

两者的区别就是 N18 那 240 倍；拆开之后 `deployment_smoke.py` 从「分钟级挂在模板阶段」变成 **20.6 秒全绿**。

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
6. **冒烟要打到 gateway**：临时建一个 NodePort Service（不要改基线清单），再从本机
   经跳板机转发过去；`E2B_API_URL` / `E2B_SANDBOX_URL` 指到本机端口。

## 清单侧的三个配套（不是 k0s 特有，但都是真集群跑出来的）

* `control-plane` **只能 1 副本**：节点注册表是进程内的，而心跳被 Service 轮询到某一个
  副本 ⇒ 另一个副本 15 秒后把健康的节点判成 unhealthy（实测两副本意见相反）。
* 清单里原本**没有 buildkit**，`Template.build` 无从执行 ⇒ 按 compose 的形态补成
  control-plane 的 **sidecar**（unix socket 要同 pod 才能共享 emptyDir）；镜像
  `moby/buildkit:rootless` 在 Docker Hub ⇒ 已镜像到 ACR 的 `byteplan/buildkit:rootless`。
* worker 以 **root** 跑（网络文件系统的 chown 需要 euid 0），因此会打
  `E2B_PER_SANDBOX_UID … without CAP_SYS_PTRACE` 的告警；非 route-B 路径的模板沙箱
  可能因此受影响（见 backlog N18）。

## 一组验证脚本（跑在真集群上）

三个都要 `E2B_API_URL` / `E2B_SANDBOX_URL` 指到本机转发的 gateway、`E2B_API_KEY`
与 `E2B_INTERNAL_API_KEY` 来自 `e2b-secrets`；多副本那个还需要 `KUBECONFIG`（它要重启
worker、读 pod 日志）。

| 脚本 | 验什么 | 参考耗时 |
|---|---|---|
| `deploy/scripts/multinode_smoke.py` | 4 个沙箱 2+2 跨节点，命令 + 文件 + 健康 + stdin | ~8 s |
| `deploy/scripts/deployment_smoke.py` | 跨 worker 迁移保留文件、网络配置、远端卷隔离、模板构建→worker 拉取→镜像 rootfs、箱内 MCP 经代理 | ~46 s（模板冷启动时更长） |
| `deploy/scripts/multiworker_interference.py` | **N13**：两副本共用一份 base 不互相破坏（重启一个 worker 后断言树都在、`deleted=0`、幸存者的沙箱照常读写） | ~3.5 min |

跑 N13 那个之前**先把 autoscaler 停掉**（`kubectl -n sandlock scale deploy/autoscaler
--replicas=0`），它会按需求缩容/扩容，与"重启一个 worker 再看结果"互相干扰。

## 已知未完成项

* ~~跨节点 pod 网络不通~~ —— ✅ 2026-09-17 已解（Calico VXLAN，见上一节与
  `docs/k8s-deployment.md` §11）。
* **worker 的 node id 是 pod 名**（downward API `metadata.name`），每次重建都是一个新
  节点。两条后果：① 死掉节点的预留不会被回收，fleet 视图会累积僵尸节点；② **重启后它
  承载的沙箱记录仍指向旧 id，控制面路由不到**，要等 TTL（登记为 backlog **N20**）。
  要稳定 id 需要 StatefulSet（autoscaler 现在按 Deployment scale，改起来牵连较大）。
  ⚠ 别拿 worker 上报的本地运行时列表当归属声明 —— 它含共享 base 上**所有**树。
* **共享 base 上做 reconcile 扫描时文件 API 会变慢**（24 s 空闲 / 43 s 负载下），
  与 N18(a) 同族：重活在事件循环上（backlog **N21**）。
* 控制面**仍只能 1 副本**（注册表在进程内，见上一节）。
