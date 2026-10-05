# namespace `sandlock` 的 ingress 默认拒绝（2026-10-05 上线并当场验证）

`deploy/k8s/default-deny.yaml`：一条 `podSelector: {}` 的 ingress 全拒，加三条具名例外。

## 只做 ingress，而且这是测量结果不是偏好

egress 这一半在本 namespace 是**合法地宽**的，两条都不是疏忽：

* **worker 承载沙箱的公网出口**。`fd_inject_connect` 在 worker 的 netns 里替沙箱建连
  （`attack-surface.md`「网络策略的正确读法」段：netns 不是这层的防线），所以沙箱出站
  就是 worker pod 自己的流量。给它一条"只许 CP + DNS"的 egress = **所有沙箱同时断网**。
* **control-plane 的 buildkit sidecar 要拉公网镜像**（模板构建）。

两条都得给"全部放行"，于是 egress 那半收不到任何东西，却要多背一条说不清的路：k0s 的
kube-apiserver 是**宿主进程、没有 pod**（实测 `kube-system` 里没有 apiserver pod），CP 经
`10.96.0.1:443` 到达它；这条能不能用 NetworkPolicy 表达，取决于 Calico 对 egress 策略按
DNAT 前还是后判地址。收益为零、风险是"下一次 apply 断整个平台"，所以不做。agent 的 egress
本来就已经收窄（只许 CP:3000 + 集群 DNS），在 `c3-agent.yaml` 里。

## 三条例外，每条都写清谁进哪个端口

| 策略 | pod | 谁可以进 | 端口 |
|---|---|---|---|
| `sandlock-default-deny` | 全部 | **没人** | — |
| `e2b-control-plane` | `app=control-plane` | **任意来源** | 3000、49983 |
| `e2b-redis` | `app=redis` | `app=control-plane` | 6379 |
| `e2b-worker` | `app=e2b-worker` | `app=control-plane` | 49983 |

（agent 那两条 `:49985`/`:49986` 在 `c3-agent.yaml` 里，本来就是 CP-only。）

**`e2b-control-plane` 不写 `from`，这是必须的**：它是对外入口（外部客户端经 NodePort/SLB
进来），而 **kubelet 的 `httpGet` 探针从节点 IP 进来** —— 实测线上两个副本都有
`/healthz`:3000 的 liveness/readiness。两者都不在任何 pod selector 里，所以这一条只能是
"任意来源"。c3-agent 的探针特意走 `exec` 就是为了躲这个坑，CP 躲不掉。

NetworkPolicy 是**并集**语义：这份 deny 与那些具名策略叠加，不会互相覆盖。

## 上线当场的验证（2026-10-05，集群实测）

apply 之后 15 秒，9 个 pod 全部 `Running` 且 `2/2`、`1/1` 就绪（探针没被挡）。

**应当放行的，全部通：**

| 路径 | 结果 |
|---|---|
| CP → redis:6379 | `OK` |
| CP → worker:49983 | `OK` |
| CP → apiserver（10.96.0.1:443） | `OK` |
| agent → CP:3000 | `OK` |
| worker → CP:3000 | `OK` |
| agent 巡检上报 | 日志里 `POST /internal/nodes/<node>/agent/inventory 200 OK` |
| CP 调 apiserver | 日志里 `GET https://10.96.0.1/api/v1/... 200 OK` |
| CP 收 worker 心跳 | 日志里 `POST /internal/nodes/e2b-worker-0/heartbeat 204` |
| 外部/kubelet 进 CP | 日志里 `172.18.80.140:4720 - "HEAD / HTTP/1.0" 200 OK` |

**应当被拒的，全部被拒**（`TimeoutError`，不是"看起来没报错"）：

| 路径 | 结果 |
|---|---|
| worker → redis:6379 | `BLOCKED TimeoutError` |
| agent → redis:6379 | `BLOCKED TimeoutError` |
| agent → worker:49983 | `BLOCKED TimeoutError` |

最后跑了一次端到端：`deployment_smoke.py` = **`DEPLOYMENT SMOKE OK`**（命令与文件过网关、
跨节点迁移保文件、网络配置、远端卷挂载与隔离、**模板构建 → registry 推送 → worker 拉取 →
image rootfs**、沙箱内 MCP 网关与流式 HTTP、kill 后预留归零）。最后那两项覆盖的正是
CP 的出站（buildkit）与 CP→worker 的数据面 —— 也就是这份策略最容易误伤的路径。

## 加新 workload 时要知道的

新 pod 的 ingress 默认是**全拒**。要么给它写一条具名策略，要么它就只能被已有例外覆盖。
这也是这份策略的意义：以前"改一个策略就能打通 agent"，现在多了一层要先被说服。
