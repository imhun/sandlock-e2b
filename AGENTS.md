# 本仓库的 Agent 须知

## 部署集群：先认集群，再敲命令

本仓库的部署目标是**自建 k0s 集群**（2 节点，**全 arm64**，`172.18.80.94` / `172.18.80.140`，
namespace `sandlock`）。

**本机 `kubectl` 的默认 context 不是它** —— 不加 `KUBECONFIG` 时 `kubectl get nodes` 会安静地
返回另一套**阿里云 ACK 集群**（7 节点、`v1.34.3-aliyun.1`、`172.18.93.x/94.x`、没有 `sandlock`
namespace）。往那套里敲写操作就是打在别人的生产负载上。

```bash
deploy/scripts/open-cluster-tunnel.sh         # 建通道并自检集群身份（会拒绝错集群）
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"   # 任何 kubectl 都要带
```

连接流程（跳板机 ControlMaster → 本地 16443 转发）、集群里有什么、入口、怎么上节点、
当前部署状态与踩过的坑，全部记在 **[docs/deploy-clusters.md](docs/deploy-clusters.md)**。
动手改部署前先读它第 2 节的自检与第 7 节的现状。
