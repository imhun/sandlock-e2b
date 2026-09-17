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

## 集群怎么起的（一次性的）

节点走跳板机，和 compose 那条线同一拓扑：本机 → 跳板机 → 节点。要点：

1. **k0s 二进制**：`https://github.com/k0sproject/k0s/releases` 的 arm64 资产在本机/节点上都会被限速截断
   （实测跑到 4.8 MB 就停）。用国内加速前缀下载（`https://gh-proxy.com/https://github.com/...`，实测 3 MB/s），
   再经跳板机分发到各节点（`scp` 两跳约 20 秒 / 240 MB）。
2. **前置**：`modprobe overlay br_netfilter` + `net.ipv4.ip_forward=1`、
   `net.bridge.bridge-nf-call-iptables=1`、`net.ipv6.conf.all.forwarding=1`（写进
   `/etc/modules-load.d/k0s.conf` 与 `/etc/sysctl.d/99-k0s.conf`）；无 swap。
3. **控制面**：`k0s install controller --enable-worker --no-taints --start`。
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

## 已知未完成项

* **跨节点 pod 网络不通** —— 阿里云 ECS 的「源/目的地址检查」默认开启，会丢弃目的 IP
  不是该实例的转发包；kube-router 是原生路由型 CNI（不做封装），跨节点包的目的 IP 是
  对方的 `10.244.x`。需要关掉两个实例的源/目的地址检查，或改用封装型 CNI
  （`spec.network.provider: calico` + ipip/vxlan）。**在此之前**：多副本 `worker`
  Deployment 与冒烟脚本（它们都要求 ≥2 个健康 worker）都跑不了；临时把 worker 钉在
  一个节点上可以验证单节点形态。详见 `docs/k8s-deployment.md` §10。
* **worker 的 node id 是 pod 名**（downward API `metadata.name`），每次重建都是一个新
  节点；死掉节点的预留不会被回收，fleet 视图会累积僵尸节点。要稳定 id 需要 StatefulSet
  （autoscaler 现在按 Deployment scale，改起来牵连较大）。
