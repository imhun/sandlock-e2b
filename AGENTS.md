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

## 验收：先在本地车道跑，绿了再发线上

k0s 集群是**线上**，不是验收环境（与 `docs/cross-platform-lanes.md` 纪律 1 同一条精神：
测试不出本机）。任何改动的验收顺序固定为：

1. **本地 lane 先跑**：默认就是**本机的 compose 多节点栈**
   —— `deploy/compose/docker-compose.multinode.yml`（3 个 worker + 控制面 + redis + agent 两面，
   worker 以 65534 跑；**与线上拓扑最接近**，就是这条车道的理由）：
   `docker compose -f deploy/compose/docker-compose.multinode.yml up -d`（本机 override 视需要叠加）。
   纯内核/CLI 形状也可以直接用本机 Docker 跑一次性容器。单测、契约、以及能在本地证明的行为都在这里，
   **RED→GREEN 的读数要留下来**；
2. 本地全绿之后，才构建镜像、滚到 k0s（`deploy/k8s-k0s/apply.sh`）；
3. 线上只做**本地证明不了的那些形状事实**（例如 k8s 的挂载/QoS/命名空间形状），并且**带回退杆**再上。

"本地跑不了，所以上集群试" 不是理由：2026-10-06 实测本机 Docker VM 就是 cgroup v2（root 已委派
全部控制器），cgroup 那类看起来只属于内核/容器的行为在本地 lane 上能完整跑通。
