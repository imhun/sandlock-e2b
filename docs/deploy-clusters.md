# 部署环境：本仓库的目标集群（防止认错集群）

**这份文档解决一个真实发生过的事故**：本机 `kubectl` 的**默认 context 不是本项目的集群**，
`kubectl get nodes` 会安静地返回另一套**阿里云 ACK 集群**。往里敲写操作 = 打在别人的生产负载上。
2026-09-25 记录，所有事实都是当天实测。

## 0. 硬规则

1. **任何 kubectl 都要显式带 kubeconfig**，不要依赖 `~/.kube/config` 的 current-context：

   ```bash
   export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"   # 从仓库根目录执行
   ```

2. **动手前先跑第 2 节的自检**。节点数、架构、K8s 版本、`sandlock` namespace，四个里有一个不对
   就停手 —— 你连的不是本项目的集群。

## 1. 两边对照

| | ✅ 本仓库的目标集群 | ❌ 不是目标（默认 context 指的这套） |
|---|---|---|
| 类型 | 自建 k0s | 阿里云 ACK 托管 |
| K8s 版本 | `v1.36.4+k0s` | `v1.34.3-aliyun.1` |
| 节点数 | **2** | 7（含 2 个 virtual-kubelet） |
| 节点 IP | `172.18.80.94`、`172.18.80.140` | `172.18.93.x` / `172.18.94.x` |
| 架构 | **全是 arm64** | x86_64 与 arm64 混 |
| node 名 | `izuf697v12g31dyz4uvsjlz`、`izuf6d1usviqv6x9qk1hpcz` | `cn-shanghai.172.18.*` |
| namespace | `sandlock`（**有**） | 没有 `sandlock` |
| kubeconfig | `tmp/k0s/kubeconfig`（server `https://127.0.0.1:16443`） | `~/.kube/config` 的 `main` / `saas` |

## 2. 30 秒自检（每次动手前）

```bash
cd <仓库根>
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
kubectl get nodes -o wide
kubectl -n sandlock get pods
```

**对的**：2 个节点、`arm64`、`Rocky Linux 10.2`、`v1.36.4+k0s`、IP 是 `.80.94` / `.80.140`；
`sandlock` 里能看到 `control-plane` / `autoscaler` / `e2b-worker-0,1` / `redis` /
`seccomp-installer`。

**错的**：7 个节点、`v1.34.3-aliyun.1`、IP 是 `172.18.93.x`/`94.x`、`sandlock` 报
`namespaces "sandlock" not found` ⇒ **一行写操作都不要做**，先修 kubeconfig。

## 3. 怎么连（按这个顺序，已实测）

API 只从跳板机可达，所以是「跳板机 ControlMaster + 本地端口转发」两段。**用脚本，别手敲**
（脚本会建连接、开转发、缺 kubeconfig 时自动取一份，最后**断言集群身份**）：

```bash
cd <仓库根>
deploy/scripts/open-cluster-tunnel.sh          # 建通道 + 自检
deploy/scripts/open-cluster-tunnel.sh --check  # 通道已在，只自检
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
```

自检输出形如 `✓ 2 节点 / arm64 / 含 +k0s`；连错集群时它会**非零退出**并逐个点名不符项
（实测拿默认 context 跑 `--check`：`✗ 节点数 7 != 2` + 9 条 `v1.34.3-aliyun.1` 不符 ⇒ 退出码 1）。

**坑（实测）**：`ssh -L` 这一步**必须带 `-i "$SSH_KEY"`**。没有 ControlMaster 时裸 `ssh -L`
只会报 `root@172.18.74.236: Permission denied (publickey)` —— `tmp/k0s/open-tunnels.sh`
（scratch 脚本，不在版本库）当年就是这么坏的：它自己的 `ssh` 不带 `-i`，只在
`/tmp/k0s-bastion-root.sock` 已有 ControlMaster 时才成立。现在的脚本自带 expect 建立这一步，
不依赖 `tmp/` 下任何东西。

参数速查：跳板机 `172.18.74.236`（`root`，密钥 `~/.ssh/id_pub` + 口令，都在
`deploy/scripts/bastion.env`，该文件 gitignore）；控制面节点 `.80.94`。

### kubeconfig 丢了怎么办

`tmp/` 是 gitignored，`tmp/k0s/kubeconfig` **不在版本库里**。重建：

```bash
# 从控制面节点取 admin kubeconfig（5646 字节，实测 rc=0），再把 server 改成
# https://127.0.0.1:16443（API 证书 SAN 含 127.0.0.1，所以本地转发即可）
```

取文件的通道见第 6 节（`run-target.exp`，`TARGET_HOST=172.18.80.94`，命令
`k0s kubeconfig admin`）。

## 4. 集群里有什么（2026-09-25 实测）

节点（都是 arm64 / Rocky Linux 10.2 / kernel `6.12.0-211.34.1.el10_2.aarch64`）：

| 节点名 | IP | 角色 | k0s |
|---|---|---|---|
| `izuf697v12g31dyz4uvsjlz` | `172.18.80.94` | control-plane | `v1.36.4+k0s` |
| `izuf6d1usviqv6x9qk1hpcz` | `172.18.80.140` | worker | `v1.36.4+k0s` |

`sandlock` namespace：`control-plane`（Deployment，2/2 容器 = 控制面 + gateway）、
`autoscaler`（Deployment）、`e2b-worker`（StatefulSet，`e2b-worker-0/1` 各落一个节点）、
`redis`（Deployment）、`seccomp-installer`（DaemonSet，2/2）、
`gateway-nodeport`（NodePort **31907**）、`gateway` / `control-plane` / `redis` /
`worker-headless`（ClusterIP）。**（2026-09-29，C3 Task 7 起）**：基线的 DaemonSet 有两个 ——
`e2b-c3-agent`（每节点一个，两个容器：非 root 的面 A + root 的面 B）与 `seccomp-installer`；
C1 那个 `e2b-priv-broker`（每节点一个 root broker，`chown`/`rm`/`walk` 经 unix socket 代做）
**已随 `socket` 形态一起退役**，现行的特权动作只在 agent 面 B 里（见 §7.9 与
`docs/production-deployment-requirements.md` §5.4(b)）。本节上面的 pod 清单是 2026-09-25 的读数。

镜像 tag 必须等于 `deploy/stack/.version`（`apply.sh` 就是拿它渲染的）。2026-09-25 实测
两边都是 `0.1.0-440-g9b57736-20260922-191343`。

## 5. 入口（三个，同一套 `X-API-Key`）

| 从哪里 | 地址 | 备注 |
|---|---|---|
| 本机 / 任何能到它的机器 | `http://172.18.78.49:3000` | **首选**，前置换到 `.140:31907`，本机直达、不需要跳板机 |
| VPC 内 | `http://172.18.80.94:31907`（或 `.140`） | 集群自己的 NodePort，两节点都服务 |
| 经跳板机 | `ssh -L 49983:172.18.80.94:31907 <bastion>` | 上面两条都不通时的备用 |

```bash
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
```

## 6. 上节点（要读节点的文件、容器看不到的东西时）

节点不能直连，走跳板机两跳，用仓库里的 expect 封装（它负责把命令 base64 过去、按用户执行）：

```bash
cd <仓库根>
set -a; . deploy/scripts/bastion.env; set +a
export TARGET_HOST=172.18.80.94            # 或 .140
cmd=$(base64 < 你的脚本.sh)
expect deploy/scripts/lib/run-target.exp "$cmd" root
```

**坑（实测）**：`ssh -o ControlPath=/tmp/k0s-bastion-root.sock root@172.18.80.94 'hostname'`
**不会**落到节点 —— 复用跳板机连接的结果是回到跳板机自己（hostname 打印
`aliyun-bastionhost`）。要碰节点就用 `run-target.exp`，别用裸 `ssh`。

## 7. 当前部署状态（**最近一次：见 §7.11（2026-09-30，compose 车道的三条缺口收口）**；§7.10 是 C3 收口评审、§7.9 是 C3 Task 7 上线，下面 §7.1–§7.8 是历史记录）

> **本节从 §7.1 到 §7.8 是 2026-09-27 → 09-29 的分批记录，其中多处标着"仓库已落，集群未上线"
> 的段落到 2026-09-29 已经全部上线**（C3 的 Task 2–7 在 09-29 随 Task 7 的镜像一起滚上去了）。
> **动手前先读 §7.9 与 §7.10**：§7.9 是现役的 pod 清单、版本与"没有 `e2b-priv-broker`"的读数，
> §7.10 是收口评审后重取的 worker/agent cap 读数（§7.9 表里 worker 的 `CapBnd` 已被它取代）；
> §7.1–§7.8 保留为上线经过与当时判据。

**版本**：`0.1.0-721-g01e4b72-20260927-231235`（= `deploy/stack/.version`；`apply.sh` 就是按它渲染的；
C1 的三条尾项与「记账项批次」都在这一版）。2026-09-27 **三次上线**实测：`autoscaler` /
`control-plane` / `e2b-worker` / `e2b-priv-broker` 四个工作负载的镜像都是同一版（broker 与 worker
必须同版本滚，见 §7.1）。三次的经过分别见 §7.1（第一次，C1 主体）/ §7.2（第二次，三条尾项）/
§7.3（第三次，记账项批次）。

**pod（2026-09-27 23:1x 实测，C1 三次上线后）**：`control-plane` 两个副本各 `2/2`、
`autoscaler` `1/1`、`e2b-worker-0/1` 各 `1/1`（分别落在 `.80.94` / `.80.140`）、`redis` `1/1`、
`seccomp-installer` `2/2`、**`e2b-priv-broker` `2/2`（一节点一个）**。节点仍是 2 台 arm64 /
`v1.36.4+k0s`。

### 7.1 C1 上线实测（2026-09-27，已执行）

**动作与顺序**（当时 CP 的沙箱列表为 **0 个**，停机窗口无代价）：

1. `build-and-push.sh` → 新版本 `0.1.0-698-g55e5e79-20260927-195247`（多架构，含新 C broker：
   镜像里 `e2b-maint` 的 usage 已含 `serve|ping`，file caps `cap_chown,cap_dac_override=ep` 在位）；
2. `kubectl -n sandlock scale statefulset/e2b-worker --replicas=0`；
3. `migrate-state-owner.sh`（先默认 dry-run 看计划）→ `--apply`：**8 条目标、`missing=0`、
   `chowned=8`**，每条都打印了 STAT/AFTER 且 **files/dirs 计数前后完全一致**（无丢文件）：
   `state` 1996 文件/1036 目录、`workspaces/_snapshots` 2001/4、`_snapshots`（控制面记录根）
   2005/7、`_templates` 53/53、`_builds` 54/108，另有 `_images`（已是 65534）、`_secrets`、
   `workspaces/_migrate`。Job 与 ConfigMap 跑完自清理；
4. `apply.sh`：等 `ds/e2b-priv-broker` 先滚完再滚 worker（脚本内建顺序），随后预热 base image。

**验收证据（全部现场实测）**：

* worker 容器 securityContext = `{"capabilities":{"add":["SETUID","SETGID"]},"seccompProfile":…}`
  —— **没有 `runAsUser`**，即 worker 真的不再是 root；
* `e2b-priv-broker` 每节点 `1/1`；socket 形态 `710 0:65534 /run/e2b-broker` 与
  `660 0:65534 /run/e2b-broker/broker.sock`；以 peer 身份 `ping` 回
  `{"ok":true,"peer_uid":65534,"peer_gid":65534,"uid_pool":[10000,1000],"roots":[workspaces,state,<export>,image-cache]}`
  （四根顺序与 Python 侧逐位一致）；
* broker 的**有效能力集 `CapEff=0xcb`** = 恰好 `CHOWN`+`DAC_OVERRIDE`+`FOWNER`+`SETGID`+`SETUID`
  （`drop: [ALL]` + 这五条 add；不再是运行时默认的满 root 集）—— 这是**第一次上线当时**的读数：
  那版探针还在用 `setpriv` 降权，所以留了 `SETUID`/`SETGID`；§7.3 起收成三条（`0x0b`）；
* `multinode_smoke.py` = `MULTI-NODE SMOKE OK`（4 箱 2+2、命令/文件/stdin、预留归零），
  `deployment_smoke.py` = `DEPLOYMENT SMOKE OK`（含跨节点迁移保文件、远端卷隔离、
  模板构建→registry→worker 拉取→镜像 rootfs、MCP 网关）；**最小能力集下又各跑一遍 multinode = OK**；
* 活沙箱的树 = `770 10000 65534`（`<池 uid>:<worker gid>`，由 broker 经 NFS 交出去），
  `kill` 后树被删除、worker 记账里的 `walk` 计数在走（三个 verb 都验证过）。

**上线时踩到的两件事（都已修/已记）**：

1. **探针必须以对端身份连 socket**。第一版 DaemonSet 的 liveness/readiness 直接以容器的
   root 跑 `e2b-maint ping` → 被 peer 门拒（`refused: peer uid 0 does not match
   E2B_BROKER_PEER_UID=65534`），daemon 本身一直正常服务，但 pod 停在 `Running 0/1`、
   反复重启、rollout 超时。修法：探针先用 `setpriv --reuid/--regid 65534 --clear-groups`
   降到对端身份再 ping（`deploy/k8s/priv-broker.yaml`，pin 当时叫
   `tests/unit/test_worker_manifest_permissions.py::test_the_broker_probes_connect_as_the_peer_identity`，
   §7.3 起已改名 `…::test_the_broker_probes_dial_the_root_only_health_socket`）。
   这也意味着 broker 的 cap 集必须保留 `SETUID`/`SETGID`。**（后续修订，见 §7.3）**：探针不再
   降权 —— 改连**容器私有**的健康 socket，`SETUID`/`SETGID` 已从 cap 集撤掉；**业务 socket 的
   对端门本身没动**（仍然只放 `E2B_BROKER_PEER_UID`/`GID`）。
2. **`deployment_smoke` 第一次跑在模板构建轮询上 404**（`Template build bld_… not found`），
   立刻重跑即全绿。当时归因成"控制面 2 副本 + 构建状态在进程内"，**这个归因是错的**：状态本来
   就在共享卷上、跨副本读取是设计的一部分（`_write_build` 的注释写明 trigger 与 poll 可能落在
   不同副本）。真根因是**记录文件的非原子写**（`Path.write_text` 先 `O_TRUNC` 再分次写，poll
   落在这个窗口里 `json.loads` 抛 `ValueError`，被 `except` 吞掉后变成 404）—— wave 3 的 fix-c
   改用 `write_text_atomically`（同目录临时文件 + fsync + `os.replace`）后一次跑过，**与副本数
   无关**，也不用把控制面缩到 1 副本。根因链条见 `docs/reports/fix-c-report.md` §1。

### 7.2 第二次上线：三条尾项收口（2026-09-27，已执行）

版本 `0.1.0-708-g3f92ba3-20260927-211625`。这次是**真正的升级路径**（集群上已有 broker），
顺带验证了 `apply.sh` 内建的"先 `ds/e2b-priv-broker` 后 `sts/e2b-worker`"闸门：
`build-and-push.sh` → `apply.sh`（日志顺序：`等待 broker DaemonSet 滚动完成` →
`daemon set "e2b-priv-broker" successfully rolled out` → `等待 worker 滚动完成` → 预热）→ 冒烟。

尾项内容：`walk` 有自己的 512 MiB 上限（推导写实）且 `runtime/platform_disk` 从"整棵 `_runtime`
一次 walk"改成逐子项求和（否则单树前提不成立、合法答案会被拒）；`image-cache-init` 对 `secrets/`
的 chown 失败不再静默；`migrate-state-base.sh` 的 4 处 bash 3.2 隐患修掉。

验收：broker/worker 全 `1/1`；broker `CapEff=0xcb`（**该版仍是五条形态**；§7.3 收到三条）；
peer 身份 `ping` 回 `ok:true` 且四根一致；
运行中的镜像里 `BROKER_MAX_WALK_RESPONSE_BYTES == 512 MiB`；`multinode_smoke` 与
`deployment_smoke` **都一次跑过**（`DEPLOYMENT SMOKE OK`）。

**一个小坑**：重建镜像期间隧道会掉（`apply.sh` 报
`dial tcp 127.0.0.1:16443: connect: connection refused`）——重跑
`deploy/scripts/open-cluster-tunnel.sh` 即可，别把它当成集群问题。

### 7.3 第三次上线：记账项批次（2026-09-27，已执行）

版本 `0.1.0-721-g01e4b72-20260927-231235`。这一版把上两轮收尾时**记账**的遗留项做进代码与
清单后重新上线：四支并行 worktree/分支（`feat/c1-fix-{a,b,c,d}`，各由子代理实现并通过评审）
`--no-ff` 并入 main，再由 `apply.sh` 滚上集群。分支报告归档在 `docs/reports/fix-{a,b,c,d}-report.md`（工作笔记原件在同名 `.superpowers/sdd/`）。

内容（每条都"修"而不是"记账"）：

* **fix-a（C 侧 / 清单）**：新增**健康 socket** `--health-socket`（**容器私有**路径
  `/run/e2b-broker-health.sock`、`0660 root:root`、键集严格只答 `{v,hello}`、要求 peer uid 0）；
  liveness/readiness 改为容器 root **直连健康 socket**（不再 `setpriv` 降权；**业务 socket 的
  peer 门逐字未动**）；broker cap 从五条收到 **`drop:[ALL]` + `[CHOWN, DAC_OVERRIDE, FOWNER]`**；
  拒绝路径改为"非阻塞首读，只有沉默对端才退到一次有界 poll"（诊断开关
  `E2B_BROKER_REFUSAL_TRACE` 默认关）；`walk` 有**自己的** 64 MiB 未转义上限、两条流共享额度
  （推导 `6 × 64 MiB = 384 MiB < worker 侧 512 MiB`）。
* **fix-b（Python 侧）**：secret 注入**先全部解析、再统一落地**；`os.open(..., 0o600)` 消除 umask
  窗口；reclaim 前显式检查父目录属主 / 非 sticky；roots 归一**拒绝相对拼写**（fail closed）；
  混合 env/文件条目保持输入顺序。
* **fix-c（原子写）**：新增 `write_text_atomically` / `write_json_atomically`（同目录临时文件 +
  fsync + `os.replace`）并替换 7 处调用点。根因是控制面 2 副本 + 非原子 `write_text` 让
  `deployment_smoke` 的模板构建轮询偶发 404；**更严重的一条**是
  `envd_service/runtime/registry.py` 写 `_runtime/<id>/sandbox.json` 非原子，而
  `uid_pool._recorded_uid` 把"解析失败"当作"没有记录" ⇒ 两个 worker 可能发出**同一个 host uid**
  （E3.2 隔离静默失效）。
* **fix-d（文档 / 注释）**：`auto` 模式遇残留 socket 硬拒启动写进 README 与 k8s-deployment；
  `security-hardening.md` §8.3 标已收口（保留历史）；四处 "registry base = workspace base" 的
  docstring 改对；容器内跑测试必须带 seccomp 档写入文档；直接 exec 断管退出码 141→77 一句。
  调查结论：`<workspaces>/_pure_rootfs` **无缺口**（0755 由 worker 在自己可写的树根下建，只在
  无基镜像的 pure 沙箱落盘），无需改清单。

**验收（全部现场实测，均在 `0.1.0-721` 上）**：

| 判据 | 结果 |
|---|---|
| `apply.sh` | 通过；broker 先滚、worker 后滚、随后预热 base image |
| broker 能力集 | **`CapEff=0x0b`** = 恰好 `CHOWN`+`DAC_OVERRIDE`+`FOWNER`（`SETUID`/`SETGID` 已不在） |
| socket 形态 | `/run/e2b-broker` `710 0:65534`、`broker.sock` `660 0:65534`、`/run/e2b-broker-health.sock` `660 0:0` |
| 两条探针 | 容器 root 直连健康 socket → `ok:true`；worker pod 内 uid 65534 连业务 socket → `ok:true`（`peer_uid/gid=65534`，四根白名单一致） |
| 三个 verb（三条 cap 下） | `chown` 新树 `770 10000:65534`、`rm` kill 后树消失、`walk` worker 记账计数在走 |
| 两条冒烟 | `multinode_smoke` = `MULTI-NODE SMOKE OK`；`deployment_smoke` = **`DEPLOYMENT SMOKE OK`（一次跑过，模板构建不再 404）** |

> §7.1 里"探针必须保留 `SETUID`/`SETGID`"的结论**已被本节取代**：探针不再降权，改连健康 socket。
> 业务 socket 的对端门没有变化，仍然只放 `E2B_BROKER_PEER_UID`/`GID`（worker 的 65534）。

### 7.4 C1 特权外置后的形态（2026-09-27 起；仓库规格 = 集群现状）

* 基线 `deploy/k8s/priv-broker.yaml` 新增 **`e2b-priv-broker` DaemonSet**，每节点一个 **root** 容器
  （`runAsUser: 0` + `capabilities.add: [CHOWN, DAC_OVERRIDE, FOWNER]`）：`chown`/`rm`/`walk`
  由它经 unix socket `/run/e2b-broker/broker.sock` 代做，并接管了原来在 worker pod 里的两个
  属主 init（`image-cache-init` / `workspace-root-init`）。它挂在**基线**里（不是 overlay），
  kustomize 渲染出的 `name: e2b-priv-broker` 在 `deploy/k8s` 与 `deploy/k8s-k0s` 各恰一份。
* **worker pod 里不再有 root**：`e2b-worker` 的 worker 容器没有 `runAsUser`（回落镜像
  `deploy/docker/Dockerfile.envd` 的 `USER 65534:65534`），`capabilities.add` 只剩
  `SETUID`/`SETGID`，唯一的 initContainer 是**非 root** 的 `wait-for-broker`（broker 先监听、
  worker 才放行；等的是 `hello` 往返，不只是 socket 文件存在）。
* **第一次滚这个形态必须先把平台态属主迁过来**（一次迁移，不是滚动）：worker 缩到 0 →
  `deploy/scripts/migrate-state-owner.sh --apply` → 再把 worker 起回来。它只 `chown` **八个**平台目录
  （`state`/`workspaces/_migrate`/`workspaces/_snapshots`/`_images`/`_secrets`/`_snapshots`/
  `_templates`/`_builds`），**树根下恰放行两条**：`workspaces/_migrate`（N27 之后控制面的迁移暂存
  就在树根之下，不是 export 根）与 `workspaces/_snapshots`（**worker 的快照 payload 根**，
  `envd_service/agent.py` 硬编码在 `<workspace_base>/_snapshots`；控制面的快照*记录*在另一个根
  `<export>/_snapshots`，两条都在计划里）；其余 `<export>/workspaces/**`（那是池 uid 的树）
  **绝不碰**。用法见
  `deploy/k8s-k0s/README.md`「平台态属主迁移」、正文见 `docs/k8s-deployment.md` §24。顺序反了
  （先上 worker）会得到读不了 `0600`/`0700` 平台态的 worker —— 也就是每个 `Sandbox.create()`
  都失败。

> 上面「pod（2026-09-27 实测）」是 **C1 三次上线之后**的读数：broker DaemonSet 每节点 `1/1`、
> worker 容器无 `runAsUser`（非 root）。改部署前按 §2 的方式重取。

> **"现在跑的是哪一版"永远以 `deploy/stack/.version` + 集群里 `autoscaler/control-plane/e2b-worker`
> 三个工作负载的实际镜像为准**（两边必须一致），别引用本文任何一节里写死的版本号。本节记的是
> **2026-09-27** 的实测状态；§9/§10/§11 是**历史上线记录**（各自写的是那一版当天的验收），
> §7.1/§7.2/§7.3 是 2026-09-27 三次上线的验收记录（最近一次 = §7.3），§12/§13 是当天早先
> 两次发版的细节记录。

**2026-09-27 实测的形态开关**（`sts/e2b-worker` 的 env；`e2b-worker-0` 运行中容器的 `env` 与之一致）：

| 变量 | 值 | 是什么 |
|---|---|---|
| `E2B_WORKSPACE_BASE` | `/var/lib/e2b-sandboxes/workspaces` | 树根（N27 下沉一级后的位置） |
| `E2B_STATE_BASE` | `/var/lib/e2b-sandboxes/state` | 平台状态（N27：树的同挂载兄弟，沙箱看不到） |
| `E2B_ROUTE_B_TMP_ROOT` | `/var/lib/e2b-sandboxes/state/.route-b` | route-B 槽位临时根（随 N27 挪进 state） |
| `E2B_REAL_ROOT` | `1` | 真根（N35/N14 已上线） |
| `E2B_PID_NS` | `true` | 每沙箱独立 pid ns（N45） |
| `E2B_ENABLE_NET_ISOLATION` | `true` | 每沙箱 netns + fd 注入 connect |
| `E2B_ENABLE_NETWORK` | `true` | 通配/字面 `allowOut` 生效（N42） |
| `E2B_PAUSE_CHECKPOINT` | `1` | pause 先写 checkpoint 图再冻结 |
| `E2B_PLATFORM_DISK_MB` | `8192` | checkpoint 图的平台账上限 |
| `E2B_BASE_IMAGE` | `…python-mcp:3.14@sha256:3675662d…` | MCP-capable 基镜像（digest 固定，见 §12） |

`E2B_PURE_ROOTFS` **未设** ⇒ 走代码默认 **`synth`**（2026-09-27 起；N16 合成骨架 + 真根，`E2B_REAL_ROOT`
未显式设置时跟着它走 —— `envd_service/config.py::resolve_real_root`）。**退回杆是一句话**：
`E2B_PURE_ROOTFS=off`（回到 N15 的 identity 根）。生产是 image-rootfs（两套清单都设 `E2B_BASE_IMAGE`），
不受它影响；只有**不带 base image** 的 pure 部署 / 本地池才用到这条默认，见
`docs/production-deployment-requirements.md` §2.4.11。**2026-09-28 直接问线上 worker**（镜像
`0.1.0-721-g01e4b72-20260927-231235`，`kubectl exec e2b-worker-0 -- python3 -c …`）：把两个键从进程环境里
去掉后 `pure_rootfs=synth`、`resolve_real_root(pure_shape=True)=True`、`resolve_real_root(pure_shape=False)=False`
（image 形态不变）；显式 `E2B_REAL_ROOT=0` + `synth` 时 `check_pure_rootfs_pairing` 按名拒绝
（`E2B_PURE_ROOTFS=synth without E2B_REAL_ROOT=1: …`）—— 即**线上跑的这份镜像就是新默认**。

**别把 `python-mcp:3.14` 当稳定引用**：`deploy/docker/Dockerfile.mcp-base` 用的是
`pip install --no-cache-dir mcp uvicorn`，**没有钉版本**，所以每次重建它都可能产出不同内容 ——
2026-09-27 这次发版重建后该 mirror tag 指向 `sha256:4474e78f…`，而集群与其它清单里钉的
`E2B_BASE_IMAGE` 仍是 `sha256:3675662d…`（**刻意保留**：mirror tag 的 digest 变了**不代表**
线上基镜像换了；旧 digest 依旧可解析，worker 还成功预热过它）。**换基准镜像时按 digest 换，
不要按 tag 换**，否则会静默换掉所有沙箱的基底。（2026-09-25 那次重建同样把 tag 推成过
`sha256:e91b0ae2…`，pin 同样没动；两个 digest 的对照见 §12。）

**真根（N35/N14）已上线**（2026-09-25 单节点灰度 → 推广，两台 worker；2026-09-27 仍在线上）：

* worker 环境里有 `E2B_REAL_ROOT=1`，**写在 `deploy/k8s/worker.yaml`**（不是临时 patch）；
* **整栈都在同一版本**（`autoscaler` / `control-plane` / `e2b-worker` 三个工作负载），
  `kubectl diff -f <渲染出的整栈>` 为空 ⇒ 线上与仓库规格一致，`apply.sh` 幂等。
  ⚠ 这一条是**差点漏掉**的：真根那次只滚了 worker，控制面与 autoscaler 还停在上一版
  `0.1.0-440-…`，是后来跑整栈 `apply.sh` 才收敛的 —— **"改了一个工作负载"不等于"发了一版"**，
  判断当前状态要看全部 `deploy,sts`，不要只看你要改的那个。
* seccomp 档已是 N35 那份：两台节点上 `/var/lib/k0s/kubelet/seccomp/sandlock-worker.json`
  都是 14927 字节、`sha256 071486c0…`（与仓库文件逐字节相同），`mount`/`umount2`/`pivot_root`
  三个都在允许组里。

验收（同一条命令、两台 worker 各跑一次；探针就是 N35 的形状）：

| worker | 根形态 | 对照（动态 ELF） | 判别（shebang 脚本，同一条命令里写→chmod→执行） |
|---|---|---|---|
| （灰度后）两台 | 真根 | `exit 0` `ELF_OK` | **`exit 0` `SHEBANG_OK`** |
| （上线前）旧规格 | 模拟根 | `exit 0` `ELF_OK` | `exit 126` `Permission denied` |

第三行是这次唯一一次能拿到"同一集群上两种形态对照"的机会，所以记在这里：它就是 N35 要消灭的那个形状
（用户级安装 `pip install --user` 落在 `~/.local/bin` 的 console script 属于同一类）。
另有 `deploy/scripts/multinode_smoke.py` 与 `deployment_smoke.py` 在其后全绿。

**上线顺序（必须遵守，且已经由 worker 自己兜住）**：**先应用 seccomp 档、再打开 `E2B_REAL_ROOT`**。
反过来的话每个 `Sandbox.create()` 都会 EPERM。现在不必靠人记得：worker 第一次建 executor 时会用
子进程把 userns→mount ns→bind→pivot_root→umount2 走一遍，失败即抛
`RuntimeError`，并写明是哪一步失败、该应用哪个文件
（`envd_service/executors/sandlock.py::_real_root_capability`，用例 `tests/unit/test_real_root_gate.py`）。

**这次上线前的状态（留档，说明"档没应用"这个坑长什么样）**：线上当时跑的是
`8ea5909`（2026-09-15）那份 profile —— 三处（节点文件、集群 ConfigMap、DaemonSet 注解）
一致地写着 `0e07967a…`；那份里 `pivot_root` **根本不在允许组**（`defaultAction: SCMP_ACT_ERRNO`
无条件拒），`mount`/`umount2` 只在 `includes.caps: [CAP_SYS_ADMIN]` 组里而 worker 早已去掉
SYS_ADMIN ⇒ 真根一行都建不起来。也就是说 N35 的 `kubectl apply`（installer 修订 `84f1c11`）
**从未在集群上执行过**，而索引里只写了"上线顺序"、没写"到底应用了没有"。

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
kubectl apply -f deploy/k8s/seccomp-installer.yaml   # ① 档（先）
# 等 DaemonSet ready，再动 worker 的 E2B_REAL_ROOT    ② 开关（后）
```

### 7.5 C3 Task 2 的 internal API 身份绑定（**仓库已落，集群未上线**）

**现状（未变）**：集群跑的还是 §7 那一版；`control_plane.yaml` 里**没有** `E2B_NODE_ADDRESS_MODE`，
也没有控制面的 ServiceAccount/RBAC。也就是说线上此刻仍是"舰队共享 key + 自陈 node_id/address"。

**下一次上线会带什么**（仓库现状，`docs/open-issues.md` N49 行有"关了哪些/没关哪些"的逐条说明）：

- `deploy/k8s/control-plane.yaml` 显式 `E2B_NODE_ADDRESS_MODE=k8s` + 同名 ServiceAccount 与一条
  **只读 pods**（`get`，namespaced Role/RoleBinding）；没有它解析器**取不到地址就拒**（503 点名），
  不会退回自陈。
- node-scoped 四个端点（`register` / `{id}/heartbeat` / `{id}/sandboxes` / `{id}/reconcile`）的自陈
  直接用不了：解析不到的**自称**一律 503，注册地址不再取 `body["address"]`。
- **没有** per-node key：仍是共享 key，所以"同一节点上的其它东西同时有该节点 IP 与 key"这档盲区
  **仍在**（N49 的"未关闭"）。

**⏳ 待部署窗口执行（判据 ③：两节点 worker 的请求在 CP 侧源 IP 不同）**：命令见 C3 Task 2 报告
§7（`open-cluster-tunnel.sh` → `KUBECONFIG=tmp/k0s/kubeconfig` → 取两个 worker 的 pod IP →
rollout 后从两个 pod 各发一次节点作用域请求 → `kubectl -n sandlock logs deploy/control-plane` 里
两行 `came from` 必须是**两个不同**的 pod IP）。**本行待该窗口完成后回填结果。**

### 7.6 C3 的 per-node agent（**仓库已落（Task 3 + Task 4 片 B），集群未上线**）

**现状（未变）**：集群跑的还是 §7 那一版 —— 没有 agent，worker 的 `E2B_SLOT_IDENTITY` 仍是
代码默认 `spawn`（槽位身份由 worker 镜像里的 file-capability `e2b-slot-spawn` 授予），
`E2B_PRIV_HELPER_TRANSPORT` 仍是 `socket`。

**仓库现状（下一次上线会带什么）**：

- `deploy/k8s/c3-agent.yaml`：**一个 DaemonSet、两个容器**，pod 级 `hostPID: true`。
  面 A `agent` = 独立镜像 `e2b-sandlock-agent`（`USER 65534:65534`、BND 只声明
  `SETUID`/`SETGID`、身份取自 `spec.nodeName`）；面 B `maint` = root + `drop:[ALL]` +
  `CHOWN/DAC_OVERRIDE/FOWNER`（与 C1 broker 逐条相同），挂基线那个 `sandbox-shared` PVC 与
  节点本地 `/var/lib/e2b-images`，**载荷是 Task 4 片 B 装上的**：与面 A **同一个服务**
  （一张 op 表：`grant-slot` + `chown`/`rm`/`walk`），但听**自己的端口 49986**（D22 ——
  两个容器共享 pod netns，都绑 49985 会 `EADDRINUSE`；而 file op 落到 65534 的面 A 上，
  NFS 每个 chown 都 `EPERM`）。两者都在基线（不在
  overlay 差异里），禁项逐条成立：无 `SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/host 网络/特权容器，
  且**没有**任何"禁止提权"式字段（那会让内核静默忽略 file capabilities）。
- 同文件的 `NetworkPolicy e2b-c3-agent`：agent 的入口**只允许 control-plane pod**（**两个
  端口**：49985 面 A、49986 面 B，同一条规则的端口列表）—— "只有两条通道"的连接层那一半，
  `worker ↔ agent` 在连接层就不存在。
- **身份来源（D21 选项 1 的部署前提）**：`worker.yaml` 的 worker 容器现在**显式 pin**
  `runAsUser: 65534`/`runAsGroup: 65534` —— CP 的可信来源读的就是 pod spec 的
  `securityContext`，只靠镜像 `USER` 会读成"未知"⇒ 不记身份 ⇒ 每个需要身份的文件 op 具名 503。
- **worker 镜像不再含 `/var/lib/e2b-priv/`**（判据 2/15）：`e2b-slot-spawn`/`e2b-maint` 只在
  agent 镜像里；worker 的 BND 因此是**空集**（`SETUID`/`SETGID` 随二进制一起去掉），
  `E2B_PRIV_HELPER_TRANSPORT=agent`。C1 的 `e2b-priv-broker` DaemonSet 保留到 Task 7，
  但它现在跑 **agent 镜像**（`e2b-maint` 在那儿），这是 `socket` 回退与 worker 的
  `wait-for-broker` 闸门还能成立的前提。
- **CP 侧新增**：`E2B_ROUTE_B_TMP_ROOT`（`scope-slot-document` 的路径由 CP 推导，缺它该 op
  具名 503）、`E2B_C3_AGENT_MAINT_PORT=49986`（面 B 端口）、`E2B_IMAGE_CACHE_DIR` 改为
  fork 出 worker 的节点本地缓存路径 `/var/lib/e2b-images` 并显式设
  `E2B_IMAGE_OCI_DIR=/var/lib/e2b-sandboxes/_images`（`chown-secret` 的路径必须与 worker
  写 secret 的目录逐字一致，而 template 的 OCI tar 必须留在共享卷上）。
- 凭据 `E2B_C3_AGENT_TOKEN`：**只**出现在 control-plane 与 agent 两处（worker 清单/镜像里
  一个字都没有，pin 在 `tests/unit/test_c3_internal_api_shape.py`），由
  `deploy/k8s-k0s/secrets.sh` 与其他托管键一起生成（**上线前必须先跑它**，否则 agent pod 起不来）。
- RBAC：control-plane 的 Role 从 `get pods` 扩到 `get,list pods`（寻址要按 label 列**本节点**
  的 agent pod），范围不变（本命名空间的 pods）。
- worker：`E2B_SLOT_IDENTITY=agent-grant`（回退 = 改回 `spawn`）；**没有** `hostPID`（它会把
  槽位 pid 放进宿主 pid namespace，`NSpid` 判别值当场失效）。
- 上线闸门：`deploy/k8s-k0s/apply.sh` 的 rollout 顺序变成 **broker → agent → worker**（agent 是
  worker 的新上游，fail-closed 没有回落路径）。
- 旋钮：`E2B_SLOT_IDENTITY`（worker，`spawn|agent-grant`）、`E2B_SLOT_IDENTITY_REPORT_TIMEOUT_S`、
  `E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S`、`E2B_SLOT_IDENTITY_UNSHARED_TIMEOUT_S`、
  `E2B_C3_AGENT_URL`/`_NAMESPACE`/`_LABEL`/`_PORT`/`_MAINT_URL`/`_MAINT_PORT`/`_TOKEN`/
  `_TIMEOUT_S`/`_FILE_OP_TIMEOUT_S`/`_MAX_CONCURRENCY`（末者出厂 **64**，理由写在
  `control_plane/config.py` 与清单注释里；k8s 只设 `_MAINT_PORT`，compose 设 `_MAINT_URL`）。

**⏳ 待部署窗口执行（判据 1/2/3/7 的 k8s 臂 + agent 上线 + `E2B_SLOT_IDENTITY` 切换）**：命令见
C3 Task 3 slice B 报告 §4（`open-cluster-tunnel.sh` → `KUBECONFIG=tmp/k0s/kubeconfig` →
`kubectl kustomize deploy/k8s-k0s | kubectl diff -f -` → 跑 `secrets.sh` → `apply.sh` → 真机复验
`probe_c3_userns_map_handoff.py --role forker/agent`）。**本行待该窗口完成后回填结果。**

> ⚠ 判据 13（`NSpid` + cgroup 双命中）与 16（并发建箱）**不要在 k8s 上验收**：这里是 1 节点 2
> 副本 worker，两条都会"全绿但什么都没测到"。它们必须在
> `deploy/compose/docker-compose.multinode.yml`（3 worker 同机，已加 `c3-agent` 服务）上跑。

### 7.7 C3 Task 5 的 CP 无 root（**仓库已落，集群未上线**）

**现状（未变）**：集群上的 control-plane pod 仍是 §7 那一版 —— 主容器以 **`uid=0`** 跑，
pod 里有一个 `runAsUser: 0` 的 `image-cache-init`，盘上 `_volumes` 是 **`0:0 755`**（§13.6
那张表就是在这个形态下量的）。

**仓库现状（下一次上线会带什么）**：

- **CP 主容器 `runAsUser: 65534` + `runAsGroup: 65534`**（`deploy/k8s/control-plane.yaml`）。
  取值是量出来的，不是对称：平台自己的目录已经是 65534、`state/.uid_pool.lock` 是
  `65534:65534 0600`（换 uid 连开都开不了，而它在启动期就被碰）、镜像缓存的属主也是 65534。
- **CP pod 里没有 root 容器了**：`initContainers` 为空，`image-cache-init` 搬到了 agent
  自己的 pod（`deploy/k8s/c3-agent.yaml` 的 `storage-init`，root，与面 B 同一个理由：这台
  NAS 的 `chown` 走 AUTH_SYS，只认 uid 0）。**`buildkit` sidecar 是点名保留的例外**：仍是
  镜像的 uid 1000 + `seccompProfile: Unconfined`，且**不能**加
  `allowPrivilegeEscalation: false`（rootlesskit 的 `newuidmap` 会死）。它不是 root，
  不违反口径，但它是这个 pod 里唯一保留宽 profile 的容器。
- **卷存储的属主交棒（裁定 D24）**：`storage-init` 在**每个节点**上做一次
  `chown 65534:65534 _volumes`（以及已存在的 `_volumes/_meta`），**非递归**（它下面的卷数据
  目录与每沙箱切片属于池 uid，`chown -R` 会在每次 agent 滚动时把它们抢回来），幂等且有名字
  （`already belongs to uid 65534` / `-> uid 65534 mode …` / `_meta` 缺失时的
  `does not exist -- nothing to hand over`），**两个目标各自校验**（`_meta` 单独一条门 ——
  评审 round 1 的 Important，它从前没有门且成功行无条件打印），拒绝时 pod 停在 init 并打印
  一次性命令。它的能力集与面 B 逐条相同（`drop: [ALL]` + `CHOWN/DAC_OVERRIDE/FOWNER`；
  `DAC_OVERRIDE` 是量出来的，见 §13.6）。D24 的理由、偏离 §3.2 字面的说明与**部署窗口复验程序**写在
  `docs/c3-privilege-relocation.md` §13.6（裁定）与 §13.6.1（程序）。
- **CP 侧的具名失败**：`VolumeRegistry.create` 在属主不对时抛
  `VolumeRootNotOwnedError`，消息里带同一条 `chown 65534:65534 ...`（不再是裸 `EACCES`）。
- **compose 车道本任务未动**（记账项）：那两条栈的 control-plane 仍以 root 跑，它们的
  `image-cache-init` 本来就是**独立服务**（§13.3 判定"形态已经是对的"），而 Task 5 的清单
  判据是 **k8s 的 pod**。要让 compose 也收敛，是同一套交棒 + 同一条 `runAsUser` 的独立改动。

**⏳ 待部署窗口执行（判据 8 的真机臂）**：**先把 agent 滚起来**（`storage-init` 把 `_volumes`
交给 65534），**再滚 control-plane** —— 顺序反了也不会坏（交棒幂等、且 CP 只在**用户建卷**时
才需要它），但反过来时第一次建卷可能撞上 `VolumeRootNotOwnedError`（有名有姓，按它给的命令
处理即可）。随后跑 `docs/c3-privilege-relocation.md` §13.6.1 的六步复验，并把结果回填到本节
与 §13.6 的那张表。**本行待该窗口完成后回填结果。**

### 7.8 C3 Task 6 的自愈（**仓库已落，集群未上线**）

**现状（未变）**：集群上的 agent 仍是 Task 4/5 那一版 —— 它不扫描、不主动发起任何连接
（NetworkPolicy 是 `policyTypes: [Ingress]`），而 worker 的孤儿清扫在 agent 形状下被具名关掉
（`E2B_PRIV_HELPER_TRANSPORT=agent` 的启动告警）。所以**今天的集群上，孤儿树没有人回收** ——
这正是 Task 6 要补的那条可用性硬依赖。

**仓库现状（下一次上线会带什么）**：

- **面 B 多了一个周期巡检**（`E2B_C3_AGENT_SCAN=on`，只挂在挂了共享工作区的那个容器上）：
  首扫 30s、之后每 120s 扫 `<workspaces>/*`，把**看见的沙箱 id** 报给控制面
  `POST /internal/nodes/<宿主名>/agent/inventory`（body 只有 `{"sandboxes": […]}`），控制面用
  **权威记录**判定孤儿后指令**同一节点**的 agent 用既有的 `rm` 删。
- **一条新的出口**：`deploy/k8s/c3-agent.yaml` 的 NetworkPolicy 变成 `[Ingress, Egress]`，出口
  只允许 `app: control-plane` 的 3000 端口（`E2B_CONTROL_PLANE_URL` 指向它）。控制面侧没有
  NetworkPolicy，无需为这条新入口加规则。
- **CP 侧的三档门**（记录不共享 / 有记录读不出来 / 枚举条数对不上 `activeSandboxes` 三者任一
  ⇒ **整轮具名推迟**，什么都不删）写在 `control_plane/self_heal.py`，逐条有用例。

**⏳ 待部署窗口执行（判据 9：worker 崩溃不重启时盘上仍在 N 分钟内收敛）**：

1. 先把 agent 滚上去（面 B 的新 env + NetworkPolicy），确认 `E2B_C3_AGENT_SCAN=on` 的那行启动日志
   与 `c3-agent inventory:` 的周期行。**这一条要按下面三种失败分开读**（它们的修法不同）：
   - `the control plane at http://control-plane:3000 is unreachable: … Name or service not known`
     （或 `Temporary failure in name resolution`）⇒ **DNS 被出口策略挡了**。这条路径上 agent 主动发起的
     出口只有两条：到 control-plane pod 的 3000，以及到 kube-system 集群 DNS 的 53（UDP+TCP）。
     若集群 DNS 的 pod 标签不是标准的 `k8s-app: kube-dns`（例如换过发行版/改过 label），
     就照它自己的 selector 改 `deploy/k8s/c3-agent.yaml` 的第 ② 条出口 —— **不要**改成放通全部出口。
   - `… is unreachable: timed out`（解析没问题、连接不通）⇒ **策略或 Service 挡了**：先确认
     NetworkPolicy 的第 ① 条与 `control-plane` Service 的 3000 端口，再看 CNI 是否在 agent 的节点上
     真执行了 egress。
   - `the control plane refused the inventory report (status 403)` ⇒ 凭据过了、**源 IP 不符**：查 CNI
     是否保留了源地址（§11.1 第 9 项那两个前提）与 `E2B_C3_AGENT_TOKEN` 两边是否一致；
     `401` 则是 token 不一致，`503` 是 CP 侧没配 agent 凭据或查不到这个节点的 agent。
2. 造一棵"记录已不在、树还在"的孤儿（例如删掉 CP 记录后让 worker 停摆 / 直接造一棵无记录的
   `sbx_*` 树），**等 2–3 分钟**，断言树消失、CP 日志出现 `c3 self-heal: node=… removed the
   orphan tree …`。
3. 反向臂：留一棵**有记录**的树（活沙箱），确认它出现在 `protected` 里且**不**被删；再滚一次
   control-plane 复做同一断言（Task 6 简报的用例②："滚动重启期间不误删活沙箱"）。
4. 把结果回填到本节与 `docs/c3-privilege-relocation.md` §11.1 第 5 项。

### 7.9 C3 Task 7 上线：退役 C1 的节点 broker（**2026-09-29，已执行**）

> **这是现行的现状。§7.1–§7.8 里所有"仓库已落，集群未上线"的段落，到这一次为止都已上线。**

**版本**：`0.1.0-764-g8776c67-20260929-215208`（= `deploy/stack/.version`，`apply.sh` 就是按它渲染的）。
镜像：worker / agent / autoscaler / quota-agent / control-plane-gateway 都在这个 tag 上（ACR，
arm64）。构建走的是 `PLATFORMS=linux/arm64 ./deploy/scripts/build-and-push.sh` + 逐个 `docker push`
（单平台分支**只 `--load` 不推**，所以推是手动的）+ `docker manifest inspect` 逐个复核 —— 这是
`tmp/wt-c3` 里上一轮用过的同一条路径。

**动作**：`apply.sh`（渲染后 apply，8 个镜像引用 pin 到上面的 tag；等 `ds/e2b-c3-agent` → 等
`sts/e2b-worker` → 预热 base image）→ **`kubectl delete daemonset e2b-priv-broker`**。
⚠ `apply.sh` 不 prune：清单里删掉 `priv-broker.yaml` **不会**让集群上那个 DaemonSet 消失，
它必须显式删（这是"退役"唯一一步不在清单里的事）。

**pod（实测）**：`control-plane` 2 个副本各 `2/2`、`autoscaler 1/1`、`e2b-worker-0/1` 各 `1/1`、
`e2b-c3-agent` 两个各 `2/2`、`seccomp-installer 2/2`、`redis 1/1`，
**没有任何 `e2b-priv-broker` pod**（`kubectl -n sandlock get pods | grep -c broker` = 0）。

**形状（实测，都是 pod spec / `/proc` 的读数）**：

| 判据 | 读数 |
|---|---|
| worker 的 init | **没有**（`initContainers` 为空）—— `wait-for-broker` 随 broker 一起走了 |
| worker 的 env | `E2B_PRIV_HELPER_TRANSPORT=agent`、`E2B_SLOT_IDENTITY=agent-grant`；**没有** `E2B_PRIV_HELPER_SOCKET` |
| worker 的挂载 | 只剩 `shared`（PVC）与 `image-cache`（hostPath）；**没有** `broker-socket` / `/run/e2b-broker` |
| worker 的 cap | `CapEff=0000000000000000`、`CapBnd=00000000a80425fb`（无 `capabilities` 块） |
| agent 面 A | `runAsUser=65534`、`CapEff=0`、`CapBnd=00000000a80425fb`；`as_uid` = `cap_setgid,cap_setuid=ep` |
| agent 面 B | `runAsUser=0`、**`CapEff=000000000000000b`**（恰好 `CHOWN`+`DAC_OVERRIDE`+`FOWNER`）；`e2b-maint` = `cap_chown,cap_dac_override=ep` |
| 禁项（§2.3） | worker 与 agent 的 pod spec 里 `SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`/`hostNetwork`/`allowPrivilegeEscalation`/`no-new-privileges` **一个字都没有** |
| broker 的属主 init 搬家后 | agent pod 的 `storage-init` 与 `workspace-root-init`（都 `runAsUser: 0`）各跑了一轮：日志见下 |

```
storage-init: /var/lib/e2b-images is owned by uid 65534
storage-init: /var/lib/e2b-sandboxes/_images is owned by uid 65534
storage-init: /var/lib/e2b-sandboxes/_volumes already belongs to uid 65534 (mode 755) -- nothing to do
workspace-root-init: /var/lib/e2b-sandboxes/workspaces/_snapshots owner=65534 mode=755
workspace-root-init: /var/lib/e2b-sandboxes/state/_runtime owner=65534 mode=711
workspace-root-init: /var/lib/e2b-sandboxes/state/_runtime/.checkpoints owner=65534 mode=711
workspace-root-init: /var/lib/e2b-sandboxes/workspaces owner=0 mode=1777
workspace-root-init: /var/lib/e2b-sandboxes/state owner=65534 mode=1777
workspace-root-init: /var/lib/e2b-sandboxes/workspaces/_migrate owner=65534 mode=1777
```

**N48（属主 0 的老树，判据 10）**：删 broker 前后各查一次，`workspaces/` **下没有 `owner=0` 的条目**
（`find … -mindepth 1 -uid 0` = 0；两个 agent pod 上各测一次，见 `.superpowers/sdd/task-7-report.md` §1）。
注意 `workspaces/` **目录本身**仍是 `0:65534 mode=1777`（粘滞位、world-writable）——那是卷根约定，
不是残留，`workspace-root-init` 的 gate 也按"owner 不是 65534 但可写"放行。

**冒烟**：`multinode_smoke.py` = `MULTI-NODE SMOKE OK`（4 箱 2+2、命令/文件/stdin、预留归零）；
`deployment_smoke.py` 的 **C3 段全绿**（建箱 + 命令/文件过网关、跨节点迁移保文件、网络配置、
远端卷挂载 + 兄弟卷隔离、预留归零），**卡在 Track Z 的模板构建那一段**：
`e2b.exceptions.BuildException: buildkit build exited with code 1`，buildkit 日志是
`dial tcp …: i/o timeout` / `mkdir /nonexistent: permission denied`，而 CP pod 里
`registry-1.docker.io` **Network is unreachable**、ACR 可达 —— 模板构建要拉 Docker Hub 的
`python:3.11-slim`。**这不是本次改动引入的**：上一轮（21:07，改动之前）的 `deployment_smoke` 日志
在**同一步**以**同一个异常**失败（`tmp/build/deployment_smoke.log`）。

**残留（无害，记在这里）**：节点上 `/run/e2b-broker/`（hostPath `DirectoryOrCreate` 建的）目录还在，
但**没有任何组件挂它、也没有人读**（worker 的挂载已删）。要清就在节点上 `rmdir`；不清也不影响。

> ⚠ **上表里 worker 的 `CapBnd=00000000a80425fb` 是收口评审修掉的那个点** —— 见 §7.10。

### 7.10 收口评审：`workspace-root-init` 的能力集 + worker 的 BND（**2026-09-29，已执行**）

收口评审的两条 must-fix 都落在 pod spec 上，所以各滚了一次栈（同一 tag，只换清单）。

**版本**：`0.1.0-768-g17aa2fb-20260929-223205`（= `deploy/stack/.version`）。构建链与 §7.9 逐字相同：
`PLATFORMS=linux/arm64 ./deploy/scripts/build-and-push.sh`（单平台分支只 `--load`）→ 四个
`e2b-sandlock-{worker,agent,autoscaler,quota-agent}` 手动 `docker push` → 五个 tag 逐个
`docker manifest inspect` 复核 → `apply.sh`（agent DaemonSet → worker StatefulSet → 预热）。

**改了什么**：① `deploy/k8s/c3-agent.yaml` 的 init `workspace-root-init` 从"`runAsUser: 0` +
无 `capabilities:` 块"改成 `drop: [ALL]` + `{CHOWN, DAC_OVERRIDE, FOWNER}`（与面 B/`storage-init`
逐条相同，理由同 §7.9 的 `storage-init`）；② `deploy/k8s/worker.yaml` 的 worker 容器加
`capabilities: {drop: [ALL]}`（此前省掉整块 ⇒ 继承的是 runtime 默认 BND）。

**cap 读数（before → after，`CapBnd` / `CapEff`）**：

| 容器 | before | after |
|---|---|---|
| worker `worker` | `0xa80425fb` / `0` | **`0` / `0`** ✔（判据 2/15 现在字面成立） |
| agent 面 A `agent` | `0xa80425fb` / `0` | `0xa80425fb` / `0`（不动） |
| agent 面 B `maint` | `0xb` / `0xb` | `0xb` / `0xb`（不动） |
| agent init `storage-init` | `CHOWN,DAC_OVERRIDE,FOWNER` | 同（不动） |
| agent init `workspace-root-init` | **运行时默认 14 条（含 `CAP_NET_RAW`）/ 同** | **`CHOWN,DAC_OVERRIDE,FOWNER` / 同** ✔ |

**怎么读的（两个坑）**：① agent pod 是 pod 级 `hostPID: true`，所以 `kubectl exec … cat
/proc/1/status` 读到的**是宿主机的 pid 1**（会显示满集），不是容器自己 —— 要读容器自己的进程得用
`/proc/self/status`；② 两个 init 容器退出得比 `exec` 还快，所以它们的读数取自节点上
`k0s ctr -n k8s.io c info <container-id>` 的 OCI `process.capabilities`（bounding/effective），
两台节点各查一次。init 的日志在两台节点上都走完并全绿（`… is writable by uid 65534`），
说明三条 cap 够 `chown`/`chmod`/`mkdir -p` 用 —— 这是 §7.9 `storage-init` 那条实测的现场复核。

**冒烟**：`multinode_smoke.py` = **`MULTI-NODE SMOKE OK`**（4 箱 2+2、命令/文件/stdin 过网关、
kill 后两个 worker 的预约都归 0）。

### 7.11 compose 车道的三条缺口收口（**2026-09-30，分支 `feat/c3-compose-gaps`；未上线，只在本地栈实测**）

§7.9/§7.10 的读数全在 k8s。这一轮补的是**三个分离 compose 栈**（`deploy/compose/docker-compose.prod.yml`、
`deploy/compose/docker-compose.multinode.yml`、`deploy/stack/docker-compose.prod.yml`）里 C3 明知留下的
三个缺口：compose CP 仍是 root、没有策略层、multinode 没有 Redis。前两个是"k8s 有、compose 没有"的
等价物缺失，第三个让那条车道的孤儿巡检整条失效。改法与现场读数（全部来自本机 multinode 栈，
镜像 = 本树构建的 `:c3-gaps`）：

**① CP 收到 65534 + 属主交棒**

三个栈的 `control-plane` 都加了 `user: "65534:65534"`（= k8s 的 `runAsUser/runAsGroup: 65534`）。
栈里**只剩两个 root 服务**，两个都必需：`image-cache-init`（root one-shot，做属主交棒）与面 B
`c3-agent-maint`（NAS 上 `chown` 只有 uid 0 做得成）。`image-cache-init` 现在同时承担 k8s
`storage-init` 的活：D24 的卷存储交棒（**非递归**、幂等、逐目标校验、失败具名 FATAL，措辞与 k8s
`storage-init` 同源）+ CP 自己写的平台命名空间 + "根是 65534 或 1777"那两条。

```
$ docker exec c3gaps-control-plane-1 id
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
$ docker exec c3gaps-control-plane-1 sh -c 'grep -E "^(Uid|CapEff)" /proc/self/status'
Uid:	65534	65534	65534	65534
CapEff:	0000000000000000
$ curl -sS -X POST http://127.0.0.1:3300/volumes -H 'X-API-Key: local-key' \
      -H 'Content-Type: application/json' -d '{"name":"c3-gaps-store-probe"}'
{"volumeID":"vol_329a97c908c8426f", …}                                    # HTTP 201
$ docker exec c3gaps-control-plane-1 stat -c '%n %u:%g %a' /var/lib/e2b-sandboxes/_volumes{,_meta}
/var/lib/e2b-sandboxes/_volumes 65534:65534 755
/var/lib/e2b-sandboxes/_volumes/_meta 65534:65534 755
```

（CP 的 `CapBnd` 仍是 runtime 默认 `0xa80425fb` —— 与 k8s CP 主容器一致：那边也只钉
`runAsUser/runAsGroup`，不写 `capabilities.drop`；非 root ⇒ `CapEff` 为 0。）

**② agent 两面进专用网络 `agent-plane`**（k8s 的 NetworkPolicy 在 compose 的等价物）

三个栈都是：`c3-agent` / `c3-agent-maint` **只**挂 `agent-plane`，`control-plane` 同时挂
`default`（`worker ↔ CP` 那条不变）与 `agent-plane`，worker 全部留在默认网络。

```
$ docker inspect -f '{{.Name}} {{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' \
      c3gaps-c3-agent-1 c3gaps-control-plane-1 c3gaps-worker-1-1
/c3gaps-c3-agent-1      c3gaps_agent-plane=192.168.117.2
/c3gaps-control-plane-1 c3gaps_agent-plane=192.168.117.4 c3gaps_default=192.168.147.6
/c3gaps-worker-1-1      c3gaps_default=192.168.147.5
$ docker exec c3gaps-worker-1-1 python3 -c 'import socket; socket.gethostbyname("c3-agent")'
gaierror: [Errno -2] Name or service not known      # worker 解析不到 agent 的两个服务名
```

⚠ **本机（OrbStack）证明不了这条的 IP 那一半**：OrbStack 的已知行为是不同 user-defined 网络之间
仍可按 IP 互通（上游 issue orbstack#1944 / #2492），实测 worker 直连 `192.168.117.2:49985`
**是通的**（标准 Docker daemon 会由 `DOCKER-ISOLATION-STAGE-2` 丢掉这条包；OrbStack 的 VM 里
`iptables` 这个命令都不存在）。所以这条拒止在本机只到"名字 + 凭据"两层：worker 拿着 agent 的
IP 打过去，agent 自己按 token 具名拒（实测 `POST …/agent/grant-slot` 无 token / 错 token 都是
`401 {"error":"unauthorized"}`），而 **worker 侧没有 token 可用**（`E2B_C3_AGENT_TOKEN` 只在 CP
与两个面上，`tests/unit/test_c3_agent_manifest.py` 三方都钉着）。**要 IP 层真拒止，目标机必须是
实现了网络隔离的 Docker daemon**（本仓库的目标机 Rocky Linux + 原生 Docker 属于这一类）；
本机验收的结论按"名字层 + 凭据层"读。

反向那一半（agent 只出得去 CP）在本机是**真的**（`internal: true` 生效）：
`docker exec c3gaps-c3-agent-1 python3 …` 连 `worker-1:49983` = `OSError: [Errno 101] Network is
unreachable`，连 `control-plane:3000` = ok。

**③ multinode 栈补 Redis，自愈真的活了**

`deploy/compose/docker-compose.multinode.yml` 加了 `redis`（`redis:8-alpine` + `--requirepass
${E2B_REDIS_PASSWORD:-local-redis-password}`，与另外两个栈同形），CP 加
`E2B_REDIS_URL: redis://:…@redis:6379/0` + `depends_on: redis: service_healthy`，面 B 打开
`E2B_C3_AGENT_SCAN=on`。门的读数（面 B 的日志，30s 首扫 + 120s 周期）：

```
INFO:deploy.c3_agent.scan:c3-agent inventory: node=c3-agent scanned=0 protected=0 orphans=0 removed=0 failed=0 deferred=-
```

手工放一棵沙箱形状的孤儿树（`/var/lib/e2b-sandboxes/sbx_<32 hex>/`，65534）后下一轮：
`scanned=1 protected=0 orphans=1 removed=1 failed=0 deferred=-`，树消失 —— "CP 决策 + agent 执行"
整条链路真跑过。

⚠ **顺带量到并修掉的两个真缺陷**（详见 §11.2.1 第 14/15 条）：compose 的巡检上报原来被源 IP
因子整条拒（面 B 上报、期望值却只取面 A 的地址 ⇒ 每轮 403），以及 agent 入口不放开 INFO ⇒
**成功**轮次那一行看不见。前者是"给 Redis 就活了"这个前提本身不成立的原因，两者都已在同一分支修掉。

**测试与验收**：本树 unit 相关文件 417 passed（C3/worker/compose/deploy 一组）、
`tests/contract/test_c3_worker_kernel_identity.py` 8 passed（真容器）；
`multinode_smoke.py` = `MULTI-NODE SMOKE OK`（4 箱 2+1+1，命令/文件/stdin 过网关、kill 后预约归 0）；
判据 13 `JUDGMENT 13: all assertions passed`、判据 16 `JUDGMENT 16: all assertions passed`
（并发 in-flight 3 vs 反臂 1）。全部命令与原始输出见
`.superpowers/sdd/c3-compose-gaps-report.md`。

## 8. 改部署的入口

```bash
KUBECONFIG=... deploy/k8s-k0s/apply.sh          # 版本取自 deploy/stack/.version
DRY_RUN=1 KUBECONFIG=... deploy/k8s-k0s/apply.sh    # 只渲染
SKIP_WARM=1 KUBECONFIG=... deploy/k8s-k0s/apply.sh  # 不预热 base image
```

`DRY_RUN` 的输出是**干净的数据流**（进度/诊断走 stderr），所以可以直接喂给 kubectl ——
改清单前想先看"会发生什么"，这是最有用的那条命令：

```bash
DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -        # 看差异
DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl apply --dry-run=server -f -
```

overlay 改了什么、为什么（NAS PV 必须 NFSv4.0、容量与 resources、Calico VXLAN 只能建集群时定）
见 `deploy/k8s-k0s/README.md` —— C1 起**没有** worker `runAsUser: 0` 这一行：特权动作在**基线**的
`e2b-priv-broker` DaemonSet 里（§7）。集群层设计的全貌见 `docs/k8s-deployment.md`。

---

## 9. checkpoint/restore 的上线记录（2026-09-25）—— 历史记录（该版本当天的验收）

> **这是历史记录**：本节记的是 checkpoint/restore 上线当天的实测，**不是当前部署状态**
> （当前版本与形态开关见 §7，最近一次发版见 §12）。本节末的复核行各自跟着当天的版本。

**版本**：`0.1.0-525-g65ad183-20260925-212439`（= 当轮 `deploy/stack/.version`；**当前部署版本
见 §7/§12**，本节末的 2026-09-26 复核行给出重跑这条验收时的版本）。这一轮改了三样
东西，所以 rebuild 链条跑了两遍：E2B 侧代码（主仓 `9ddebc5`）、fork 的 `exclude_main`
（fork `da0faf5`）、fork 的 restore-stub 随 wheel（fork `2d5f2e9`）。整栈同一版本，
`kubectl diff` 只剩版本行 + worker 的两个新环境变量。

> 当晚又滚过两次（`0.1.0-523` / `0.1.0-525`；fork `685301c` 冻结释放 fork 通知的修复、
> `89e8ab2` restore 面包屑 + E2B `65ad183` 记录 slot stderr），链条同上：
> wheel → 镜像 → `apply.sh`。**验收在这两版之后全绿**（见下表）。

**worker 上的两个新开关**（写在 `deploy/k8s/worker.yaml`，不是临时 patch）：

| 变量 | 值 | 干什么 |
|---|---|---|
| `E2B_PAUSE_CHECKPOINT` | `"1"` | `pause` 先写一张 checkpoint 图再冻结；`resume` 时进程不在就恢复它 |
| `E2B_PLATFORM_DISK_MB` | `"8192"` | 图记**平台**的账（不是用户 `diskMB`）；0 会是不限，所以这里显式给一个上限 |

**这次上线在集群上量出来的三件事**（细节见 `docs/checkpoint-restore-e2b-half.md` §6(i)）：

1. **route-B 的 slot 以沙箱自己的池 uid 运行**（实测 `host_uid=10001`，slot 进程 `uid=10001`，
   worker 是 root）。所以图的目录必须**交给那个 uid**：`<base>/_runtime/.checkpoints/<id>`
   （store `0711`、每个 `<id>` 归该沙箱 `0700`）。按原设计写成 `_runtime/<id>/checkpoint`
   （worker `0700`）时，引擎**捕获成功、保存 EACCES**：
   `checkpoint save failed: process error: io error: Permission denied`。
2. **`exclude_main`**：会话的 M0 是 park（`while :; do kill -STOP $$; done`），所以"用户跑过
   东西的沙箱"永远是 2 个活子进程，引擎（正确地）拒绝盲捕 —— 见 fork `da0faf5`。
3. **restore stub 必须随 wheel 走**：`build.rs` 把它编译进 build 容器的 `target/`，而
   `stub_path()` 用的正是那条路径 ⇒ 装到 worker 上的 wheel 里没有它，每次 resume 都被
   `restore-stub was not built` 拒绝（见 fork `2d5f2e9`，修完立刻通）。

**验收状态**（脚本 `deploy/scripts/checkpoint_acceptance.py`，每步都断言）：

* ✅ `pause` 写图：`_runtime/.checkpoints/<id>/latest`，422 KiB，含 `meta.json` / `policy.dat` /
  `process`；属主是那个沙箱的 uid；沙箱自己的树一个字节没动。
* ✅ 平台账随心跳上报：节点视图 `platformDiskUsedMB/platformDiskBudgetMB = 0/8192`。
* ✅ 删掉宿主 worker 的 pod → 重建 → 重新注册 → `resume` **把镜像恢复进了一个新会话**
  （worker 日志逐字：`resumed … into the session (child 1, pid 30); 4 fd(s) could not come
  back (sockets/pipes/memfds): [fd 0 pipe, fd 1 pipe, fd 2 pipe, fd 3 pipe]`）。
* ✅ **thaw 路径（不用重启 worker 的那一半）完全正确**：`pause` → `Sandbox.connect` 解冻后
  **同一个进程继续计数**（3 → 4），而且**在同一个会话里 exec 能拿到输出**
  （`echo THAWED_OK` → `THAWED_OK\n`）。FUP-29 追的就是这条形状，线上是好的。
* ✅ **被恢复的进程活着，而且还在干活**（`0.1.0-525`）：resume 之后计数器继续前进（4 → 5），
  同会话 `exec` 拿到 `EXEC_OK`，图被消费，引擎面包屑
  `child alive 50ms after the handshake: true`。
* 当晚那几次"恢复了但进程不见"的**真因不在引擎，而在验收脚本的命令串**：脚本写成
  `sh -c 'exec python3 …'`，而 worker 本来就把命令包成 `/bin/sh -c "<串>"` ⇒ 会话里的活子进程
  是**第二个 shell**，python 成了孙子；捕获按设计只抓那一个活子进程，于是抓到 shell，
  恢复出来的 shell 唯一的孩子早没了、`wait4` 拿到 ECHILD 就退出。面包屑把这件事说穿了：
  那张图是 `maps=19` / 填充 397 KB（dash 的大小），而真 python 是 `maps=32` / 6.3 MB
  （`/proc` 里也看不到了）。命令串改以 `exec` 开头（worker 的 shell 原地变成 python）后全绿。
  **相关语义已写进 `docs/checkpoint-restore-e2b-half.md` §6(g)**：pause 抓的是会话里的活子进程，
  会 fork 出子 shell 的命令形状（`sh -c '…'`、管道、`&&`）抓到的就是那个子 shell。
  fork 侧登记见 `docs/fork-plan-followups.md` FUP-30（已关，含定位方法与两组数字）。
* ⚠️ **验收脚本的部署语义**：`max_concurrent_commands_per_sandbox` 默认 **1**，
  所以"后台进程还在跑 + 再 exec 一条命令"会排队 30 s 然后 429；脚本里先 `handle.kill()`
  再 exec（`deploy/scripts/checkpoint_acceptance.py` 已按此写）。

**怎么再跑一遍**（密钥从集群里取，不写进仓库）：

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d)
export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)
.venv/bin/python deploy/scripts/checkpoint_acceptance.py
```

（脚本最后会把沙箱 `kill` 掉；想留下现场排障就用 `deploy/scripts/acceptance/probe_restore_state.py`，它不 kill，
并打印沙箱 id 与宿主 pod。）

> **这条脚本会删宿主 worker 的 pod，所以不能在有别人沙箱的时候跑（2026-09-27）**：跑到
> "换 worker"那一半（第二次 `pause` 之后）它会 `kubectl delete pod <宿主 worker>`，让 StatefulSet 重建一个——这是
> "pause 能不能活过它的 worker"的唯一正确测法；代价是**当时宿在那台 worker 上的任何别人的
> 沙箱会一起死**，而 worker 的沙箱注册表是内存态 ⇒ 它们再也没有办法 `resume` 回来。
> 所以脚本在删 pod 之前先读控制面的按节点名单（`GET /internal/nodes/<id>/sandboxes`，
> 与 worker 做分区 reconcile 用的是同一份权威视图）：
>
> * 名单里还有**不属于本次验收**的沙箱 ⇒ **拒绝、退出码 2、一个 pod 都不碰**，并打印出路
>   （先把那些沙箱迁走/杀掉再重跑，或 `--force` 显式承担）；
> * 名单**读不到**（通道/控制面坏了），或名单里**连本次验收自己的沙箱都没有** ⇒ 同样拒绝：
>   拿不到证据就不删，"没有别人"这句话只有在名单完整时才算数；
> * 只有"名单恰好就是本次验收的那一条"才继续。
>
> `--force` 是唯一的显式出口（= 我确认那些沙箱可以和这个 pod 一起死）。**要放 CI 或多人
> 并行跑，前提是目标 worker 上没有别人的沙箱**——别再退回人工肉眼复核：Task 2 报告 §7.6 的
> 那次复核就是空判据（worker 镜像里**没有 `ps`**，`ps | grep` 什么都查不出来）。

> **2026-09-25 夜复核**：部署 `0.1.0-527-g946daa9` 上再跑一遍这条验收，全绿（含"删掉宿主
> worker pod → 重建 → resume"，日志 `tmp/k0s/restore-recheck3.log`）。对照 §9 上面那两轮，
> 这次先红了**两次**、两次都不在 restore，而是验收脚本自己的两个毛病，已经修掉并写进
> `docs/checkpoint-restore-e2b-half.md` §6(j)：① `kubectl` **通道必须活着**（它死了会让
> `image_on_node` 打出空列表，看起来像"图没写"，而 worker 日志里明明写着写了 —— 脚本现在
> 开头就 `kubectl get nodes` 前置断言，并把 stderr 带进断言消息）；② 夹具的计时器文件改成
> **临时文件 + `os.replace`**（原来 `open(w)` 的截断窗口一旦被 `pause()` 冻住，文件在整个冻结期
> 都是空的，读者拿到的 `""` 被当作"还没写" ⇒ 误报"计数消失"）。

> **2026-09-26 复核**：`deploy/scripts/checkpoint_acceptance.py`（本轮**从 `tmp/k0s/` 转正进仓库**，
> 成了这条能力的常备判据）在 `0.1.0-597-g3701a53-20260926-163057`（= `deploy/stack/.version`，
> 含 N15 的 `_chroot_root`、F11 多副本、两次 fork 修复、**N27 的树根下沉迁移**）上全绿：
> 20 行 `{"step": …}`、末行 `{"step": "OK"}`、退出码 `0`（日志 `tmp/k0s/checkpoint-task2.log`）。
> 这条能力在生产形态（image-rootfs + `E2B_REAL_ROOT=1`）下**可用**。
>
> **"恢复后不能 exec"不是拦路虎**：D9 已在 2026-09-25 由 fork `1f41f1a` 关闭 —— E2B 走
> `restore` verb 把镜像恢复**进会话**，`exec` 继续由 init 服务（判据就是上面「验收状态」里那条
> `exec_after_resume`：`EXEC_OK\n`）。restore stub 的交付也不再走宿主路径：fork `a6f6b04`
> 改成按描述符投递，模拟根与真根两态都有用例（`test_restore_resumes_inside_a_chroot_root`）。
> 剩下的都是**语义与运维**问题，以及 Task 1 在会话路径上补的那两条用例（见
> `docs/superpowers/plans/2026-09-26-checkpoint-restore-productization.md`）。
>
> **转正这一轮改掉的第 3 个"脚本自己的毛病"（N27 的路径假设）**：脚本原来按
> `<export>/_runtime/.checkpoints/<id>` 找图，而 N27 把平台状态搬成了树根的**兄弟**
> （`<export>/state/_runtime/.checkpoints/<id>`，同一个挂载上的 `rename(2)` ⇒ 旧路径**不存在**）
> ⇒ 照旧写法读到的是 `No such file or directory`，与"图根本没写"**字面不可分**（就是上面
> ①/② 那类假红）。现在脚本按 worker 清单里的 `E2B_STATE_BASE` 算（缺省回退
> `E2B_WORKSPACE_BASE`，再缺省回退导出根），并在任何捕获之前先 `test -d` 断一次——
> `{"step": "layout"}` 就是这一条。同一轮补了三条前置断言（`E2B_PAUSE_CHECKPOINT=1` /
> `E2B_REAL_ROOT=1` / `E2B_PLATFORM_DISK_MB=8192`）：开关没开时，失败原因不是引擎。

**重建链条（改了 fork 就要从第一步走）**：`deploy/scripts/build-sandlock-wheels.sh`
（交叉编两个 arch 的 wheel + supervise + restore-stub，约 4 分钟）→ `deploy/scripts/build-and-push.sh`
（镜像推 ACR，层缓存命中时 1 分钟）→ `KUBECONFIG=... deploy/k8s-k0s/apply.sh`（滚两台 worker +
预热 base image，约 3–5 分钟）。

## 10. 空闲判定补采样（CPU）的上线记录（2026-09-25）—— 历史记录（该版本当天的验收）

> **这是历史记录**：本节记的是 CPU 采样上线当天的实测，**不是当前部署状态**
> （当前版本与形态开关见 §7，最近一次发版见 §12）。

**版本**：`0.1.0-527-g946daa9-20260925-215057`（= **当轮** `deploy/stack/.version`），整栈同一版本
（`kubectl diff` 只剩版本行）。这一轮**只改 worker 侧代码**（主仓 `946daa9`：新模块
`envd_service/runtime/cpu_activity.py` + `agent.py` 里一条独立采样循环），fork 没动 ⇒ 链条
只有 **镜像 → `apply.sh`** 两跳（`build-sandlock-wheels.sh` 不必跑）。**没有新环境变量**：
采样默认就开（`E2B_CPU_ACTIVITY_INTERVAL_S` 默认 5 s，0 = 关；`E2B_CPU_ACTIVITY_PERCENT`
默认 5），`E2B_CPU_TRACE=1` 只是排障用的每轮摘要，线上**没开**。控制面一行没改 —— 这条信号
走的是既有的 `sandboxActivity` 心跳。

**验收**：`deploy/scripts/acceptance/cpu_activity_acceptance.py` 全绿（两个沙箱、两段 45 s 静默窗口；
两组时间戳见 `docs/resource-contention.md` §6 的表）。

**这次量出来的两件事（以后验收任何"活动/空闲"类功能都要记得）**：

1. **`GET /sandboxes` 的 `lastActiveAt` 读的是共享 store（Redis），不是控制面内存**。
   `Registry.mark_active` 只在内存里前进，最多每 `E2B_ACTIVITY_PERSIST_INTERVAL_S`（默认 **30 s**，
   本集群**未设** ⇒ 30 s）才 `save()` 穿透一次（`control_plane/registry/manager.py::mark_active`）。
   ⇒ **验收窗口必须长于这个间隔**：第一版脚本用 20 s 窗口，连"跑一条命令"都没能让
   `lastActiveAt` 动，差点把一条好功能判死；而且窗口内**首次请求本身也是活动**，所以断言要
   放在第二段完全静默的窗口里，否则证不出是采样干的。
   ⚠️ 同一条滞后又回到了驱逐：`eviction_candidates` 也走 `list()`（= store），所以驱逐看到的
   活动时间最坏同样落后 30 s。默认空闲阈值 300 s 之下占 10%，可接受；**调小空闲阈值时
   要一并算进去**。
2. **读列表不算活动**：`GET /sandboxes` 是纯读，轮询它既不制造也不掩盖信号 —— 这让它适合当
   观测手段，但"窗口里没有别的请求"这件事得由脚本自己保证。

**怎么再跑一遍**（约 100 s）：

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d)
.venv/bin/python deploy/scripts/acceptance/cpu_activity_acceptance.py
```

## 11. N27（平台状态另起 `state base`）的上线记录与集群验收（2026-09-26）—— 历史记录（该版本当天的验收）

> **这是历史记录**：本节记的是 N27 上线当天的实测与集群验收，**不是当前部署状态**
> （当前版本与形态开关见 §7，最近一次发版见 §12）。§11.4 里的命令路径已按判据入口的
> 搬迁改指 `deploy/scripts/acceptance/`，改的是状态与事实。

**版本**：`0.1.0-597-g3701a53-20260926-163057`（= **当轮** `deploy/stack/.version`；今天已不是，
见 §7/§12）。整栈同一版本 ——
`autoscaler` / `control-plane` / `e2b-worker` 三个工作负载的镜像都是这一版（实测）。布局（树根下沉一级、
平台状态成为同挂载的兄弟目录）见 `docs/k8s-deployment.md` §23；迁移窗口的执行记录见
`.superpowers/sdd/progress.md` 的「N27 迁移已执行 + 已上线」段（`done=12 unknown=0`、逐条
`same_inode=yes`、journal 0600）。

### 11.1 卷上现状（2026-09-26 只读复核，`e2b-worker-0`）

```
/var/lib/e2b-sandboxes             1777  _builds _images _secrets _snapshots _templates _volumes state workspaces
/var/lib/e2b-sandboxes/state       1777  .route-b  .state-base-migration.journal(0600,585B)  .uid_pool.lock  _runtime
/var/lib/e2b-sandboxes/workspaces  1777  _migrate + 7 棵 <id> 树
```

* 顶层**没有** `_runtime` / `.route-b`：迁移是同挂载 `rename(2)` ⇒ 旧路径**不再存在**（不是"留了一份副本"）。
  回退窗口 = `state/.state-base-migration.journal`（0600）+ `migrate-state-base.sh --rollback --apply` 反向改名。
* 没有 migrate Job / ConfigMap 残留；`statefulset/e2b-worker` = **2/2 ready**；两个 control-plane 副本启动都打印
  `workspace base = /var/lib/e2b-sandboxes/workspaces` / `platform state base = /var/lib/e2b-sandboxes/state`。
* 证据：`tmp/k0s/n27-t7-cluster-state.log`、`n27-t7-cluster-volume.log`、`n27-t7-cluster-layout.log`。

### 11.2 形态无关性：两种形态 + 一条反例（探针 `deploy/scripts/acceptance/probe_state_base_visibility.py`）

两条判据，都在沙箱内跑：① `stat` 四个平台状态路径（`<base>`、`<base>/_runtime`、`<base>/.route-b`、
`<base>/_runtime/.checkpoints`）必须**全失败**且 errno ∈ {`ENOENT`, `EACCES`}（N15 之后 pure 形态是中介的
策略拒绝，不是 ENOENT，所以只认 ENOENT 的探针会只在一个形态上通过）；② 从 `cwd` 到 `/` 的**每一层**
要么列不出来、要么列出来**不含** `state` / `_runtime` / `.route-b` / `_secrets` **以及 `<state base>` 的
basename**（2026-09-27 起：chain 半边跟着 `--state-base` 走，探针会把这一轮真正在守的名字打成
`CHECKER-WATCHED`；换基名即失明的那版见本节末）。退出码 `0`=成立 / `1`=不成立 /
`2`=VACUOUS。两条判据各带**正对照**（沙箱自己写的 canary 必须能 `stat` 到、workspace 那一层必须列出它），
所以"到处都拒"不会被读成"干净"；反例是"迁移前布局"（平台状态就在树根上），它必须报 `1`。

| 形态 | 怎么跑 | ① `stat` ×4 | ② 祖先链 | 退出码 |
|---|---|---|---|---|
| **image-rootfs（生产）** | `probe … cluster`（真集群、真 `Sandbox`） | `ENOENT` ×4 | 3 层（`/home/user` → `/home` → 沙箱自己的 `/`），无泄漏 | **0** |
| **pure + 合成根 + 真根**（N16） | `probe … lane --shape synth-realroot --layout n27` | `ENOENT` ×4 | 3 层（合成根），无泄漏 | **0** |
| **pure + identity（无根，N15；2026-09-27 前是默认，现在是退回杆）** | `probe … lane --shape identity --layout n27` | `EACCES` ×4（**读不到 ✔**） | ✘ 在 `<export>` 一层列出 `["_secrets", "state"]` | **1** |
| 对照：**迁移前布局** | `probe … lane --shape identity --layout legacy` | 状态目录**本身可 `stat`**（该层还列出 `_runtime` / `.route-b` / `_secrets`） | ✘ | **1** |
| pure + 合成根 + 模拟根 | `probe … lane --shape synth-emulated --layout n27` | — | — | **2**（`LANE VACUOUS`）：起不来（`SlotRefusal: instance is closed`，checker 一次都没跑到）⇒ 形态不可服务 —— N16 守卫要求 `E2B_PURE_ROOTFS=synth` 必须配 `E2B_REAL_ROOT=1`；**2026-09-27 之前这一档是 traceback + `exit 1`**（与"反例成立"同一个退出码），现已由探针改成 VACUOUS |

**结论（形态无关性的准确边界）**：**有根的形态**（生产 image-rootfs、pure+合成根+真根）两条判据都成立 ——
平台状态**既不在祖先链上、也读不到**；**无根的 identity 形态只成立一半**：四次 `stat` 全 `EACCES`（读不到 ✔），
但那条形态的"祖先链"**就是宿主路径链**（中介必须放行 workspace 的祖先目录），于是 `../..`（= `<export>`）能列出
`state` 与 `_secrets` 的**名字**（内容仍不可达 —— 对它们 `stat` 就是 `EACCES`）。这条残差不是 N27 引入的
（迁移前同一条形态在 `..` 一层就列出 `_runtime` 等），它是"没有根"这件事本身，也就是
`docs/superpowers/plans/2026-09-26-pure-shape-synthetic-rootfs.md`（N16 合成根）要消掉的那条。
⇒ **`docs/pure-shape-decision.md` §2、`docs/open-issues.md`、`docs/task-backlog.md` 里"pure 形态也不在祖先链上"
这句要按本表限定为"有根形态"**（Task 8 写它时没有实跑，见 `.superpowers/sdd/n27-task-7-report.md`）。

**2026-09-27 复核（同一探针、三档重跑；原始输出 `tmp/k0s/n27resid-{identity-n27,synth-realroot-n27,identity-legacy}.log`）**：结论逐字未变，并把"要让**默认**形态也消掉该做什么"补齐 ——

- `--shape synth-realroot --layout n27`（`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT=1`）⇒ `exit 0`，`stat=PASS`（`ENOENT` ×4）、`chain=PASS`（3 层 = 沙箱自己的合成根；`<export>` **根本不在链上**）⇒ **N16 已消掉"能列出名字"**。
- `--shape identity --layout n27`（`E2B_PURE_ROOTFS=off` —— **切换前是默认档**，2026-09-27 起它只是退回杆）⇒ `exit 1`，`stat=PASS`（`EACCES` ×4 —— **不是** `ENOENT`）、`chain=FAIL`，`LEAK ["_secrets", "state"]` ⇒ **残差仍在，但不再属于默认形态**（就是本表的 identity 那一行）。
- `--shape identity --layout legacy`（反例档）⇒ `exit 1`，`stat=FAIL`（状态目录**本身**可 `stat`：`OK mode=0o40755`）、`chain=FAIL`，`LEAK [".route-b", "_runtime", "_secrets"]` ⇒ **判据不是恒真的空检查**。

**默认档已经切了（2026-09-27 用户裁定，`098ba10`）：`E2B_PURE_ROOTFS` 默认从 `off` 切到 `synth`。** 所以现在**默认**的 pure 形态走本表第二行（`chain=PASS`），本表第三行的 identity 档降级成**显式退回杆**（`E2B_PURE_ROOTFS=off`）—— 它的残差不再属于默认形态。**2026-09-28 复核（默认档真的跑了，不是推断；日志 `tmp/n27-default-synth-lane.log` / `tmp/n27-off-identity-lane.log`）**：不设任何键（= 产品默认）时 `route_b_active=True has_root=True chroot=/tmp/…-pure-rootfs/sbx_slot_0`、`stat=PASS`（`ENOENT` ×4）、`chain=PASS`（3 层 = 合成根自己）、`exit 0`；退回杆 `E2B_PURE_ROOTFS=off` 时 `has_root=False chroot=/`、`chain=FAIL` + `LEAK ["_secrets", "state"]`、`exit 1` ⇒ **默认档的残差已消**，而判据在退回杆上仍可翻红。**切默认那天暴露的探针 bug 也在这次复核里修掉**：`lane` 会把脚本拷进沙箱当 `/home/user/n27-checker.py` 再跑 `in-sandbox`，而 `--scratch` 的默认值在解析期就去算 `parents[3]` —— 浅路径上 `IndexError: 3`，lane 报 `FAIL lane: expected exactly one VERDICT and one EXIT line, got 0 and 0`（就是默认档第一次跑出来的样子）；现在默认值只在 `lane` 里惰性求值，pin 见 `tests/unit/test_n27_probe_cli.py`。切换的三条代价/风险 —— ① pure 形态**每沙箱一份骨架目录**（`<workspace base>/_pure_rootfs/<id>`：普通目录 + bind 系统目录 + 整棵 `/dev` + `pivot_root`；`gateway_common.paths.PURE_ROOTFS_DIR_NAME`）；② **依赖 `E2B_REAL_ROOT=1`** —— `synth` 配 `REAL_ROOT=0` 结构性不成立（本轮 `--shape synth-emulated` 实测起不来，按 VACUOUS `exit 2` 报；N16 的成对守卫还会在 worker 启动时 loud 拒）；③ **依赖 worker seccomp 档已应用**（`mount/umount2/pivot_root` 无门闩），漏了会被 worker 启动自检当场拒（见 N16/N35）。生产两条清单都设 `E2B_BASE_IMAGE` ⇒ 生产是 image-rootfs 形态、不受这次切换影响，受影响的只有 pure 部署。

**探针自身两处假闸本轮一并修掉**（RED→GREEN 见 `.superpowers/sdd/n27-identity-residual-report.md`）：**(a)** ② 的 chain 半边只认四条硬编码名字 ⇒ 一旦 `E2B_STATE_BASE` 的 basename 不在那四条里（例：换名成 `platform`）就**失明** —— `<export>` 照旧列着那个名字，探针却报 `chain=PASS`；现在 chain 半边跟着 `--state-base` 走，并把这一轮真正在守的名字打成 `CHECKER-WATCHED`。**(b)** lane 在 checker 跑起来**之前**崩掉也走 `exit 1`，与"反例成立"同一个退出码 ⇒ 崩溃会被读成反例；现在报 `LANE VACUOUS` + `exit 2`。

机制旁证（"祖先"而不是"随便一个目录"）：identity 形态下 workspace 的父目录可列（`ls -a /tmp/n27-mount` `rc=0`），
同一个挂载里**不是祖先**的 `/workspace/...` 一律 `Permission denied` —— `tmp/k0s/n27-t7-lane-mount.log`。

### 11.3 不回归（两条冒烟，凭据只从 Secret 取、不打印）

| 冒烟 | 结果 | 日志 |
|---|---|---|
| `deploy/scripts/multinode_smoke.py` | 两个 worker 各落 2 个沙箱（`NODE DISTRIBUTION` 两个节点都在），commands / files / health / stdin 全过，kill 后两边预约归零 ⇒ `MULTI-NODE SMOKE OK` | `tmp/k0s/n27-t7-smoke-multinode.log` |
| `deploy/scripts/deployment_smoke.py` | 命令/文件、跨节点迁移保文件、网络配置、远端卷、**template build → registry push → worker pull → image rootfs**、MCP gateway 全过 ⇒ `DEPLOYMENT SMOKE OK` | `tmp/k0s/n27-t7-smoke-deployment.log` |

### 11.4 怎么再跑一遍

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
deploy/scripts/open-cluster-tunnel.sh                       # 建通道 + 自检 2 节点 arm64
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
tmp/testenv/bin/python deploy/scripts/acceptance/probe_state_base_visibility.py cluster
tmp/testenv/bin/python deploy/scripts/multinode_smoke.py
tmp/testenv/bin/python deploy/scripts/deployment_smoke.py

# 形态对照（prod-shaped 测试容器；仓库挂在 /src，因为 identity 形态拒 /workspace）
sh deploy/scripts/acceptance/n27-t7-lane.sh python3 -u deploy/scripts/acceptance/probe_state_base_visibility.py lane --shape identity     --layout n27
sh deploy/scripts/acceptance/n27-t7-lane.sh python3 -u deploy/scripts/acceptance/probe_state_base_visibility.py lane --shape identity     --layout legacy
sh deploy/scripts/acceptance/n27-t7-lane.sh python3 -u deploy/scripts/acceptance/probe_state_base_visibility.py lane --shape synth-realroot --layout n27
```

（lane 容器是 `e2b-sandlock-test:latest`（amd64），caps 与 seccomp 档同
`deploy/scripts/arm-lane/x86-security.sh`；`E2B_BASE_IMAGE` 必须**显式传空**，`${VAR:-default}` 会把它换成默认值。）

## 12. 2026-09-27 发版：`0.1.0-652-g43fb88a-20260927-102733`

> **当前部署状态的权威表在 §7**（版本、pod、形态开关都以 §7 为准）；本节只记这次发版当时
> 做了什么、验收数字是多少。§7 与本节若有重复，以 §7 为准、到这里来查发版细节。

**为什么发**：`main` 领先上一版（`0.1.0-597-g3701a53-20260926-163057`）**55 个提交**，其中三条只在
镜像里生效，线上不滚就一直是旧行为：

* **N37** `8253ad6`：envd 的 **process 流从不发 SDK 要的 in-band `KeepAlive`**（filesystem watch 每 15 s 发、
  process 没有）⇒ 边缘对**静默 60.0 s** 的响应体做空闲切断，表现为"单条命令写 4 千个文件就断流"。
  修完线上单命令 4000 文件 **3/3 通过**（见下表）。
* **N41** `f6d35d4`：归还预留变成 store 侧的**一次 `WATCH/MULTI` 事务**（标记 + 账本 DECR 同批）。
* **N45** `e2e5f1a`：`E2B_PID_NS` 补齐到每一个 worker 栈（池里沙箱此前与 worker 共 pid ns）。

（同一批里还有 FUP-28 撤掉 `..` 相对软链改写、N44 的基镜像对齐、N43 的 `du`/`tar`/`find` 回归修复。）

**构建与上线**

```bash
./deploy/scripts/build-and-push.sh                      # 版本戳自动写 deploy/stack/.version
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
deploy/k8s-k0s/apply.sh                                 # 渲染 + apply + 预热 base image + 等滚动
```

* 版本：`0.1.0-652-g43fb88a-20260927-102733`（= `deploy/stack/.version`）。
* `apply.sh`：**7 个镜像引用已 pin**；`e2b-worker` StatefulSet 滚完（2/2）、`control-plane`/`autoscaler`
  `configured`、`redis` `unchanged`；base image 仍是清单里那条
  `python-mcp:3.14@sha256:3675662d…`（两个 worker 的 `peek` 都 `cached=true`、`warmed=skipped`）。
  ⚠️ 这次构建把 ACR 上的 **mirror tag** `byteplan/python-mcp:3.14` 重推成了新 digest
  `sha256:4474e78f…`；**清单里的 digest pin 没动**，所以线上仍跑原来那条 —— 别把"tag 的 digest 变了"
  读成"线上基镜像换了"。**（2026-09-27 复核：pin 确实没动 —— `deploy/k8s/worker.yaml` 与
  `deploy/k8s/control-plane.yaml` 的 `E2B_BASE_IMAGE` 仍是 `…python-mcp:3.14@sha256:3675662d…`，
  `deploy/k8s*` 里只有这两处 `E2B_BASE_IMAGE` 声明、digest 一致；运行中的 `e2b-worker-0` 容器
  `env` 也逐字相同。
  mirror tag `byteplan/python-mcp:3.14` 当前在 ACR 上指向哪个 digest 需要 ACR 凭据才能复核，
  本仓库内不复核 —— 但 pin 与线上都还是 `3675662d…` 这一条。）**
* 上线后 `kubectl diff`（`DRY_RUN=1 apply.sh` 渲染的整栈 vs 线上）**0 行差异** ⇒ 仓库规格与线上一致。

**验收（全部在 `0.1.0-652` 上跑）**

| 判据 | 结果 | 证据 |
|---|---|---|
| 预检两档 lane（冻结 HEAD `43fb88a`，发布前） | gate A `2060 passed / 10 skipped / 3 xfailed / 0 failed`、gate B `2053 / 17 / 3 / 0`；对上一基线各 **+33 passed** 且逐条归因（无删除、skip/xfail 逐字不变） | `tmp/k0s/preflight-gate{A,B}.log` |
| 仓库规格 ≡ 线上 | `kubectl diff` **0 行**（渲染 1024 行 / 7 处 pin） | `tmp/k0s/apply-render2.yaml`、`tmp/k0s/apply-diff2.txt` |
| `multinode_smoke.py` | 两 worker 各 2 个沙箱；commands / files / health / stdin 全过；kill 后两边预约 0 ⇒ `MULTI-NODE SMOKE OK` | 见下一行的日志 |
| `deployment_smoke.py` | 命令+文件、跨节点迁移保文件、网络配置 echo+原子更新、远端卷+兄弟卷隔离、**模板构建→registry push→worker pull→image rootfs**、MCP gateway 全过 ⇒ `DEPLOYMENT SMOKE OK` | `tmp/k0s/release-652-acceptance.log` |
| **N37 判据**（单命令写 4000 文件，×3） | **两轮各 3/3**（首轮 `92.9/94.2/93.5 s`、落盘那轮 `92.5/93.6/93.2 s`），每轮 `files on disk=4000`；修前同形状在 **61.4 s** 就断 | 同上（`grep 'run [0-9]: OK'`） |
| **N42 判据**（`allowInternetAccess=True`） | `pypi.org:443 CONNECTED` + `TLSv1.3`；裸 IP `104.20.23.154:443` 按策略 `ConnectionRefusedError 111`（对照说明不是"网络全开"） | 同上 |
| checkpoint 端到端 | pause → 图落 `state/_runtime/.checkpoints/<id>/latest` → **换掉宿主 worker pod** → resume → 计数 `3→4` → `exec_after_resume EXEC_OK` → 图被消费 ⇒ `{"step":"OK"}` | `deploy/scripts/checkpoint_acceptance.py`（输出见本节文字） |
| 配额账本（N41 的副作用面） | 上述全部跑完后 `GET /internal/nodes`：两节点 `reservedMemoryMB=0`、`reservedDiskMB=0` | 同上 |

**怎么再跑一遍**：通道与两个 key 同 §11.4，把最后三行换成
`.venv/bin/python deploy/scripts/acceptance/cluster_run.py --files 4000 --runs 3`、
`.venv/bin/python deploy/scripts/acceptance/n42-egress-probe.py`、
`.venv/bin/python deploy/scripts/checkpoint_acceptance.py`（后者**会删宿主 worker 的 pod**，
脚本自带"这台 worker 上还有别人的沙箱就拒绝"的礼貌检查，`--force` 是唯一出口）。

> 本节的日志都在 `tmp/`（gitignored，会被清）。数字要复核就按上面这几行自己跑一遍 ——
> 上一轮踩过的坑是"文档里嵌了一份会飘的数字表"，所以这里连判据命令一起给，别只信表格。

## 13. 2026-09-27 第二次发版：`0.1.0-664-gdf5eec5-20260927-150255`

**为什么发**：当天第二批裁定（用户"都做了吧"）里有两件只在镜像里生效，另外两件是纯形态与文档：

* **N46**（`c5acc04` + 接线）：未命名异步拷贝现在**持有租约**（值 = owner 的租约令牌，TTL 30 s、
  每 10 s 续租），`reconcile_pending_snapshots` 由 `snapshot_reconcile_loop` **每 10 s** 跑（单飞），
  于是"有主在拷"与"孤儿"可区分；孤儿 settle 上界 ≈ 最后续租 + 40 s。
* **E6/E7**（`4396915`）：`E2B_PAUSED_TTL_S`（默认 0 = 不启用）与
  `E2B_PLATFORM_LEDGER_ALERT_RATIO`（默认 0.8，进入/退出各一条 WARNING）。
* 不在车队生效的：**pure 默认根 `off`→`synth`**（`098ba10`；生产是 image-rootfs 形态，零变化）
  与三处"提升后指错一级"的路径修复。

**构建与上线**：`./deploy/scripts/build-and-push.sh`（层缓存命中，约 1 分钟）→
`KUBECONFIG=… deploy/k8s-k0s/apply.sh`（**7 个镜像引用已 pin**；worker StatefulSet 滚完、
control-plane/autoscaler `configured`；base image 仍 `…@sha256:3675662d…`、两 worker `cached=true`）。
上线后仓库渲染 vs 线上 `kubectl diff` 为 **0 行**。

| 判据 | 结果 | 证据 |
|---|---|---|
| `deployment_smoke.py` | 命令+文件、跨节点迁移保文件、网络配置、远端卷+兄弟卷隔离、模板构建→registry→worker→rootfs、MCP gateway 全过；kill 后两节点预约 0 | `tmp/k0s/release-664-acceptance.log` |
| `multinode_smoke.py` | 两 worker 各 2 个沙箱、commands/files/health/stdin 全过、预约 0 | 同上 |
| **N46 的可见签名**（`deploy/scripts/acceptance/n46-copy-lease-probe.py`） | 未命名异步快照：`202 creating` → 拷贝期间 `GET e2b:snapshot:copy:<id>` **有值**（= owner 的租约令牌，**修前这张键从不出现**）→ 终态 `completed` → 键**已释放** ⇒ `N46 LEASE PROBE OK` | 同上 |

> 未在本版复跑的两条：N37 的 4000 文件判据与 checkpoint 端到端 —— 它们昨天在 `0.1.0-652` 上全绿
> （§12），而本版改的是控制面的快照/暂停路径，不碰 process 流与根形态；要复核按 §12 的命令跑。
> E6/E7 的默认值是"关/0.8 阈值"，线上没有可观测行为（不删东西、未越限不打日志），
> 它们的判据在仓库里是确定性用例（`4396915`）。
