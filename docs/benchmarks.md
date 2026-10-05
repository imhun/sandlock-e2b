# 性能实测与优化记录

> 2026-10-05 从 README「轻量化到什么程度（实测）」小节沉淀出来 —— README 只留关键读数，
> 逐段拆解、优化史与复跑口径都在这里。相关部署记录见
> [deploy-clusters.md](deploy-clusters.md)（历次上线读数），能力要求见
> [production-deployment-requirements.md](production-deployment-requirements.md) §2.4。

下面这组读数大多测于 2026-10-01 的出厂集群（2 节点 arm64）—— 一条命令就能复跑，
每个数字都附了口径。**① 开头的"当前读数"是 2026-10-05 在线上 `0.1.0-1018` 复测的，① 的修复后数字取自
`0.1.0-864`，②③ 取自 `0.1.0-824`**；建箱延迟用
[deploy/scripts/acceptance/lightweight_metrics_probe.py](../deploy/scripts/acceptance/lightweight_metrics_probe.py)
（走 SDK 的完整路径），要把"平台耗时"和"客户端到入口的网络"分开量则用
[create_latency_probe.py](../deploy/scripts/acceptance/create_latency_probe.py)。

## ① 镜像预缓存后，建箱很快

- **当前读数（2026-10-05 复测，线上 `0.1.0-1018-gcf144a5-20261005-172044`）：
  建箱 p50 = 75 / 71 ms**（客户端边界，本机经入口 `http://172.18.78.49:3000`，两轮各 n=10，
  p95 241 / 144、mean 107 / 88，预热两发 156 / 167 ms）；**平台侧（控制面 pod 内回环）
  p50 = 41 ms**（n=10，p95 53、mean 44，预热首发 539 ms）。客户端→入口那段 ≈ 30–34 ms。
  计划里"建箱 127 → ~60 ms"的**平台侧目标达成**；与 2026-10-02 首测
  （**70 / 71 ms 客户端、40 ms 平台**，[deploy-clusters.md](deploy-clusters.md) §7.33.1）
  同一水位，中间 §7.34–§7.42 六次发版没有把它打回去。
  两条口径别混着说：**41 ms 不是用户看到的值，75 ms 也不代表平台没达标**；客户端边界
  p95 波动大（241 ms）来自客户端→入口那段网络，与平台无关。
  下面的 0.66 → 0.13 s 是这条优化链子**到 2026-10-01 为止**的历史读数。
  复跑：`deploy/scripts/acceptance/create_latency_probe.py`（两条口径的调用方式见文末「复跑」）。
- **建箱 p50 = 0.66 s、p95 = 0.73 s**（n=10，**N54 修复前**；从开发机经入口实测，含 SDK 请求 →
  控制面调度 → worker 建工作目录 → 销毁。每轮建完立刻 kill，不占容量。修复后见下面最后一条）。
- 镜像没进缓存时也不贵：一次解包（2111 个文件的 python-slim rootfs）在**节点本地 0.26 s**
  —— 这也是方案刻意做的事：OCI tar 放共享卷、rootfs 解到节点本地缓存。冷节点用预热端点
  跑一次 **18.8 s**，之后一直命中。
- 旁证：4 个沙箱跨 2 节点的端到端冒烟（建箱 + 命令 + 文件 + stdin）整轮 **8.7 s**；
  加上模板构建 → registry → worker 拉取 → 解 rootfs → MCP 的完整冒烟 **20.6 s**。
- **这 0.66 s 花在哪**（2026-10-01 逐段实测 + 当天的修复）：客户端到入口的网络 **~40 ms**、控制面认证 +
  记录查询（幂等建箱对照）**2 ms**、控制面→worker 一跳 **7 ms**、worker 建树 + 把树交给沙箱 uid +
  写记录 **~120 ms**，而**约 0.45–0.55 s 原本是去镜像仓库解析基础镜像** —— 建箱时每个 worker 各自
  发约 6 次 HTTPS（`dockerauth…/auth` + `registry…/v2/…/manifests/…`）。**已修（N54）**：解析结果
  改为**落盘缓存**在 `<image cache>/.digests/`（键含 image/scheme/username/credential_host，
  TTL = `E2B_IMAGE_MANIFEST_TTL_S`），预热或任意一次建箱解析过之后，同一节点上的 `peek`/`resolve`
  都读盘不再问 registry；缓存过 TTL 仍会重解析（tag 挪动能自我失效）。
  **修后集群实测（四轮）：建箱 p50 0.66 s → 0.23 s → 0.19 s → 0.19 s → 0.13 s** —— 第一跳是上面那段
  镜像解析改成落盘缓存（N54）；第二跳是再删掉三处"白付的往返"：控制面分配 uid 时的记录读、
  file-op 的那条新 TCP 连接、控制面派发时每次新建的连接（2026-10-01，`0.1.0-841`）；
  **第三跳是把建箱的树/拷贝/改属主改由控制面直接指挥该节点 agent 做一次**（载体 C，
  `0.1.0-864`，N56），worker 那一段因此从 173 ms 掉到 76–79 ms，**但那一跳自己就要 70.8 ms**
  （同一条连接上的空指令只要 1.8 ms，所以这是 agent 在 NAS 上递归 `lchown` 的活，不是网络），
  省下的与付出的相抵 ⇒ 端到端仍是 0.19 s。**第四跳把这两条腿从串行改成并发**（`0.1.0-877`，
  N56 收尾）：建箱的 agent 材料化与 worker 的 `prepare` 同时开始，回来后补一条 `finalize` 收口 ——
  worker 那 76–79 ms 里**不需要树**的那一半（uid 认领、盘上记账种子、建箱标记）不再排在材料化后面，
  **建箱 p50 0.19 s → 0.127–0.130 s**，建箱路径上的 `fileop:*` 归零。口径：在控制面 pod 内发起，n=10；同一时刻从本机
  经入口量到 p50 **0.154 s**（`0.1.0-877` 实测）—— 那约 25 ms 就是"客户端到入口"那一段
  （`0.1.0-864` 时代同口径是 **0.33 s**，减去当时的平台时间 0.19 s 也是约 0.1 s）。`0.1.0-841` 的逐段拆解是
  **改属主往返 71 ms、运行时记录落盘 47 ms、其余文件工作 ~55 ms、预热运行时上下文 17 ms**
  （记录那 47 ms 已在 `0.1.0-864` 里移出响应路径）。**天花板换了位置**：plain 建箱的关键路径
  现在是 `max(材料化 ≈80 ms, prepare ≈73 ms) + finalize ≈8 ms + 尾部`；而**带快照的建箱是拷贝主导**
  —— 快照拷贝约 **25 ms/条目**（可复跑探针实测：1 个文件 p50 **215 ms**、40 个文件 p50 **1247 ms**；
  Task B 记的 202 条目是 **5.2 s**），**两跳并发动不了它**（省下的 ~78 ms 在 5.2 s 上是 1.5%），
  要动它得动拷贝本身。再往下抠 plain 建箱则要动"不靠 ownership 表达权限"（组位/ACL/落盘即目标 uid），
  两条见 [deploy-clusters.md](deploy-clusters.md) §7.27 / §7.28。`GET /agent/images/<ref>/warm` 热态
  **0.9–1.0 ms**（把 `<image cache>/.digests/` 删掉再跑，第一发 **0.65 s**、之后回到 0.23 s —— 也就是说重新解析
  manifest 本身约 **0.41 s**，与上面 0.45–0.55 s 那段同量级）。

## ② 有命令在跑的沙箱 ≈16 MiB，没跑过命令的空沙箱 0

先分清四种形态（**2026-10-05 在线上 `0.1.0-1018` 实测**；在 **c3-agent pod（`hostPID`，
看得到宿主 pid ns）**里按沙箱池 uid 汇总 `/proc/*/VmRSS`——沙箱进程跑在**宿主** pid ns，
worker 容器里只有 `envd_service`，扫 worker 是量不到的）：

| 形态 | 读数 |
|---|---|
| 刚建、**不跑命令**的空沙箱 | **池 uid 进程 0 个 = 0 MiB**（建箱后 0.6 s–8.5 s 连扫 8 次，全程 0） |
| 挂一条 `/bin/sleep` 的活动沙箱 | **15.6 MiB**（单箱逐进程）·4 箱同跑 **16 / 16 / 16 / 16，合计 62 MiB** |
| 跑过命令后**闲置 → 暂挂**（§7.36，`state=paused`） | **15.7 MiB**（6 个进程照样在，RSS 不释放） |
| kill 之后 | 0（基线，不留残留） |

- 单箱 15.6 MiB 的构成：**骨架 11.6 MiB**（`sandlock-supervisor` 6.1 + 2.1、
  `sandlock-init` 3.5）+ **命令链 4.3 MiB**（`sh` trap 包装 1.4、`sh -l -c` 1.5、`sleep` 1.3）。
  **观测到的是**：不跑命令的沙箱在宿主上一个进程都没有（0.6 s–8.5 s 连扫 8 次全 0），
  一跑命令 supervisor / init 立刻出现 —— 骨架跟着命令在。这是**观测**，不是对内部机制的断言。
- **10-01 那次的 16 MiB**（`0.1.0-824`，当时从 worker pod 内扫）与今天 **15.6 / 16** 同水位 ——
  **数字没变，变的是该怎么称呼**：这是"**有命令在跑的沙箱自身进程**"，**不是空沙箱的开销**
  （空沙箱是 0）。
- **闲置暂挂不还内存**：`state=paused` 时那 6 个进程照样在（**15.7 MiB**）—— SIGSTOP 只是
  冻结现场，**不释放 RSS**；§7.36 归还的是**准入配额**（账面），物理内存要等
  `E2B_PAUSED_TTL_S=1800` 到期拆除才真正归零。**本表的 0 只属于"从没跑过命令"的沙箱**，
  不能推广到"用过再闲置"。
- 这就是"沙箱是进程、不是 VM"的直接体现：**没有 guest 内核、没有虚拟化进程**——那两样在
  microVM 方案里是每个沙箱都要付的固定内存。上面这个数只含该沙箱自己的进程，
  **不含**共享的镜像页缓存，也**不含** worker / control-plane 这些平台底座（它们不随沙箱数线性涨）。
- 平台按 `memoryMB` 给每个沙箱做**准入预留**（默认 1 GiB，可调）：预留是配额口径，
  不是上面的实测占用 —— 实测占用由用户负载决定（16 MiB 里那 4.3 MiB 就是负载侧）。

> ⚠️ 复跑口径已改：`lightweight_metrics_probe.py` 的内存段原本扫 **worker pod**，沙箱进程
> 搬到宿主 pid ns 之后扫它只会得到 `skipped=no-sandbox-processes`；2026-10-05 已改成扫
> **c3-agent pod**（host 视角是 worker 视角的超集，不会漏也不会重复计）。改完实测
> `METRIC active_sandbox_memory sandboxes=4 min_mib=16 max_mib=16 total_mib=62`。

## ③ 沙箱内命令的执行延迟低

- SDK 一次 `commands.run('/bin/echo ok')` 的**端到端往返**：p50 **102 ms** / p95 **113 ms**
  （n=20，从开发机经公网入口，含 SDK 的请求与流读回）；同一口径在**内网侧**实测
  **p50 ≈ 33 ms**（[production-deployment-requirements.md](production-deployment-requirements.md) §2.4.7）。
- 沙箱内一次 `stat`：**p50 ≈ 26 µs**（2026-10-05 于 `0.1.0-1018` 复测；镜像 rootfs 文件与工作区
  文件同一水位 —— 工作区自 §7.33 本地优先后从 **2.3 ms 掉到 26 µs**，探针里 `workspace_file_nas`
  这个标签是历史遗留）。这是"路径中介 + 内核"的真实成本（记账路径上用只取大小的 `statx`，
  实测 **0.01 ms**）。
- **这个数只认 p50，均值不可用**：`stat_cost` 原先报 2000 次的算术平均，会被 seccomp
  **通知限流**污染 —— 每沙箱 5000 通知/s（`E2B_SANDBOX_NOTIFY_RATE_LIMIT` 默认值，
  2026-09-01 上线；`third_party/sandlock` 的 `seccomp/notif.rs`：超限就**睡满这一秒剩余时间**，
  被拦调用在内核队列排队）。一个 2000 次的紧循环约 54 ms 就烧穿预算，随即睡
  **852–864 ms**（与 `5000 × 26 µs ≈ 130 ms → 余 870 ms` 吻合）。于是 run 级均值随"这一秒
  有没有被点名"在 **26.9 → 338 µs（12.6×）** 之间跳，而同一沙箱 8 轮的 p50 全在
  **26.1–27.1 µs**；卡顿期间对照组 `getpid` 只有 **3–61 µs**、宿主 CPU 也没涨 ⇒ 不是进程被
  抢占，是限流睡。所以 **2026-10-01 记的"30 µs"与 10-05 中途出现的 159 µs 是同一分布的两次
  抽样，不是每次调用变慢**；探针已改成同时报 `p50_us/p95_us/p99_us/max_us`。
  **"放行 stat / stat 不计入限流"已立项 [open-issues N79](open-issues.md)**（两档：stat 单列预算
  属低风险；彻底放行的前置是 `/proc` 的 stat 语义，沙箱自己挂 procfs 当前实测 EPERM）。
- 隔离本身几乎不加钱：开 per-sandbox 网络命名空间后**建连 p50 0.291 ms**（未隔离 0.034 ms，
  只影响短连接）；在已部署的中介形态里再加 PID 命名空间，实测增量 **≤2 µs/次**。
  pid_ns 打开后 fork 要拦 stat 族（`newfstatat`/`statx`/`faccessat`/`readlinkat`…），
  部署形态（模板 rootfs + chroot 中介）里这些调用本来就已经过 supervisor；
  只有"没有任何路径中介"的裸形态才看得见真实单价（**+80~90 µs/次**）。
  口径与实测表：[production-deployment-requirements.md §2.4.10](production-deployment-requirements.md)。

## 复跑

```bash
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"      # 本机 kubectl 默认指向另一套 ACK，必须带
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
CP=$(kubectl -n sandlock get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')

# ① 建箱延迟 · 平台侧 —— 探针是纯标准库的，直接灌进控制面 pod，只剩回环
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    python3 - --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 \
    < deploy/scripts/acceptance/create_latency_probe.py
# ② 建箱延迟 · 客户端边界 —— 平台 + 客户端到入口那段网络（proxy 要摘掉）
env -u http_proxy -u https_proxy -u all_proxy \
    python3 deploy/scripts/acceptance/create_latency_probe.py \
    --base http://172.18.78.49:3000 --key "$E2B_API_KEY" --n 10

# ③ 内存 / 命令往返 / 镜像解包
python deploy/scripts/acceptance/lightweight_metrics_probe.py   # 内存那段需要 kubectl，非 k8s 可加 --no-memory
```

本次读数与探针输出：`tmp/k0s/lightweight-metrics.log`；其它口径出处见
[production-deployment-requirements.md](production-deployment-requirements.md) §2.4.6 / §2.4.10
与 [k8s-deployment.md](k8s-deployment.md) §12 / §22。

> 适用范围：Linux（内核 6.12+，即 Landlock ABI ≥ 6）；暂停与快照是进程 / 文件系统级语义，
> 不保留运行内存。完整清单见 [README §已知边界](../README.md#7-已知边界)。

## 相关：本地优先布局的读数（2026-10-02）

线上从 `0.1.0-915-gfb8a74b-20261002-211709` 起跑"本地优先"布局：沙箱树在节点本地盘
（小文件写入 13.0 ms → **0.196–0.223 ms/个**，58–66×）、快照是一个 `fs.tar`
（2000 文件从快照建箱 ~52 s → **532 ms**）、**建箱 p50 = 70 ms（客户端边界）/ 40 ms（平台侧）**
（2026-10-02 首测；2026-10-05 在 `0.1.0-1018` 复测为 **71–75 ms / 41 ms**，同一水位）。
一个版本、一张验收表、两条硬约束与回退路见 [deploy-clusters.md](deploy-clusters.md) §7.33。
