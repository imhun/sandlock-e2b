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

> **（2026-09-30，仓库侧变更；集群按这份实测照旧）**：`autoscaler` 这个 Deployment
> **在仓库里已经不存在了** —— 扩缩容循环被 control-plane 收进去（`E2B_AS_ENABLED=true`，
> 见 `docs/open-issues.md` N50 与 `docs/SCALING.md` §6.4）。所以本文里凡是"实测到
> `autoscaler 1/1`"的记录都对，但**下一次 `deploy/k8s-k0s/apply.sh` 之后**，这里会少一个
> Deployment、`control-plane` 会多一份 RBAC（scale/evict）；在那之前集群仍是旧形态。
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

## 7. 当前部署状态（**最近一次发版：见 §7.34（2026-10-03，N57/N60/N61/N62/N63 收口，当前版本 `0.1.0-931-g4181bb4-20261003-013625`；四条收尾读数 + 两条冒烟全绿，两次非预期读数与操作教训见该节）；上一版：§7.33（2026-10-02，建箱存储本地优先 **Task 0–5 的发版记录**，版本 `0.1.0-915-gfb8a74b-20261002-211709`；跨切面冒烟**先红后绿**（红灯那次：验收窗口留下的孤儿沙箱**真的占着名额**，再加上拒绝路径漏掉的那几份预约）—— 迁移"源节点不可达"拒绝路径泄漏目标节点配额，见该节与 N59；控制者清掉台账与 8 个孤儿沙箱之后**两条冒烟都通过**，N59 的修复已随本版 `32f3667` 上线）；上一版的细节：见 §7.31（2026-10-02，N57/Task 4：本节点 state 分家，`prepare` 72–76 → **7.4 ms**，Task 5 的目录链修复同船）；§7.30（2026-10-02，Task 2：快照载荷从爆炸式 `fs/` 目录改成 `fs.tar`，捕获每条目 39.7 → 9.0 ms、占块减半，从快照建箱 28.2 → 34.4 ms/条目（读侧每条目包含检查，Task 3 搬本地后消失；`942c5bd` 的守卫修复在 `0.1.0-895` 上线后重测 **30.4 ms/条目**），**该节版本 `0.1.0-900-g0079c84-20261002-161409`**；老 `fs/` 快照 4/4 仍可恢复，详见该节）；§7.29（2026-10-02，N58：根重切上线 —— `_snapshots` 合一、`_migrate` 上浮，建箱 p50 124 → 135/136 ms、逐段不变，**该节版本 `0.1.0-887-g7ef319b-20261002-100406`**；该次上线当场抓到"从快照建箱"的 502，已前滚修复 + 复验，详见该节）；§7.28（2026-10-01，N56 收尾：两跳并发 (b)，建箱 p50 193 → 127–130 ms，**该节版本 `0.1.0-877-ge15b77f-20261001-231822`**；该次上线中间出过一次"每次建箱都 409"的全站故障，已回滚 + 修复，详见该节）**；§7.27 是载体 C（建箱材料化改走控制面直送，p50 191 → 193 ms，版本 `0.1.0-864-g3377ffc-20261001-204142`）；§7.26（2026-10-01，N55：建箱里三处白付往返 —— 记录读、file-op 连接、控制面派发 —— 加一个逐段计时开关，建箱 p50 228 ms → 191 ms，该节版本 `0.1.0-841-g0d7dc76-20261001-131547`）**；§7.25 是 N54：镜像 digest 解析结果落盘缓存（建箱 p50 0.66 s → 0.23 s，版本 `0.1.0-839-g4271c45-20261001-112409`）；§7.24 是 N53：worker 丢掉"控制面不认的"运行时记录 + TTL 拆除顺序 + 历史残留清理，版本 `0.1.0-836-g6d7532b-20261001-102113`；§7.23 是沙箱第一档 syscall 加固 + clone3 命名空间位，版本 `0.1.0-824-gf2aec0b-20261001-073534`；§7.22 是 ① 第二步：删掉 worker 侧 file-capability 形态的残留，版本 `0.1.0-818-g7205fba-20260930-221244`；§7.21 是 ① 第一步：exec/socket 传输具名拒绝，版本 `0.1.0-816-g1c85e7c-20260930-213813`；§7.20 是回退杆清理：删 `E2B_AS_K8S_KIND` 与 `spawn`，版本 `0.1.0-814-gf8d1685-20260930-210628`；§7.19 是 C3 出厂形态收尾：删 C1 死代码 + slot 身份默认按形态解析，版本 `0.1.0-811-g071beb4-20260930-202337`；§7.18 是 N51 缩容目标修正、§7.17 是 autoscaler 并入控制面 + 本地池退役、§7.16 是 quota-agent 搬到顶层 `quota_agent/`（`deploy/` 从此不含任何 Python 包）、§7.15 是 `priv` 的 C 源码跟进搬去 `c3_agent/priv/`、§7.14 是 C3 agent 代码搬去顶层 `c3_agent/`、§7.13 是同一轮的 `Template.build` mirror 链路修复、§7.12 是 compose 车道评审的两条回归、§7.11 是同一轮的三条缺口收口、§7.10 是 C3 收口评审、§7.9 是 C3 Task 7 上线，下面 §7.1–§7.8 是历史记录）

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
INFO:c3_agent.scan:c3-agent inventory: node=c3-agent scanned=0 protected=0 orphans=0 removed=0 failed=0 deferred=-
```

手工放一棵沙箱形状的孤儿树（`/var/lib/e2b-sandboxes/sbx_<32 hex>/`，65534）后下一轮：
`scanned=1 protected=0 orphans=1 removed=1 failed=0 deferred=-`，树消失 —— "CP 决策 + agent 执行"
整条链路真跑过。

⚠ **顺带量到并修掉的两个真缺陷**（详见 §11.2.1 第 14/15 条）：compose 的巡检上报原来被源 IP
因子整条拒（面 B 上报、期望值却只取面 A 的地址 ⇒ 每轮 403），以及 agent 入口不放开 INFO ⇒
**成功**轮次那一行看不见。前者是"给 Redis 就活了"这个前提本身不成立的原因，两者都已在同一分支修掉。

**测试与验收**：本树 unit 相关文件 423 passed（C3/worker/compose/deploy 一组，与报告 §3.6 同一次运行）、
`tests/contract/test_c3_worker_kernel_identity.py` 8 passed（真容器）；
`multinode_smoke.py` = `MULTI-NODE SMOKE OK`（4 箱 2+1+1，命令/文件/stdin 过网关、kill 后预约归 0）；
判据 13 `JUDGMENT 13: all assertions passed`、判据 16 `JUDGMENT 16: all assertions passed`
（并发 in-flight 3 vs 反臂 1）。全部命令与原始输出见
`.superpowers/sdd/c3-compose-gaps-report.md`。

### 7.12 compose 车道评审：CP 变 65534 带出的两条回归（**2026-09-30，已修；只在本地栈实测**）

§7.11 的 CP-uid 改动在**两个我上一轮没有起过的栈**上带出两条回归；这一节是它们的现场读数。
两条都在 `deploy/stack` 形态（`-p c3stack`，镜像同样来自本树 `:c3-gaps`）上量的，因为那正是
有 buildkit、也挂了 `./tls` 的那个栈。

**① `deploy/stack` 的控制面读不到 buildkit 的 unix socket**

rootless buildkitd 把 socket 建在 `buildkit-data` 卷里（`srw-rw---- 1000:1000`，builder 服务
用它镜像自己的 uid 1000），而控制面是 65534、没有那 1000 组位；k8s 那份靠 **pod 级
`fsGroup: 1000`** 拿到（§13.6 的 B5），compose 没有等价物 ⇒ `Template.build` 在这条车道上
直接坏掉。修法是给该服务加 `group_add: ["1000"]`（只在这一个栈上加，另外两个栈没有 builder）。

```console
$ docker exec c3stack-control-plane-1 sh -c 'id; stat -c "%n %F %a %u:%g" /run/buildkit/buildkitd.sock'
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup),1000
/run/buildkit/buildkitd.sock socket 660 1000:1000
$ docker exec c3stack-control-plane-1 sh -c 'grep ^Groups /proc/self/status'
Groups:	1000 65534
$ docker exec c3stack-control-plane-1 buildctl --addr unix:///run/buildkit/buildkitd.sock debug workers
ID                            PLATFORMS
qaaf72gm7s2a34gq2xdk2lb0u     linux/amd64,linux/amd64/v2,…            # 控制面自己的 buildctl 通了
$ # 反臂：同一个 buildctl，换成"我上一轮发出的那个形态"（uid 65534、没有那组）
$ docker run --rm -u 65534:65534 -v c3stack_buildkit-data:/run/buildkit:ro \
    --entrypoint buildctl e2b-sandlock-control-plane-gateway:c3-gaps \
    --addr unix:///run/buildkit/buildkitd.sock debug workers
error: failed to list workers: Unavailable: connection error: desc = "transport: Error while
dialing: dial unix /run/buildkit/buildkitd.sock: connect: permission denied"
```

（`docker exec -u 65534:65534` **不能**当反臂：exec 会带上容器的 `GroupAdd`，`Groups: 1000 65534`、
照样能连 —— 所以反臂用 `docker run -u 65534:65534` 且**不加** `--group-add` 重建那个形态。）

**② 文档里的 TLS 配方现在会把控制面打死**：`gen-tls-cert.sh` 写 `0600` 的 key，而
`deploy/stack`/`deploy/compose prod` 把 `./tls` 只读挂给 uid 65534 的 CP —— 它读不了不是自己的
`0600` 文件。（bind mount 的属主在**原生 Linux** 上是宿主 uid；OrbStack 会把宿主 bind mount
呈现成"容器自己的 uid"，所以本机要用**卷**来复现那个属主关系，见下面 `c3stack_tlsdata`。）

```console
$ docker run --rm -u 65534:65534 -v c3stack_tlsdata:/tls:ro alpine \
    sh -c 'stat -c "%n %a %u:%g" /tls/tls.key; head -c1 /tls/tls.key >/dev/null && echo readable || echo "NOT readable"'
/tls/tls.key 600 0:0
NOT readable by 65534
$ # 旧配方（root:root 0600）+ TLS 打开
$ docker inspect c3stack-control-plane-1 --format 'status={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}}'
status=restarting exit=1 restarts=6
$ docker logs c3stack-control-plane-1 | tail -3
  File "/usr/local/lib/python3.14/site-packages/uvicorn/config.py", line 129, in create_ssl_context
    ctx.load_cert_chain(certfile, keyfile, get_password)
PermissionError: [Errno 13] Permission denied
$ # 修好之后（生成器改成 0644，两个文件都是）
$ docker run --rm -v c3stack_tlsdata:/tls alpine chmod 644 /tls/tls.key
$ docker inspect c3stack-control-plane-1 --format 'status={{.State.Status}}'
status=running
$ docker logs c3stack-control-plane-1 | grep -i uvicorn | tail -1
INFO:     Uvicorn running on https://0.0.0.0:3000 (Press CTRL+C to quit)
$ curl -sS -k https://127.0.0.1:3400/healthz -w ' HTTP %{http_code}\n'
{"status":"ok"} HTTP 200
$ curl -sS http://127.0.0.1:3400/healthz -w ' HTTP %{http_code}\n'      # 同端口明文必须失败
HTTP 000
$ echo | openssl s_client -connect 127.0.0.1:3400 -servername control-plane 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName
subject=CN=localhost
X509v3 Subject Alternative Name: DNS:localhost, IP Address:127.0.0.1, IP Address:::1, DNS:control-plane
```

修法选了**改模式**而不是改属主/组：这是**本地自签验证**配方（脚本头部就这么写），跑它的运维
通常不是 root、`chown 65534` 做不到；而 k8s 那条同源交付走 `kubectl create secret tls`，Secret
在 pod 内的默认模式本来就是 `0644`。生产不用这个脚本（TLS 在入口终结，或用属主/组与 CP 一致的
证书）。⚠ 顺带记一条仍然成立的边界：这条车道把 TLS 打开之后，**worker 还要信任那张自签 CA**
（`E2B_CONTROL_PLANE_URL` 也得改成 `https://`），那是配方注释里早就点明的一步，本轮没有替它做。

**同批的另外三件（评审的 minor）**：`image-cache-init` 补上 k8s `storage-init` 的那套能力集
（`drop: [ALL]` + `CHOWN/DAC_OVERRIDE/FOWNER`；实测 `0xb` 够、`0x9` 在 `mkdir -p <65534-owned>/_oci`
就 `Permission denied`）；缓存交棒的非递归形补上钉子（行为臂看不到它）；两处会误导的注释
（"k8s **control-plane pod** 的 init"其实是 **agent DaemonSet** 的；"Everything it needs to own"
过宽 —— `state/`、`_runtime`、`.route-b` 是 CP 以根属主身份在可写根**之下**建的）。

### 7.13 k0s 上线：`Template.build` 的 mirror 链路被 buildctl 的 `$HOME` 打死（**2026-09-30，已上线**）

这一节是 §7.9 那轮冒烟末段留下的"环境问题"的真身 —— **它不是环境问题，也不是"没配镜像源"**。

**现象**：`deployment_smoke.py` 的 `Template.build` 段失败，buildkit 侧（CP pod 的 `buildkit`
sidecar）日志是

```
level=info msg="trying next host" error="mkdir /nonexistent: permission denied" span="resolving docker.io/library/python:3.11-slim"
level=info msg="trying next host" error="mkdir /nonexistent: permission denied" span="resolving docker.io/library/python:3.11-slim"
level=info msg="fetch failed" error="failed to do request: Head \"https://registry-1.docker.io/v2/library/python/manifests/3.11-slim\": dial tcp 74.86.228.110:443: i/o timeout"
```

两句 `trying next host` 正好是两个 mirror（`docker.m.daocloud.io`、`docker.1ms.run`）——**它们在发出
第一个请求之前就死了**，构建只是回落到 origin `registry-1.docker.io` 再超时（集群侧把 docker.io
解析到 `2a03:2880:…` / `74.86.228.110`，没有出口）。现场读起来像"镜像源没配"，实际是"镜像源
一次都没轮到"。集群侧连通性复核：从 CP 容器内 `docker.m.daocloud.io`、`docker.1ms.run` 都是
0.01 s 通，`registry-1.docker.io` 超时。

**根因**：控制面容器以 uid 65534 运行，而这个镜像里 65534 是 Debian 的 `nobody`，家目录是
`/nonexistent`（容器内 `HOME=/nonexistent`、`id` 打 `uid=65534(nobody)`）。buildkit 的 auth
provider 跑在**客户端一侧**（`buildctl`），它给 token seed 找目录用的是 `docker/cli` 的
`config.Dir()`（`$DOCKER_CONFIG` → 否则 `$HOME/.docker`），而
`session/auth/authprovider/tokenseed.go::getSeed(host)` **每个 registry host 都会
`MkdirAll(dir)` 一次**。于是每个 host 都以 `mkdir /nonexistent: permission denied` 告终，这个错误
经 session 回传给 daemon，就打在 `trying next host` 那一行上。

**对照实验**（在**线上 CP 容器里**跑同一个 `buildctl`、同一份 Dockerfile，只差一个环境变量）：

```
$ kubectl -n sandlock exec <cp> -c control-plane -- sh -c 'HOME=/nonexistent buildctl --addr unix:///run/buildkit/buildkitd.sock build …'
error: failed to solve: mkdir /nonexistent: permission denied
$ kubectl -n sandlock exec <cp> -c control-plane -- sh -c 'HOME=/nonexistent DOCKER_CONFIG=/tmp/e2b-bkdcfg buildctl --addr unix:///run/buildkit/buildkitd.sock build …'
#5 exporting to image … #5 DONE 0.0s
$ ls -la /tmp/e2b-bkdcfg
-rw------- 1 nobody nogroup 74 .token_seed      # ← 建出来了；里面的 host 就是 mirror
```

**修法**（`control_plane/api/templates.py`，提交 `77edfa8`）：所有 `buildctl` 子进程显式带上
`DOCKER_CONFIG`（`_buildctl_env()`），`_write_docker_config()` 也写到同一个目录（它就是 push 凭据
要去的地方）；`DOCKER_CONFIG` 已设置时沿用。默认落在 `$TMPDIR/e2b-docker-config`（实测
`/tmp/e2b-docker-config`）——容器本地，**不落共享卷**，因为那里会写 registry 凭据。
pin：`test_docker_config_dir_never_needs_a_home`、`test_buildctl_build_is_handed_the_docker_config_dir`
（旧代码上红：`KeyError: 'env'`）。

**上线**：版本 **`0.1.0-792-g77edfa8-20260930-131220`**。
`PLATFORMS=linux/arm64 ./deploy/scripts/build-and-push.sh` 仍踩那条已知坑（单平台走
`build-images.sh` 的 `--load`、不推送），补推 worker/autoscaler/agent/quota-agent 四个 tag 并逐个
`docker manifest inspect` 核对；CP 镜像 `sha256:45bed84f…` 的 `COPY control_plane/` **没有命中缓存**，
新代码确实在镜像里。`./deploy/k8s-k0s/apply.sh`（走 overlay）→ 10 个 pod 全 Ready。

**判据**：

- `deploy/scripts/deployment_smoke.py` → **`DEPLOYMENT SMOKE OK`**，含
  `OK: template built -> registry push -> worker pull -> image rootfs`（§7.9 里失败的就是这一段）；
- `deploy/scripts/multinode_smoke.py` → **`MULTI-NODE SMOKE OK`**；
- 新 CP pod 的 buildkit 日志里 `mkdir /nonexistent` **0** 行、`trying next host` **0** 行；
- 直接指纹：出力的那个副本的 `/tmp/e2b-docker-config/.token_seed` 里唯一 host 是
  **`docker.m.daocloud.io`** —— mirror 这次真的被用上了，origin 一次都没到。

compose 车道是同一份代码、同一个镜像、同样 65534，`$HOME/.docker` 同样不可写（同一个 Debian
`nobody`），所以这条修复对两条车道一起生效，清单不用改。`deploy/stack/buildkitd.toml` 与
`deploy/k8s/buildkit.yaml` 的 mirror 段落各加了一句注释指向 `_docker_config_dir()`，免得下一个人
再按"没配镜像源"排查一遍。

### 7.14 C3 agent 代码搬到顶层 `c3_agent/`（**2026-09-30，已上线**）

口径澄清：**`deploy/` 只放部署配置与脚本**。C3 的节点 agent 是自带镜像的**服务**（有自己的
`Dockerfile.agent`、自己的 DaemonSet/两个 compose 服务、自己的 `CMD`），和 `control_plane/`、
`autoscaler/` 同类，所以按项目目录放。提交 `117846f`：

- `git mv deploy/c3_agent c3_agent`（8 个文件），模块名 `deploy.c3_agent` → `c3_agent`；
- `deploy/docker/Dockerfile.agent`：`COPY c3_agent/ /app/c3_agent/` + `CMD ["python3","-m","c3_agent"]`，
  并且**不再** COPY `deploy/__init__.py`（agent 镜像不再带 `deploy` 命名空间）；
- 引用全局改名：`control_plane/*`、`gateway_common/worker_identity.py`、k8s 注释、14 个测试文件、
  三份活文档与本文件的 §7.13 引用。`deploy/__init__.py` 现在只为 `quota_agent` 存在
  （同型，本轮不动），docstring 写明这条口径；
- **没回改**：`.superpowers/sdd/task-*-report.md` / `c3-*-report.md` 里的 `deploy/c3_agent` 是当时的
  实测记录（含 stack trace），改掉就等于篡改证据。

上线版本 **`0.1.0-794-g117846f-20260930-133726`**（照例：单平台走 `--load`，补推
worker/autoscaler/agent/quota-agent 四个 tag + `docker manifest inspect` 核对）→ `./deploy/k8s-k0s/apply.sh`。

判据：

- 两个 agent 容器（面 A `agent` / 面 B `maint`）的 `/app` 里**只有** `c3_agent`、`gateway_common`，
  且 `python3 -c "importlib.util.find_spec('deploy') is None"`；`/proc/*/cmdline` 里两个面都是
  `python3 -m c3_agent`；日志 logger 名从 `deploy.c3_agent.scan` 变成 `c3_agent.scan`；
- `deploy/scripts/deployment_smoke.py` → **`DEPLOYMENT SMOKE OK`**（agent 参与的段全过：槽位身份授予、
  文件操作、跨节点迁移、模板构建），`deploy/scripts/multinode_smoke.py` → **`MULTI-NODE SMOKE OK`**。

### 7.15 `priv` 的 C 源码跟进搬到 `c3_agent/priv/`（**2026-09-30，已上线**）

§7.14 的口径（`deploy/` 只放配置与脚本）对 `deploy/priv/` 同样成立：那 5 个 `.c/.h` 是
**agent 镜像的编译输入**（`as_uid` / `e2b-maint` 两个 file-capability 二进制），所以跟着唯一的生产
消费者走。提交 `0f75011`：

- `git mv deploy/priv c3_agent/priv`；`Dockerfile.agent` 与 `Dockerfile.test-runner` 的
  `COPY` 各改一行；`Dockerfile.agent` 文件头补一段"为什么在 `c3_agent` 下"，免得下一个人按
  "`deploy/` 才放镜像输入"搬回去；
- `Dockerfile.test-runner` 顺带修掉一条过期注释（它说"worker 镜像也装这两个 broker" ——
  C3 Task 4 片 B 之后 worker 一个都不装，`e2b-slot-spawn` 只剩测试车道）；
- **顺带修一个上一轮漏掉的 bug**：`tests/contract/test_c3_slot_identity_grant.py` 仍在拼
  `deploy/{__init__.py,c3_agent,priv}` 的旧上下文 —— 本机整模块 `skipif` 所以没暴露，
  Linux 车道会在 fixture 的 `docker build` 上失败（`COPY c3_agent/` 找不到）；
- 删除 `tests/security/test_worker_nonroot.py` 里那份**已无人消费**的 priv 上下文拷贝；
- 新增 pin：agent Dockerfile 必须 `COPY c3_agent/priv/`。

上线版本 **`0.1.0-796-g0f75011-20260930-135415`**。值得记的一条实测：**agent 镜像的产物没变** ——
重新构建后 `/var/lib/e2b-priv/{as_uid,e2b-maint}` 仍是同一个 COPY 层（镜像里 mtime 还是
`Sep 30 03:55`，`getcap` 两条同前），因为 COPY 层的哈希只认文件内容，不认源路径。也就是说这次
重新上线是"仓库与集群对齐"，功能上零变化。

判据：`deployment_smoke` → **`DEPLOYMENT SMOKE OK`**、`multinode_smoke` → **`MULTI-NODE SMOKE OK`**；
两个面这两轮分别被调用了 6 次（`POST /agent/grant-slot`）与 46 次（`POST /agent/{chown,rm,walk}`），
即搬走的 C 在线上确实还在跑。

### 7.16 quota-agent 搬到顶层 `quota_agent/`（**2026-09-30，已上线**）

§7.14/§7.15 那条口径的最后一块：`deploy/quota_agent/` 也是"自带镜像的服务"，同样搬到顶层。
提交 `3a0ed5b`：

- `git mv deploy/quota_agent quota_agent`（4 个文件），模块名 `deploy.quota_agent` → `quota_agent`，
  `Dockerfile.quota-agent` 的 `CMD ["python","-m","quota_agent"]`；
- `Dockerfile.quota-agent` 原来是 **`COPY deploy/ deploy/`** —— 为了一个包把整棵清单/脚本树拖进镜像；
  现在是 `COPY quota_agent/ quota_agent/`。镜像内实测 `/app` 只剩
  `quota_agent/ gateway_common/ envd_service/ requirements.txt`（`envd_service` 是
  `xfs_quota` 那个模块，本来就必需）；
- **删除 `deploy/__init__.py`**：它的 docstring 当初就写着自己"只为 `deploy.quota_agent` 存在"，
  搬走后全仓再无一处 `import deploy.*`，于是 `deploy/` 不再是 Python 包 —— 这才让"`deploy/` 只放
  部署配置与脚本"字面成立；
- 两处**逐字钉子**同步改（改一边就红）：`test_upgrade_quota_agent_profile.py`（比对
  `deploy/scripts/lib/helpers.sh` 的报错文案）、`test_worker_manifest_permissions.py`（比对
  `docs/k8s-deployment.md` 那张 token 表里的路径）。

上线版本 **`0.1.0-798-g3a0ed5b-20260930-141013`**。**k8s 车道不部署 quota-agent**
（`deploy/k8s/worker.yaml` 的注释 + `docs/production-deployment-requirements.md` §2.4.4 W4），
所以 `apply.sh` 只 pin 了 8 处镜像引用，`quota-agent` 那枚 tag 是给 compose/服务器侧用的；
对集群而言这次仍是"仓库与集群对齐"，功能零变化。

判据：`deployment_smoke` → **`DEPLOYMENT SMOKE OK`**、`multinode_smoke` → **`MULTI-NODE SMOKE OK`**；
quota-agent 相关 + 钉子类测试 195 passed，全量 `tests/unit` 1934 passed（与改前同数）。

至此 `deploy/` 顶层只剩 `compose/ docker/ k8s/ k8s-k0s/ scripts/ seccomp/ stack/` —— 没有 Python 包，
也没有会被 import 的代码。

### 7.17 autoscaler 并入控制面 + 本地池退役（**2026-09-30，分三次上线**）

N50（`docs/open-issues.md`）：扩缩容循环从独立 Deployment 收进 control-plane，本地 Docker 池整条
退役。仓库侧的判定与形状见 `docs/SCALING.md` §6.4；这里只记**集群上真实发生过的三次**与验收读数 ——
其中前两次是验收本身逼出来的缺陷，值得单独读。

**上线三步（每次都是 `build-and-push.sh` → `deploy/k8s-k0s/apply.sh`）**

| 步骤 | 版本 | 集群动作 | 为什么不是一次 |
|---|---|---|---|
| ① 合并 | `0.1.0-804-g07f2cb5-20260930-164152` | 先 `kubectl delete deploy/sa/role/rolebinding autoscaler`（`kubectl apply` **不 prune**，清单里删掉的对象得手删；删除前留档 `tmp/k0s/autoscaler-removed-20260930.yaml`），再 apply（7 个镜像 pin；control-plane 拿到 `E2B_AS_*` 与 scale 规则；`kubectl diff` 与仓库规格一致后 0 行） | 第一次上线即被测出写路径缺陷，见下 |
| ② 修 scale | `0.1.0-805-g1e39d90-20260930-170113` | 只改控制面镜像 | 见 ② 行 |
| ③ 修冷却标记 | `0.1.0-806-g4392042-20260930-171355` | 只改控制面镜像 | 见 ③ 行 |

**② 写路径：`PUT .../scale` 被 apiserver 判 400**（实测，从控制面 pod 用它自己的 SA token 打同一对请求）

`KubernetesBackend.scale_to` 发的是 `PUT /apis/apps/v1/namespaces/sandlock/statefulsets/e2b-worker/scale`
+`{"spec":{"replicas":3}}`，apiserver 回 **400**：*"the name of the object (e2b-worker based on URL) was
undeterminable: name must be provided"* —— replace 形态要求 body 自带 `metadata.name`。同一 URL 上
`PATCH .../scale` + `application/merge-patch+json` ⇒ **200**，随后 `GET` 读到 `replicas=3`、
`e2b-worker-2` 起来并注册。症状在集群上长这样：每轮一条 `autoscaler tick failed`，舰队**永不增长**
（暖池下限只在低于当前副本数时才被判定，空闲的 2/2 把它藏了 12 天）。修法与钉子见提交 `1e39d90`。

**③ 冷却标记被陈旧写覆盖**（实测，第一次 MIN 3→2 的验收）

`e2b:autoscaler:state` 的 `last_scale_up` 在缩容后被写回 `-inf`。机制：单飞 TTL = poll（5s），两个 tick
可以相邻或重叠；某个 tick 读到 `-inf` 之后被对端的扩缩容插了一刀，它再把自己那份**整快照**写回，
就抹掉了对端刚写下的冷却 —— 而"共享冷却"正是这个 store 存在的全部理由（观测到的后果：3 副本在
同一区间先扩后缩，冷却窗口形同不存在）。修法：`write(before, after)` 只写**两者不同**的字段
（Redis 侧是部分 HSET，无变化不发命令），提交 `4392042`。

**③ 之后的验收（`0.1.0-806`，全部实测）**

| 判据 | 读数 |
|---|---|
| 集群规格 ≡ 仓库 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行** |
| 循环活着且在动 k8s API | `e2b:autoscaler:tick` 连续 5 次采样均存在；两个副本日志 `autoscaler tick failed` = **0** |
| 扩：暖池下限 | `kubectl set env deploy/control-plane E2B_AS_MIN_REPLICAS=3` ⇒ `sts/e2b-worker` 2→3，`e2b-worker-2` 起来并注册（`/internal/nodes` 三个 healthy）；`last_scale_up=1790759746.04` 落盘并在后续 tick 保持 |
| 缩：drain + retire | 放回 `MIN=2`（清单值）⇒ 循环挑中**唯一空闲**的 `e2b-worker-2`，drain → 0 沙箱 → `remove_node` → `sts` 3→2、pod 删除、`draining_node_id` 清空；`last_scale_down=1790759837.90` |
| 冷却真的生效 | `1790759837.90 - 1790759746.04 = **91.9 s ≥ 60 s**`（修 ③ 之前同一序列是"同一区间先扩后缩"） |
| 沙箱不受缩容影响 | 全程 3 个沙箱（两个 worker 各 ≥1）在扩缩容前后都能 `exec` 出 `alive-<hostname>`；收尾后 `/internal/fleet/sandboxes` = `{}`、两节点 `reservedMemoryMB=0` |
| 官方冒烟 | `multinode_smoke.py` ⇒ `MULTI-NODE SMOKE OK`；`deployment_smoke.py` ⇒ `DEPLOYMENT SMOKE OK`（命令/文件、跨节点迁移保文件、网络配置、远端卷隔离、模板构建→registry→worker 拉取、MCP 网关）—— 都在 `0.1.0-806` 上重跑 |
| 最终形态 | `control-plane` 2/2（`…control-plane-gateway:0.1.0-806-…`）、`e2b-worker` 2/2、`redis` 1/1；`autoscaler` 的 Deployment/SA/Role/RoleBinding **0 个** |

**残留**：见 `docs/open-issues.md` **N51** —— `pod-deletion-cost` 只被 ReplicaSet 控制器读，StatefulSet
缩容永远删最高序号，所以"退出的是你 drain 的那个节点"只在两者一致时成立（本次验收里空闲的正是
最新的 `-2`，所以走的是一致那条路）。修法两选一（未做），触发条件写在 N51 行。

### 7.18 N51：缩容只许删"它真会删的那个 pod"（**2026-09-30，已上线 `0.1.0-808-g1ca681e-20260930-175431`**）

§7.17 的残留。修法与理由见 `docs/open-issues.md` N51：`ScaleBackend.retire_victim(candidates)`
由**后端**回答"这一轮缩容真会删谁"（Deployment = 循环的首选；StatefulSet = 最高序号，且必须
在候选里，否则回 `None` = 谁都别删），循环在第 5 步与第 3 步都先过这一问，`None` 就什么都不做
并打一条按节点去重的 `scale-down held` 告警；第 3 步顺带补上一直缺的 `current > min_replicas`。

**集群验收（真构造出危险形状，不是推断）**

| 步 | 动作 | 读数 |
|---|---|---|
| 准备 | 4 个沙箱铺满 `-0`/`-1`，`MIN=3` 扩容 ⇒ `e2b-worker-2` 起来；再建 1 个沙箱 —— 调度器按"剩余容量优先"把它放到最新的 `-2` | `fleet/sandboxes` = `{worker-2:[1], worker-1:[2], worker-0:[1]}` |
| 造危险形状 | 杀掉 `-0`/`-1` 上的沙箱（两台空闲），只留 `-2` 上那一个 | `{"e2b-worker-2":["sbx_8885214d353aa856"]}` |
| **必须拒绝** | 放回 `MIN=2`（清单值） | **120 s 内 `sts` 一直是 3/3**（远超 60 s 冷却），三个 pod 全在，**`-2` 上的沙箱照常 `exec`**；日志恰好一条（按节点去重）`scale-down held: ... would delete a different pod than the idle node e2b-worker-0 (it shrinks from the top)` |
| **必须收它** | 杀掉 `-2` 上那个沙箱 ⇒ 最高序号变空闲 | 循环立刻 drain + retire，`sts` 3→2，日志 `retired drained node e2b-worker-2`（**是 `-2`**，不是它排序上的首选 `-0`） |
| 收尾 | | `fleet/sandboxes` = `{}`；两节点 `reservedMemoryMB=0`；`kubectl diff` 与仓库规格 **0 行**；`control-plane` 2/2、`e2b-worker` 2/2、`redis` 1/1 |

修前同一形状的行为（按代码推断，未再复现）：循环 drain `-0`（候选表按 node id 排序，`-0` 在前）、
`remove_node` 打注解后整副本 -1 ⇒ **控制器删掉的是 `-2`，把上面那个活沙箱连同 pod 一起收走** ——
而循环从没选过它。

### 7.19 C3 出厂形态收尾：删 C1 死代码 + slot 身份默认（**2026-09-30，已上线 `0.1.0-811-g071beb4-20260930-202337`**）

N52（`docs/open-issues.md`）：把 C1 时代剩下的死代码删掉（`maint.c` 2267 → 344 行、
`priv_common.c` 575 → 325 行、`tests/contract/test_broker_socket_c.py` 2038 行），并把
`E2B_SLOT_IDENTITY` 未设时的默认从恒 `spawn` 改成**按形态解析**（有 agent ⇒ `agent-grant`，
没有 ⇒ `spawn` + 一条 WARNING）。两次上线（`810` 删死代码、`811` 补"未知动词"的拒绝措辞），
`build-and-push.sh` → `apply.sh` 各一次。

**集群验收（`0.1.0-811`）**

| 判据 | 读数 |
|---|---|
| 特权二进制里的 socket 入口没了 | 在 agent pod 的面 B 容器里 `e2b-maint serve` ⇒ `usage: unknown verb 'serve' (expected chown|rm|walk)`（exit 2）；无参数 ⇒ `expected chown|rm|walk`（用法文本里已不含 `serve`/`ping`） |
| 能力集未动 | `getcap /var/lib/e2b-priv/e2b-maint` = `cap_chown,cap_dac_override=ep`；`as_uid` = `cap_setgid,cap_setuid=ep` |
| 默认值路径不误报 | 两个 worker 的日志里 `E2B_SLOT_IDENTITY is unset` **0 行**（清单显式设了 `agent-grant`） |
| 文件操作链路未回归 | 面 B 的 `chown/rm/walk` 仍可用：`multinode_smoke.py` ⇒ `MULTI-NODE SMOKE OK`；`deployment_smoke.py` ⇒ `DEPLOYMENT SMOKE OK`（含跨节点迁移保文件、远端卷隔离、模板构建→worker 拉取、MCP 网关） |
| 仓库规格 ≡ 线上 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行** |
| 终态 | `control-plane` 2/2、`e2b-worker` 2/2、`redis` 1/1、两个 DaemonSet 就绪；`fleet/sandboxes` = `{}` |

**没动的**（§7.18 结尾那份清单里属于"回退杆"而非垃圾的项）：`E2B_PRIV_HELPER_TRANSPORT=exec`
与两个 file-capability 二进制仍留在**测试车道**（`Dockerfile.test-runner`）；`E2B_SLOT_IDENTITY=spawn`
仍是无 agent 形态（单机示例、车道）的合法取值；`E2B_AS_K8S_KIND=deployment` 仍是 pre-N20 兼容。

### 7.20 回退杆清理：删 `E2B_AS_K8S_KIND` 与 `spawn`（**2026-09-30，已上线 `0.1.0-814-gf8d1685-20260930-210628`**）

用户裁定：「这几个都可以去掉了」/「现在只需要 agent grant 这个形态，真有需要的时候从 git 拿吧」。
两轮提交：`05fa7c3`（③ 只缩 StatefulSet）、`f8d1685`（② 槽位身份只由 agent 授予）。

**集群验收（`0.1.0-814`）**

| 判据 | 读数 |
|---|---|
| 缩容只认 StatefulSet | 控制面 env 里 `E2B_AS_K8S_KIND` **0 处**；Role 只剩 `pods(get,list,patch,delete)` 与 `statefulsets,statefulsets/scale(get,update,patch)`（`deployments{,/scale}` 已删） |
| 槽位身份只走 agent-grant | 两个 worker 的日志里 `spawn` / `E2B_SLOT_IDENTITY is unset` 相关行 **0 条**（清单显式写 `agent-grant`，代码也只接受它） |
| 文件操作与路由未回归 | `MULTI-NODE SMOKE OK` + `DEPLOYMENT SMOKE OK`（含跨节点迁移保文件、远端卷隔离、模板构建、MCP 网关） |
| 仓库规格 ≡ 线上 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行** |
| 终态 | `control-plane` 2/2、`e2b-worker` 2/2、`redis` 1/1、两个 DaemonSet 就绪；`fleet/sandboxes` = `{}` |

**代价（点名，用户已接受）**：route B 的车道覆盖从"root + spawn"改为"agent-grant"（车道由
`tests/security/conftest._lane_identity_reporter` 自己写 `uid_map`/`gid_map` 承接，即 agent 的那一步）；
pre-C3 的 root/no-agent 形态不再被支持 —— `route_b._spawn_slot` 与 `c3_agent/priv/slot_spawn.c`
都在 git 历史里。

**仍剩第 ① 根**（见 N52 ⑤）：`E2B_PRIV_HELPER_TRANSPORT=exec` + 两个 file-capability 二进制 +
`E2B_PRIV_HELPERS`（`envd_service/priv_helpers.py` 的本地实现半、车道 phase 2 与约 20 条用例）。

### 7.21 ① 第一步：`exec`/`socket` 传输具名拒绝（**2026-09-30，已上线 `0.1.0-816-g1c85e7c-20260930-213813`**）

用户裁定「1,2 也去掉吧，现在只需要 agent grant 这个形态」。这一版只动**行为面**（未删死代码）：

* `E2B_PRIV_HELPER_TRANSPORT` 只认 `auto|agent`；`exec` 与 C1 的 `socket` 进 `RETIRED_TRANSPORTS`，启动期按名字拒绝。
* `configure_priv_helpers` 不再解析/安装本地 `PrivHelpers`：只在部署声明 agent 形态时 wire `agent_fileops`。
* `file_steps_available` 收窄成"agent 客户端在不在"；启动警告改写为"没有特权文件操作路径"（旧文案让运维去装已经不存在的二进制）。
* 安全钉子纠了一次：新文案不能出现 agent 的地址变量名（`envd_service/**` 硬规则 5）。

**集群验收**：`deployment_smoke.py` ⇒ `DEPLOYMENT SMOKE OK`；`kubectl diff` **0 行**；`control-plane` 2/2、`e2b-worker` 2/2；worker 日志无 spawn/默认值告警。仓库侧 `tests/unit` 与基线逐条对比无新增失败。

**仍未做（① 第二步）**：`PrivHelpers` 类及其 argv 构造、capability 解码/校验、`resolve_priv_helpers`/`_build_helpers`/`broker_*`/`helpers_cover` 等约 700 行现在是**不可达代码**，`E2B_PRIV_HELPERS` 旋钮与 4 份清单里的声明成了空转 —— 删它们 + 车道 phase 2 与约 20 条用例收尾。

### 7.22 ① 第二步：删掉 worker 侧 file-capability 形态的残留（**2026-09-30，已上线 `0.1.0-818-g7205fba-20260930-221244`**）

提交 `7205fba`。承接 §7.21 的行为面收口，这一步是**纯删死代码 + 清开关/清单声明**（无行为变化）：

* `envd_service/priv_helpers.py` **1314 → 343 行**：`PrivHelpers` 类、argv 构造、file-capability 解码/校验、`resolve_priv_helpers`/`_build_helpers`/`_require_*`/`broker_*`/`helpers_cover`/`active_helpers` 全删。留下的是形态开关（`_transport_setting`/`RETIRED_TRANSPORTS`）、`PrivHelperError`、`WalkEntry`、`request_identity`、`remove_tree`/`dir_size`（worker 自己的进程内实现）、`helpers_unavailable_reason`、`file_steps_available`、`check_worker_identity_outside_pool`、`WORKSPACE_MODE`。
* 8 个调用点收敛：非 root 且无 agent 的 worker 从"走 broker"改成**具名拒绝**（secret 交接、checkpoint 交接），其余是 root 自己的 `os.chown`；`provision_sandbox_volume_mount` 的三分支合成 chmod → agent/root 两分支。
* `E2B_PRIV_HELPERS` 旋钮连同 4 处清单声明一起删除（`config.py` 字段、k8s worker、stack compose、demo compose 的 `off` 声明）。
* 车道：`Dockerfile.test-runner` 不再构建 `e2b-maint`（它只存在于 agent 镜像）；`test-prod-shaped.sh` phase 2 由"uid 65534 + 四个 cap + brokers"改为"uid 65534、空 BND"的 E5.1 形态，并移除以 broker 为前提的 route-B 契约。

**集群验收（`0.1.0-818`）**

| 判据 | 读数 |
|---|---|
| worker 里没有任何本地特权件 | `exec e2b-worker-0 -- ls /var/lib/e2b-priv` ⇒ `No such file or directory`；StatefulSet 的 env 里 `E2B_PRIV_HELPERS` **0 处**（只剩 `E2B_PRIV_HELPER_TRANSPORT`） |
| agent 那侧照旧 | `exec <agent> -c maint -- getcap /var/lib/e2b-priv/e2b-maint /var/lib/e2b-priv/as_uid` ⇒ `cap_chown,cap_dac_override=ep` / `cap_setgid,cap_setuid=ep` |
| 退役传输在**线上**具名拒绝 | `E2B_PRIV_HELPER_TRANSPORT=exec python3 -c "…_transport_setting()"` 在 worker pod 里 ⇒ `REFUSED: E2B_PRIV_HELPER_TRANSPORT='exec' is retired (2026-09-30, open-issues N52) …` |
| 未回归 | `MULTI-NODE SMOKE OK`（两 worker 各 2 沙箱、commands/files/health/stdin、kill 后预约 0）+ `DEPLOYMENT SMOKE OK`（含跨节点迁移保文件、远端卷隔离、模板构建→registry→worker→rootfs、MCP 网关） |
| 仓库规格 ≡ 线上 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行** |
| 终态 | `control-plane` 2/2、`e2b-worker` 2/2、`e2b-c3-agent` 2/2、`redis` 1/1、`seccomp-installer` 就绪 |
| 仓库侧测试 | `tests/unit` 2087 passed / 3 failed、`tests/contract` 378 passed / 3 failed —— 与改动前 HEAD 基线**逐条相同**（macOS 上的 `test_real_root_gate` dlopen 与两条 xfs_quotactl）；全量 2667 条 collection 干净 |

**形态收敛后的"没有的东西"（点名）**：worker 侧 `exec` transport、两个 file-capability 二进制、`E2B_PRIV_HELPERS` 旋钮、`PrivHelpers` 这一整套 —— 都只在 git 历史里（需要时整批 revert）。今天 worker 的三条文件步骤路径只剩：**agent**（出厂形态）、**root**（worker 自己是 root 时）、以及**进程内 E5.1**（两者都没有，能力与隔离都降级并打一条 WARNING）。

### 7.23 沙箱第一档 syscall 加固 + clone3 命名空间位（**2026-10-01，已上线 `0.1.0-824-gf2aec0b-20261001-073534`**）

提交：fork `8973f8c`（黑名单 + `clone3`），主仓 `f2aec0b`（文档 + 子模块指针）。
起因是一次「沙箱内还能摸到哪些危险 syscall」的审计：探针 `tmp/syscall-probe/probe.py`
在真 worker pod 里逐条调用候选（故意非法参数，`EINVAL`/`EBADF`/返回 ≥0 = 到达内核），
量出 26 条内层黑名单没拦、外层也不一定拦的调用。

* `DEFAULT_BLOCKLIST_SYSCALLS` **+26 条**：mount API 全家（`fsopen`/`fsconfig`/`fsmount`/
  `move_mount`/`fspick`/`mount_setattr`/`statmount`/`listmount`）、ptrace 类三条
  （`process_madvise`/`process_mrelease`/`kcmp`）、`quotactl_fd`、`kexec_file_load`、
  旧 AIO 五条、`memfd_secret`、`modify_ldt`（x86-only）、NUMA 六条。
* `handle_fork` 的命名空间禁令从「只查 `clone`」改成走 `clone_flags()`，覆盖 `clone3`
  （cBPF 读不到 `clone_args`，所以这条只能由 handler 管）。
* `sys/path_surface.rs`：5 条由 `Open`/`Gated` 改 `Blocked`，待决策集合收敛到 7 条。

**集群验收（`0.1.0-824`）**

| 判据 | 读数 |
|---|---|
| 上线后沙箱内逐条复测 | **14 条从"到达内核"翻成 `EPERM`**：`fsconfig`/`mount_setattr`（原 `EINVAL`）、`statmount`/`listmount`（原 `ENOSYS`，只被外层挡）、`quotactl_fd`/`process_madvise`/`process_mrelease`/`kcmp`（原 `EBADF`/`ESRCH`）、`io_setup`/`io_submit`、`memfd_secret`（原**成功拿到 fd**）、`get_mempolicy`/`set_mempolicy`（原**返回 0**）、`mbind` |
| 刻意留活的不受影响 | `memfd_create` 仍 `EFAULT`（到达内核）、`prlimit64` 仍成功、`rt_sigqueueinfo`/`rt_tgsigqueueinfo`/`adjtimex`/`clock_adjtime` 仍 `EFAULT`、`pidfd_open`/`pidfd_send_signal` 仍放行（in-sandbox init 要用） |
| 未回归 | `MULTI-NODE SMOKE OK`（两 worker 各 2 沙箱、命令/文件/健康/stdin、kill 后预约 0）+ `DEPLOYMENT SMOKE OK`（跨 worker 迁移保文件、远端卷隔离、模板构建→registry→worker→rootfs、箱内 MCP 经代理） |
| 终态 | `control-plane` 2/2、`e2b-worker` 2/2（新镜像 `0.1.0-824`）、`e2b-c3-agent` 2/2、`redis` 1/1、`seccomp-installer` 就绪；base image `peek cached=true` |
| 仓库侧测试 | `--lib` **914 passed / 0 failed**；`--test integration` 与基线 `a21a507` 逐条 diff **无新增失败**（容器里 26~29 条环境性失败两侧同名，抽测单独运行均通过）；新增 `test_first_tier_blocklist_refused`（真沙箱内 26 条 `EPERM`）与 `test_clone3_namespace_flags_refused`（先红后绿：修前 `EINVAL`＝进内核，修后 `EPERM`，普通线程创建仍 OK） |

**`clone3` 那条为什么不能靠外层 profile**：外层 `deploy/seccomp/sandlock-worker.json` 把
`clone3` 整条 deny 成 `ENOSYS`，线上观测不到差异 —— 也就是说修前这条禁令实际是**容器
profile 在承担**，换一个更宽 profile 的宿主就没了。修后由沙箱自己的 `handle_fork` 承担。
不能改成"禁用 `clone3`"：glibc 2.34+ 的 `pthread_create` 走它（同批集成测试里那条线程
对照就是这个用途）。

**同批更正**：`deploy/seccomp/README.md` 里"mount/pivot_root/umount2 保持 gated、未放宽"
是错的 —— 它们是 N35 真根那批加进去的**无条件 allow**（实测 worker 侧 `mount(NULL,…)`
拿到 `EFAULT`＝到达内核，`open_tree`/`fsopen` 才是 `EPERM`）。同一个文件在 k8s pod 上与
本地 `--cap-drop ALL` 容器上对 `caps:` 条件的解析还不一致（`fsconfig` 一边到内核一边被拒），
结论：mount API 不能指望外层 profile 兜底。

### 7.24 N53：worker 丢掉"控制面不认的"运行时记录（**2026-10-01，上半已上线 `0.1.0-834-g8272b6c-20261001-100321`；下半见同节末尾，`0.1.0-836-g6d7532b-20261001-102113`**）

提交 `8272b6c`。用户报的现象是"预热后建箱的时间花在哪"，量到一半先撞上这个：

**现象（上线前实测）**：worker-0 在 120 s 内对控制面打了 **598 次** `POST /internal/nodes/e2b-worker-0/file-op` → **404**（worker-1 47 次），连续数小时；worker 日志被
`WARNING envd_service.runtime.registry: cannot measure sbx_… through the agent: … (HTTP 404)` 淹没 —— 官方日志文件只有 10 MiB，**这次启动的日志行被挤出了容器日志**（`kubectl logs` 的第一行就是风暴行）。
控制面侧 `GET /sandboxes` = **0**、`workspaces/` 下 **0 棵树**、`state/_runtime/` 61 个历史目录（只有 3 个还带 `sandbox.json`）⇒ 这些 id **只活在 worker 的内存里**。

**根因两层**：① `AgentFileOpsError` 把「控制面 404 = 没这个沙箱」与「控制面不可达/超时」归成同一种错误，调用方唯一能做的就是重试 —— 而 404 是**确定**答案（硬规则 1/3：这个 id 上任何操作都不会再被授权）；② worker 的运行时记录是对该树的"声明"，声明一旦控制面不认，每一轮磁盘计量都会去 walk 一个不存在的沙箱（每 ~2 s 一轮）。

**修法（TDD：先红后绿）**：新增 `AgentFileOpsUnknownSandbox`（`AgentFileOpsError` 子类，HTTP 404 时抛，消息逐字不变 ⇒ 现有 catch 全部照旧）；`RuntimeRegistry.disk_usage_snapshot` 捕获它 → 一条具名 WARNING + `unregister()`（丢掉声明，树留给控制面的 orphan-tree GC），传输类错误保持原样（可重试、记录保留）。

**集群验收（`0.1.0-834`）**

| 判据 | 上线前 | 上线后 |
|---|---|---|
| 整队 file-op 404 | worker-0 **598 次 / 120 s** | **0 次 / 60 s**（控制面侧整段没有任何 file-op 调用） |
| `cannot measure … 404` 日志 | 每 ~2 s 一轮，淹没日志 | **0 行 / 60 s**（worker 启动行重新可见） |
| 新行为：孤儿记录被丢弃 | —— | 复现：TTL 箱（`timeout=60`）过期 → 控制面先删记录（worker 拆除 500、树与 `sandbox.json` 残留）→ 用残留记录里的 token 直连 worker envd `GET /envs` 触发 `registry.get()` 复活记录 → **90 s 内出现** `the control plane has no record of sbx_2215b701f1215acf: dropping this worker's runtime record (AgentFileOpsUnknownSandbox: …)`，该 id 随后的 file-op 404 = **0**（修前是每 2 s 一次、永不停止） |
| 仓库侧 | —— | `tests/unit` **2090 passed**；失败名单 = 基线 3 条（macOS 的 dlopen 与两条 xfs_quotactl）**+ 2 条与本次无关**的 `test_docs_only_point_at_repo_artifacts`（来自尚未入库的 `docs/security-audit/findings-k0s-2026-10-01.md` 里引用的一批 tmp/ 探针名） |

**顺带点名、同批已修的上游缺口**：TTL 到期时控制面**先**删自己的记录、**再**让 worker 拆除（`control_plane/registry/ttl.py`：`remove_expired()` → `on_expired`），于是 worker 的 `remove-workspace` 被自己的控制面以 404 拒绝，留下孤儿树 + `_runtime/<id>/sandbox.json`；后者还能被后续 `get()` 复活。

### 7.24 下半：TTL 拆除顺序 + 历史残留清理（**2026-10-01，已上线 `0.1.0-836-g6d7532b-20261001-102113`**）

提交 `6d7532b`。承接上一节点名的上游缺口 —— 它才是"孤儿树 + 可复活记录"的来源。

**修法（TDD：先红后绿）**：`SandboxRegistry.expired_candidates()` 只回答"哪些记录到期了"、**不释放**（`remove_expired()` 改成它的薄包装，行为与调用点不变）；`TTLSweeper` 改成 `expired_candidates()` → `on_expired`（**此时记录仍在**，worker 的 `remove-workspace` 有授权）→ `registry.delete()`（拆除之后再释放；`UnknownSandboxError` 视为已被并发释放）→ `cleanup_workspace()`。窗口 = 拆除本身，代价也点名：这几秒里"已过期但仍在拆除中"的记录对 `X-Sandbox-Id` 幂等建箱可见。

| 判据 | 修前 | 修后 |
|---|---|---|
| 先红 | `tests/unit/test_ttl.py` 的新用例：`on_expired` 里 `registry.get(id)` 抛 `UnknownSandboxError`（= worker 那条 404 的来源） | —— |
| 集群：TTL 箱（`timeout=60`）到期 | `DELETE /agent/sandboxes/<id>` → **500**（`AgentFileOpsUnknownSandbox`），树与 `sandbox.json` 残留 | `DELETE …` → **204**；`_runtime/<id>` 与 `workspaces/<id>` **都不存在**（json=no / tree=no）；该 id 之后 0 次 404 |
| 历史残留清理 | `state/_runtime/` 下 **63** 个孤儿目录（×2 探针 + 61 个 9 月遗留，329 KB；其中 5 个带 `sandbox.json`） | 用"控制面自报的节点沙箱清单"做守卫（当前 0 个）逐个删除：**removed 63 / kept 1**（只留基建目录 `.checkpoints`）；清理后 `dirs=0`、`du=1.0K` |
| 清理后队列状态 | —— | CP 侧 file-op 调用 **0 / 60 s**、`cannot measure` **0–1 行 / 120 s**（平台盘扫描的良性竞态提示）、`kubectl diff` **0 行**、10 个 pod 全 Running |
| 仓库侧 | —— | `tests/unit` **2091 passed**（失败 = 基线 3 + 2 条与本次无关的 docs pin）；`tests/contract` 的 TTL/配额三件（partition reconcile / pause-resume quota / redis 多副本）**17 passed** |

> ⚠ 清理是**一次性**的：它删的是"控制面已经不认"的 `_runtime/<id>` 目录（守卫是控制面自己那份清单），不含 `.checkpoints`。以后不会再攒 —— TTL 拆除成功后 `_runtime/<id>` 随树一起走。

### 7.25 N54：镜像 digest 解析结果落盘缓存（**2026-10-01，已上线 `0.1.0-839-g4271c45-20261001-112409`**）

提交 `4271c45`。起因是那个问题——"预热以后建箱的时间主要花在什么地方"——上一轮的逐段实测
（README §1）给出的答案是：**0.45–0.55 s 花在向镜像仓库解析基础镜像**，不是登记、也不是落盘。
用户裁定「按 3 改」：把解析挪到预热阶段，并且**落盘**。

**现象（上线前实测）**：一次建箱窗口里 worker 对
`dockerauth.cn-hangzhou.aliyuncs.com/auth` + `registry.cn-shanghai.aliyuncs.com/v2/…/manifests/…`
发约 **6 次 HTTPS（日志里约 620 ms 墙钟）**；直接测那条"镜像就绪探测"
（`GET /agent/images/<ref>/warm`）：解析缓存**冷 430–482 ms、热 1 ms**。

**根因两层**：① 解析结果只存在**进程内** 60 s 字典（`_DIGEST_CACHE`）—— 进程重启、同节点第二个
worker、TTL 过期都要重付这笔钱；② `peek` 与 `resolve` 是两条路：后者直接
`fetch_platform_manifest`，**完全不走缓存**。于是"预热过"只对同一个进程、60 s 之内成立。

**修法（TDD：先红后绿）**：`_platform_digest(cache_dir=…)` 增加**落盘**缓存
`<image cache>/.digests/<key>.json`（键含 image/scheme/username/credential_host，TTL =
`E2B_IMAGE_MANIFEST_TTL_S`，原子写）；`peek_image_warm` 把 `cache_dir` 传下去；
`resolve_image_rootfs` 先读盘上的 digest、命中已解包 rootfs 就直接返回（不再取 manifest）；
`_cache_usage` 把 `.digests/` 排除出"点号前缀 = staging"那条规则，否则 `prune_image_cache`
会在 staleness 窗口后把它删掉（这一条用例先红）。

**集群验收（`0.1.0-839`）**

| 判据 | 读数 |
|---|---|
| 建箱、**控制面 pod 内**发起（同一入口，只剩平台耗时） | **p50 228 ms**（n=10）；另两轮 n=4 / n=3 分别 **223 / 236 ms** —— 修前是 **0.66 s**（n=10，本机经入口） |
| 建箱、本机经跳板隧道发起 | p50 **334 ms**（n=10）—— 与上一行差 ~100 ms 就是"客户端到入口"那一段网络 |
| 冷热对照：把两个 worker 的 `.digests/*.json` 删掉再跑 | 预热那一发（必须重新解析 manifest）**652 ms** → 之后 **228 ms**；也就是解析本身 ≈ **0.41 s**，与修前逐段实测的 0.45–0.55 s 同量级 |
| `GET /agent/images/<ref>/warm` 热态 | **0.9–1.0 ms**（两 worker × 两 ref × 5 次；482 ms 的峰值只出现在删过缓存的那次） |
| 落盘缓存确实写下了 | 两 worker 的 `.digests/` 各 **2 条**（`python-mcp:3.14@sha256:…` 与 `python:3.11-slim`）；删掉后被下一次解析**自动重建** |
| 建箱窗口内 worker→registry 请求 | **0 次**（正常建箱跑一遍、再按时间窗数日志：0 行；日志里那几发 6 次 HTTPS 的突发，只出现在我**故意删缓存**的窗口） |
| 探针不留残留 | `GET /sandboxes` = **0**；两 worker `state/_runtime` = **0**、`workspaces/` = **0**（每次建完立刻 kill） |
| 终态 | 10 个 pod 全 Running；CP/worker/agent 都 pin 在 `0.1.0-839-g4271c45-20261001-112409` |
| 仓库侧 | 镜像/缓存车道 **81 passed**（`test_oci_registry.py`、`test_image_cache_sharing.py`、`test_image_rootfs_cache_split.py`、`test_worker_image_warm.py`、`test_local_oci_images.py`、`test_compose_base_image_shape.py`） |

**复跑**：

```bash
# ① 控制面内（只有平台耗时）——探针是纯标准库的，直接灌进 pod 跑
kubectl -n sandlock exec -i <control-plane-pod> -c control-plane -- \
    python3 - --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 \
    < deploy/scripts/acceptance/create_latency_probe.py
# ② 本机（平台 + 网络）
python deploy/scripts/acceptance/create_latency_probe.py --base http://<入口>:3000 --key "$E2B_API_KEY" --n 10
# ③ 解析缓存那一层（冷/热各测一次）
kubectl -n sandlock exec -i <worker-pod> -- python3 - \
    --image "<ref>" --key "$E2B_INTERNAL_API_KEY" < deploy/scripts/warm_base_image.py
```

**两条边界**：缓存过 `E2B_IMAGE_MANIFEST_TTL_S` 仍会重解析（tag 挪动能自我失效，这是有意的）；
`.digests/` 是纯缓存，删掉只多花一次解析，不会缺镜像。

### 7.26 建箱再往下抠：三处白付往返 + 一个能在线量的阶段开关（**2026-10-01，已上线 `0.1.0-841-g0d7dc76-20261001-131547`**）

提交 `0d7dc76`。§7.25 把建箱从 0.66 s 拉到 0.23 s 之后，问题是"还能不能继续"——先量，再改。

**先切分**（幂等重放：控制面先真建一个沙箱拿到授权，再把同一份 payload 直打 worker 的 agent 口）：
`POST /agent/sandboxes` 那一跳 **p50 203 ms / 整条 235 ms ≈ 85%**，控制面自己（准入、选节点、
registry 落 Redis、peek、派发）只占十几毫秒。**所以优化面全在 worker 那一段**。

**改掉的三处"白付"**（都不动语义）：

1. `uid_pool.commit()`：控制面分配 uid 的出厂形态（`claim`）**根本不写 reservation marker**，
   可旧代码仍先读一次 `_recorded_uid` 再删一次不存在的 marker —— 每次建箱白付一次 NFS 读 +
   一次空 unlink。现在先看 marker 在不在，不在就直接返回；marker 存在时的行为逐字不变
   （包括"记录不在盘上 ⇒ 保留 marker"的 fail-safe）。
2. `AgentFileOps`：file-op **每次调用新建一个 `httpx.Client`**（新 TCP + 解析 `control-plane`
   这个 Service 名）。同一时期实测：什么都不做的 `walk-workspace` 要 **27 ms**，而真走树的
   `chown` 是 49 ms。改成每个 worker 一个常驻客户端（带锁，随 lifespan 关闭）。
3. 控制面 `_provision_remote`：同样每次建箱 new 一个 `AsyncClient`。改用 `app.state.remote_http`。

**新增的测量开关**：`E2B_CREATE_TRACE=1`（`gateway_common/create_trace.py`）——每次建箱按
`provision` / `prime` / `record` / `commit` / `fileop:*` 各打一行 INFO，**不用重启就能用
`kubectl set env statefulset/e2b-worker` 打开**（`_disk_trace` 的同款做法）。

**集群验收（`0.1.0-841`）**

| 判据 | 读数 |
|---|---|
| 建箱、控制面 pod 内发起（n=10） | **p50 191 ms**（p95 224）—— 改前同口径 **228 ms** |
| 建箱里 worker 那一跳（幂等重放，n=6） | p50 **198 ms** ⇒ 剩下的时间几乎全在 worker |
| 逐段（`E2B_CREATE_TRACE=1`，n=7） | `provision` **173 ms**（其中 `fileop:chown-workspace` **71 ms**、`record` **48 ms**，其余 ~54 ms 是两次 mkdir + 记录查找 + uid 认领 + 响应）、`prime` **17 ms** |
| `commit` 段 | 两个 worker 上 **0 行** —— marker 优先的短路生效（出厂形态下它本来就是空操作） |
| 未回归 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行**；`MULTI-NODE`/`DEPLOYMENT` 冒烟见 §8 入口；10 个 pod 全 Running |
| 不留残留 | `GET /sandboxes` = 0、两 worker `_runtime`/`workspaces` = 0（探针每轮自建自删） |
| 仓库侧 | `tests/unit` **2115 passed / 8 failed** —— 8 条与改动前基线逐条相同（3 条 `test_disk_scan_offload` 来自尚未提交的 SEC-K0S-006、2 条 docs pin 来自未跟踪的 `docs/security-audit/…`、3 条 macOS/xfs 固有） |

**量过但没做的**（省下两次白改）：`xfs_project_supported` 每次建箱只 **0.34 ms**（本地读
`/proc/mounts`，出厂 `quota_via_agent=false`），缓存它没有收益；`resolve_image_rootfs` 命中
缓存 **0.2 ms**、`create_executor` **0.3 ms** —— 也就是说 §7.25 里记在"预热运行时上下文"上的
那笔账是错的，真正的 `prime` 是 17 ms（含 `SandboxRuntimeContext` 自己的构造）。

**复跑**：

```bash
# ① 建箱延迟（控制面内 / 本机经隧道）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - --base http://127.0.0.1:3000 \
    --key "$E2B_API_KEY" --n 10 < deploy/scripts/acceptance/create_latency_probe.py
# ② worker 那一跳占多少（幂等重放）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - "$E2B_API_KEY" "$E2B_INTERNAL_API_KEY" \
    < deploy/scripts/acceptance/worker_provision_cost.py
# ③ worker 内部的逐段（开着 trace 跑一次 ①，再回来关掉）
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE=1
kubectl -n sandlock logs e2b-worker-0 --since=2m | grep "create trace:"
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE-
```

**剩下的两块（要动设计，本轮没碰）**：`fileop:chown-workspace` **71 ms** 是 worker→控制面→
agent→控制面→worker 一整圈（其中约 27-30 ms 是那一圈的固定开销，其余是 agent 在 NAS 上递归
`lchown`）；`record` **48 ms** 是一次 mkdir + 一次带 fsync 的原子写。前者的省法（让 worker 拿
一次性授权直连本节点 agent、或把"建树 + 改属主"合成一次 agent 操作）会动 **agent-grant 那条
授权边界**；后者的省法（把落盘挪到响应之后或与 chown 并行）会动**"记录先于响应落盘"这条
durability 约定**，且"建完立刻 kill"是常用姿势 —— 并行的写有可能在拆除之后落地，正是 N53 清理
过的那类残留。两者都要先有设计再动手。

### 7.27 建箱材料化改走控制面直送（载体 C）（**2026-10-01，已上线 `0.1.0-864-g3377ffc-20261001-204142`**）

提交 `19ac378..3377ffc`。规范 `docs/superpowers/specs/2026-10-01-create-path-grant-design-v2.md`，
计划 `docs/superpowers/plans/2026-10-01-create-path-grant-cp-direct.md`。

**改了什么**：建箱的树 / 快照拷贝 / 卷切片 / 改属主改成**控制面在拨 worker 之前**，往**既有的**
CP→agent 通道送一条 `materialize` 指令（`POST /internal/nodes/{host}/agent/materialize`，
`X-Internal-Key`，与 `chown`/`rm`/`walk` 同一条路由、同一个 agent 自检），agent 一次做完；
worker 的 payload 带 `materialized: true` 就跳过建树段与属主段。不新增通道：worker 侧仍然没有
`E2B_C3_AGENT_TOKEN`（`tests/unit/test_c3_agent_manifest.py` 的钉子不动），清单一个字节不用改。
载体 B（worker 领授权直连）整块删除：`gateway_common/create_grant.py`、
`/internal/nodes/{node}/file-grant`、agent 的 `/internal/grants/file-op` + jti 表、
`AgentFileOps.materialize`/`AgentMaterializeUnsupported`、`E2B_CREATE_GRANT_TTL_S`。
`materialize-tree` 现在 `callers=frozenset()`：**任何请求面都问不到它**（worker 的 `/file-op`
按名字 400 拒），只有控制面自己推导得出。

**集群验收（`0.1.0-864`）**

| 判据 | 读数 |
|---|---|
| 建箱、控制面 pod 内发起（n=10，三轮） | p50 **193 / 195 / 194 ms**（p95 204 / 290 / 249）—— §7.26 同口径基线 **191 ms** ⇒ **端到端没有变快** |
| worker 逐段（`E2B_CREATE_TRACE=1`） | `provision` **173 → 76–79 ms**、`prime` **17 → 6 ms**；**建箱路径上 `fileop:chown-workspace` 0 行**（窗口里唯一那一行来自迁移路径，见"边界"） |
| 新那一跳本身（控制面 pod 内直打 agent 的 49986，n=6） | `materialize` p50 **70.8 ms**；同一条连接上的空指令 **1.8 ms**、TCP connect **0.1 ms**、同一棵树的 `rm` **22.6 ms** ⇒ 那 ~70 ms 是 **agent 自己的活**（`mkdir` + `e2b-maint chown --recursive` 走 NAS），不是网络 |
| 快照落点（生产形状） | 快照里 `workspace/kept.txt` ⇒ 新箱 `GET /files?path=workspace/kept.txt` = **200 `kept\n`**，而 `workspace/workspace/kept.txt` = **404** ⇒ 没有 v1 那样多下沉一层 |
| 树真的建出来了（C1 的活体对照） | 建完立刻读盘：`<ws>/<id>` 与 `<ws>/<id>/workspace` 都是 **0770、属主 = 池 uid 10000、组 65534** |
| 冒烟 | `MULTI-NODE SMOKE OK`（4 箱 2+2、命令/文件/stdin、预留归零）；`DEPLOYMENT SMOKE OK`（含跨节点迁移保文件、远端卷隔离、模板构建→worker 拉取→rootfs、MCP 网关） |
| 未回归 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行**；9 个 pod 全 Running；`GET /sandboxes` = 0、两 worker 的 `workspaces`/`_runtime` 无沙箱残留 |
| 仓库侧 | `tests/unit` 与基线逐条相同（2170 passed / 12 skipped / 3 failed —— 3 条 macOS 固有，见 §7.9 起的口径） |

**结论（诚实记）**：worker 那一段砍掉约 95 ms，但**新那一跳自己就要约 71 ms**，省下的与付出的
大致相等，端到端 p50 191 → 193 ms。设计与计划里"省掉那一圈往返 ⇒ 145–150 ms"的预期**没有实现**：
§7.26 记的那 71 ms 里，**大部分不是"一圈的固定开销"，而是 agent 在 NAS 上做递归 `lchown`
本身的成本**。把同一件事从"经控制面转发"改成"控制面直送"，只是换了发起者，活一点没少
（空指令同一跳 1.8 ms、同一棵树的 `rm` 22.6 ms 是旁证）。

**要真正拿掉这 71 ms，得动的是"改属主"这件事本身**，不是谁发指令：例如把沙箱对树的权限
从 ownership 换成组位/ACL（worker 与沙箱同组写入），或让树在创建时就以目标 uid 落盘
（今天做不到：写入者只有 root 或 worker）。那是新设计，不在本轮。

**边界（点名）**：**只有建箱**走这条指令。`_provision_remote` 另外三个调用点不带这个标志、
仍走老路（worker 自建自交）：迁移（`control_plane/api/sandboxes.py:2460`、`:2536`）与
fork（`control_plane/api/snapshots.py:943`）——所以迁移那一跳里仍能看到
`fileop:chown-workspace`（本次实测 58.8 ms 一条）。`local://` 形态与 `_provision_local` 一行没动。

**遗留（本轮独立评审打穿、尚未修）**：① `materialized` 在"控制面没能发出指令"的形状下
仍被写死为 `True`（`_materialize_remote` 的两个提前返回被忽略）⇒ 那些形状会**静默建出没有
workspace 的沙箱**（已用夹具复现；出厂 k8s 清单 pin 了 `runAsUser/runAsGroup: 65534`，
所以本集群不在这个形状里）；② `_CreateClaim` 只活在**单个副本**的内存里，而控制面是
`replicas: 2` ⇒ §4.5 那个窗口只在同副本内关闭（已用两个 app 共享一个 registry 复现：
DELETE 在另一副本上 204 返回、在飞的建箱随后把记录写回去）。三条 Important：新 CP + 旧 agent
的滚动窗口会让建箱 502（无降级）、`E2B_PER_SANDBOX_UID=false` 的旧形态每个远端建箱 503、
agent 侧的具名"忙"（超过 `E2B_C3_AGENT_MATERIALIZE_MAX`）会让那次建箱直接失败。
逐条出处见 `docs/open-issues.md` N56。

**复跑**：

```bash
# ① 建箱延迟（控制面内）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - --base http://127.0.0.1:3000 \
    --key "$E2B_API_KEY" --n 10 < deploy/scripts/acceptance/create_latency_probe.py
# ② 那新一跳自己多贵（控制面 pod 内直打 agent 的维护面；需要 agent token 与一个池内 uid）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - "http://<agent-pod-ip>:49986" \
    "<nodeName>" "$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_C3_AGENT_TOKEN}' | base64 -d)"
# ③ 逐段（开着 trace 跑一次 ①，再关掉）
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE=1
kubectl -n sandlock logs e2b-worker-0 --since=5m | grep "create trace:"
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE-
```

### 7.28 两跳并发（(b)）+ 一次把建箱全打没的回归（**2026-10-01，已上线 `0.1.0-877-ge15b77f-20261001-231822`**）

提交 `af687cf`（(b) 实现）+ `befa910`，上线的是 `e15b77f`。规范
`docs/superpowers/specs/2026-10-01-create-path-grant-design-v2.md`（§4.6 并发预算），
计划 `docs/superpowers/plans/2026-10-01-create-path-grant-finish.md`（Task B 定形状、Task C 实现、
Task E 上线）。

**改了什么**：建箱从"先材料化、再拨 worker"的**串行**改成两跳**并发**。控制面同时发出
agent 的 `materialize` 与 worker 的 `phase: "prepare"`，两条腿都回来之后再发一条
`phase: "finalize"`。worker 那半拆成三相：

| 相 | 内容 | 为什么在这相 |
|---|---|---|
| `prepare` | `.creating` 标记、uid 认领、`statfs` 记账种子 | 全都**不需要树**，所以能和材料化同时开始 |
| `finalize` | 建树（仅未材料化时）、卷配额 + 挂载视图、树的项目配额、属主收口、`register` | 树依赖的部分只能等 |
| `cancel` | 还 uid、删记账种子、摘标记 | 材料化失败/超时/内容被拒时的撤销；**已注册的沙箱绝不碰** |

收口信号由 Task B 的实测定：**控制面补一条指令**（0.75 ms）而不是 agent 写完成标记
（写 12.9 ms + worker 首次命中 6.8 ms，还会在沙箱树里留文件）。契约不变 —— 仍是同步：
材料化失败或超时 ⇒ 建箱失败，且 worker 那一半在返回之前被收干净（超时是**具名 504**）。
降级路（不带 `materialized`：旧 CP、迁移、fork）逐字不变，走同一次"prepare 然后 finalize"。

**集群验收（`0.1.0-877`）**

| 判据 | 读数 |
|---|---|
| 建箱、控制面 pod 内发起（n=10，三轮） | p50 **127 / 128 / 130 ms**（p95 171 / 139 / 147，mean 133 / 131 / 134）—— §7.27 同口径 **193–195 ms**、§7.26 基线 **191 ms** ⇒ **省下约 65 ms（约三分之一）** |
| 同一时刻从本机**经入口**发起（n=10） | p50 **154 ms**（p95 161）—— 比控制面内多出约 25 ms，就是"客户端到入口"那一段（§7.27 时代同口径 330 ms，即减掉当时的平台时间 0.19 s 也是约 0.1 s；入口那一段本身这轮没动） |
| 逐段（`E2B_CREATE_TRACE=1`，按沙箱归属） | `prepare` **72–74 ms**（与 agent 的 `materialize` 同时跑，所以不叠在关键路径上）、`finalize` **7.8–8.4 ms**、`prime` **5.3–5.8 ms**、`record` **47–49 ms**（延迟落盘，不在响应路径） |
| 建箱路径上的 `fileop:*` | **0 行**。20 分钟窗口里 `fileop:chown-workspace` **0 行**（§7.26 记的那 71 ms 那一条彻底不在建箱路径上）；窗口里其余的 `fileop:*` 是别的阶段：`remove-workspace`/`remove-runtime` 各 13 行（= 13 次 DELETE），`walk-workspace` 637 行（每个沙箱每 10–15 s 一次的盘上用量记账，`AgentFileOps.walk_workspace`，与建箱无关） |
| 快照建箱（与 plain 分开报，Task B 的告示） | `deploy/scripts/acceptance/snapshot_create_probe.py`：**1 个文件 p50 215 ms（n=3）**、**40 个文件 p50 1247 ms（n=2）** ⇒ **≈26 ms/条目**，与 Task B 的 25 ms/条目吻合。**两跳并发动不了它**：省下的约 78 ms 摊在 1.2 s（40 文件）上是 6%，摊在 Task B 的 202 条目 5.2 s 上是 1.5% |
| 快照落点（生产形状，v1 的坑） | 快照里 `workspace/kept.txt` ⇒ 新箱读到 `'kept\n'`；`workspace/workspace/kept.txt` ⇒ **`FileNotFoundException`**（v1 曾把内容多下沉一层） |
| 冒烟 | `MULTI-NODE SMOKE OK`（4 箱 **2+2**、命令/文件/health/stdin、kill 后两边预约归零）；`DEPLOYMENT SMOKE OK`（命令+文件、**跨节点迁移保文件**、网络配置、远端卷+兄弟卷隔离、模板构建→registry push→worker pull→rootfs、MCP 网关） |
| 未回归 / 无残留 | `DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行**（除服务端自增的 `generation`）；9 个 pod 全 Running；`GET /sandboxes` = `[]`；两 worker 的 `workspaces/` 只剩 `_migrate`/`_snapshots`、`state/_runtime/` 只剩 `.checkpoints` |
| 仓库侧 | `tests/unit` **3 failed / 2194 passed / 12 skipped** —— 3 条与基线逐条相同（macOS 固有，见 §7.9 起的口径）。⚠ 跑之前要 `env -u all_proxy -u http_proxy -u https_proxy`：带着代理变量会多出 53 条 httpx 连接类假失败 |

**⚠ 这一次上线中间出过一次全站故障，记在这里免得重演。** 第一版上线的是 `0.1.0-876-gbefa910`，
它带着 C2 的记录比对（`_record_is_still_ours`）：**每一次建箱都返回 409**
`{"message": "Sandbox sbx_… was deleted while it was being created"}`，一条沙箱也建不出来。
根因是这个比对拿内存里的 `started_at`（微秒）去比从共享存储读回来的那条 —— 而
`to_storage_dict` 是用 `to_iso_z` 写的、**只到毫秒**，所以除了正好落在整毫秒的 1/1000，
比较**恒为 False**（实测 200 次里 0 次精确往返）。**当场回滚到 `0.1.0-864` 恢复服务**，
修在 `e15b77f`（改比"存储看得见的身份"：`to_iso_z(started_at)` + `envd_access_token`），
再上线成 `0.1.0-877`。

**为什么单测没抓住**：`tests/unit/test_cp_create_delete_window.py` 里**每一条期待建箱成功的用例**
都把 registry 建在**没有 `record_store`** 的形状上 —— 那种形状下 `get` 把 create 自己那个对象
原样还回来，截断根本不发生；唯一用共享存储的那条用例断言的是 **409**，而坏代码恰好也
给 409。补的两条钉子（`test_the_shared_check_compares_the_stores_encoding_not_this_process_memory`、
`test_a_plain_create_survives_a_store_that_encodes_the_record`）在还原成旧比较时都会红，
已逐条验过。**教训**：一个"从存储读回来再比对"的检查，必须至少有一条走**会编码的**存储的用例。

**§4.3.1 四条硬要求各自由谁承接**（快路/降级路共用同一份材料化实现）：

| 要求 | 用例 |
|---|---|
| 源侧不解引用符号链接 | `test_a_symlink_in_the_snapshot_is_recreated_not_followed` |
| 目标侧具名拒绝 | `test_a_symlinked_destination_segment_is_refused_named` |
| 半棵树不报成功 | `test_a_partial_copy_is_reported_as_failure` |
| 迁移保文件 | `deployment_smoke.py` 的 "migrated … files kept"（本次实跑已过） |

另有本轮新增/改动的钉子：两跳并发的四条（`test_the_worker_starts_while_the_tree_is_still_being_made`、
`test_a_failed_materialization_fails_the_create_and_undoes_the_worker`、
`test_a_slow_materialization_is_bounded_and_named`、`test_a_plain_create_still_takes_the_old_path`）、
C2 的跨副本（`test_a_delete_on_the_other_replica_does_not_resurrect_the_record` + 上面两条）、
既有树里每个目录都到 0770（`test_a_leftover_directory_is_given_the_tree_mode`）。

**复跑**：

```bash
# ① 建箱延迟（控制面内；三轮）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - --base http://127.0.0.1:3000 \
    --key "$E2B_API_KEY" --n 10 < deploy/scripts/acceptance/create_latency_probe.py
# ② 逐段（开着 trace 跑一次 ①，再关掉；看的是 create 自己的相：prepare/finalize/prime/record）
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE=1
kubectl -n sandlock logs e2b-worker-0 --since=5m | grep "create trace:" | sort | uniq -c
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE-
# ③ 快照建箱（单独报；--files 放大拷贝那一段）
E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 \
    E2B_API_KEY="$E2B_API_KEY" tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_create_probe.py --n 3 --files 1
```

### 7.29 N58：根重切上线（`_snapshots` 合一、`_migrate` 上浮）（**2026-10-02，已上线 `0.1.0-887-g7ef319b-20261002-100406`**）

计划 `docs/superpowers/plans/2026-10-02-local-first-create.md` 的 Task 0 Step 5–8；
介质归属的实测与逐条评估在 `docs/create-local-first-layout.md`。**这一轮不翻介质**：
树仍在共享盘上，行为与重切前逐字相同，它只把"谁挂在哪个根下"写清楚 —— 为 Task 3
（树搬节点本地）清出那一步。

**三个提交**：`308b543`（Step 4 代码：平台命名空间根 helper、第五根 `E2B_NODE_STATE_BASE`、
迁移判据 `E2B_TREES_SHARED`）、`4ac410b`（Step 5+6：清单 + N58 迁移阶段）、
`7ef319b`（上线当场抓到的 `copy_from` 回归，见下）。前两个**必须同一次上线**：代码已经把
`_snapshots`/`_migrate` 的根改到共享根，而线上数据还在旧位置。

**窗口里多了一件事：先关自动扩缩器。** `E2B_AS_ENABLED=true` + `E2B_AS_MIN_REPLICAS=2`
的 autoscaler 就在控制面里，它的第 2 步**无条件**把副本数拉回 floor
（`autoscaler/loop.py:124`，5 秒一次 tick，`scale_to` 直接 patch `statefulset/e2b-worker`
的 scale 子资源）。只 `scale --replicas=0` 会在迁移 Job 跑的中途被拉回来 —— 那正是
「worker 起来了、旧位置的树与新位置对不上」。做法：`kubectl -n sandlock set env
deploy/control-plane E2B_AS_ENABLED=false` → 等 rollout → 再缩 worker；`apply.sh`
随后把清单里的 `true` 写回去。**N27 那轮没有这个坑（autoscaler 2026-09-30 才并进控制面）。**

**执行顺序与读数**

```
kubectl -n sandlock set env deploy/control-plane E2B_AS_ENABLED=false   # 关掉抢副本的那个循环
kubectl -n sandlock scale statefulset/e2b-worker --replicas=0           # 停写
deploy/scripts/migrate-state-base.sh                                    # 只读计划（脚本自己的闸门：worker=0）
deploy/scripts/migrate-state-base.sh --apply                            # ConfigMap + Job，跑完自动清理
deploy/k8s-k0s/apply.sh                                                 # 新清单；先把 agent DS 滚完再滚 worker
```

| 判据 | 读数 |
|---|---|
| 迁移计划（dry-run，经控制面 pod） | `moves=9 dirs=3 todo=14 unknown=0`：1 条上浮 + 2 条整 id 合一 + 3 条逐条合一（6 个条目）+ 3 个空壳 rmdir；**没有一条"两边同名"** |
| 迁移执行（Job） | `SUMMARY mode=apply moves=9 dirs=3 todo=14 done=14 unknown=0`；逐条 `VERIFY … same_inode=yes`、`src_gone=yes`；12 条 `SAMPLE … sha_same=yes`；journal `state/.state-base-migration.journal` 0600 1628 B；Job/ConfigMap 无残留 |
| 卷上形状（控制面 pod 内只读复核） | `<export>/_snapshots` **8 个 id**：`46dc`/`4bf1`/`ce90`（记录+载荷合一）、`962c`（重切前就完整的那个）、`2bb1`/`a6e1`（**只有载荷**）、`015f`/`1ca5`（**只有记录**）；`<export>/_migrate` 1777 nobody；`<export>/workspaces` **空**（`_snapshots`/`_migrate` 都不在了） |
| 建箱 p50（控制面内，n=10，两轮） | 上线前 **124 ms**（p95 137）→ 上线后 **135 / 136 ms**（p95 180 / 186）。逐段（`E2B_CREATE_TRACE=1`）**与 §7.28 逐条相同**：`prepare` 75–76、`finalize` 7.7–8.0、`prime` 5.3–11.3、`record` ≈51。⇒ 没有结构性回归；差的那 10 ms 是刚滚完的舰队的冷缓存（§7.28 的 127–130 是跑了很久的集群上的数） |
| 快照建箱（新快照） | `snapshot_create_probe.py --n 3 --files 1`：**p50 208 ms**，`kept='kept\n'`，`workspace/workspace/kept.txt` = `FileNotFoundException`（v1 的坑不在了） |
| **被迁移过的老快照**恢复 | `restore_snapshot_probe.py` **4/4**：`46dc`/`4bf1`（逐条合一，内容 `kept\n`）、`ce90`（2000 文件，`lease/f0000.bin` 512 B）、`962c`（2000 文件，`big/f0000.bin` 512 B） |
| 冒烟与残留 | 9 个 pod 全 Running；`GET /sandboxes` = `[]`；`statefulset/e2b-worker` 2/2；`DRY_RUN=1 apply.sh \| kubectl diff -f -` **0 行** |

**⚠ 上线当场抓到的一次回归（已前滚修复）。** 迁移 + 新清单上线后，`snapshot_create_probe.py`
立刻报

```
502: the agent for node e2b-worker-0 refused the materialization: partial-copy:
the snapshot source /var/lib/e2b-sandboxes/workspaces/_snapshots/snap_7ebb…/fs
is not a directory
```

根因：`control_plane/file_ops.py::derive_materialize` 仍然从**树根**推导快照的复制源
（`<workspace_base>/_snapshots/<id>/fs`），而 N58 已经把载荷合到 `<export>/_snapshots/<id>/fs`。
于是"从快照建箱"**全部** 502 —— 建箱（plain）、建快照、删快照都不受影响，所以只有真的
恢复一次才看得见。计划里 `control_plane/file_ops.py` 在 Task 0 的文件清单内，但 Step 4 只改了
`node_state_base` 那一半；`derive_materialize` 的 `copy_from` 形状被记在 Task 2（改成指向 tar）
名下，"指向合一后的 `_snapshots`"这一步从没写进任何 Step。

修在 `7ef319b`：`copy_from` 改走 `gateway_common.paths.snapshot_payload_dir(…,
shared_root=paths.shared_volume_root)`（与 agent 写载荷同一个 helper），钉子
`tests/unit/test_cp_materialize_instruction.py::test_a_snapshot_create_carries_copy_from`
的 fixture 把"树根"与"平台命名空间根"做成两个不同目录，改之前红的正是那条 502 路径。
重建镜像（`0.1.0-887`）→ apply → 上面两个探针全绿。

**复跑**

```bash
# ① 迁移的本机彩排（不连集群；造出 N27 之后的形状，跑到回退）
tmp/venv/bin/python deploy/scripts/acceptance/migrate_state_base_rehearsal.py

# ② 建箱延迟（控制面内；n=10）
kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - \
    --base http://127.0.0.1:3000 --key "$E2B_API_KEY" --n 10 \
    < deploy/scripts/acceptance/create_latency_probe.py

# ③ 新快照的往返
E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 \
    E2B_API_KEY="$E2B_API_KEY" tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_create_probe.py --n 3 --files 1

# ④ 被迁移过的老快照（每条检查一个进程，见工具自己的说明）
E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 \
    E2B_API_KEY="$E2B_API_KEY" tmp/venv/bin/python \
    deploy/scripts/acceptance/restore_snapshot_probe.py \
    --check 'snap_46dc467759dbbfb7:workspace/kept.txt:kept\n' \
    --check 'snap_ce90ef9852fc6809:lease/f0000.bin'
```

**回退窗口（仍然开着）**：`state/.state-base-migration.journal`（0600）+ 
`deploy/scripts/migrate-state-base.sh --rollback --apply` 原路退回。要注意**回退之后必须
同时回退镜像**：`308b543` 之后的代码只认新位置。

**这一轮没有解决的**（都在计划里）：`2bb1`/`a6e1` 有载荷没记录、`015f`/`1ca5` 有记录没载荷 ——
这是 N27 下沉时留下的**数据**问题（记录是控制面在快照成功之后写的），迁移按设计原样留着并
具名报告，没替它们编一份；Task 2 会把载荷换成 `fs.tar`、Task 3 才把树搬去节点本地盘。

### 7.30 Task 2：快照载荷打成 `fs.tar`（**2026-10-02；舰队当前版本 `0.1.0-900-g0079c84-20261002-161409`（Task 4 见 §7.31）**：本节那张验收表量的是先上线的 `0.1.0-892-g52044b8-20261002-125656`，895 带上读侧守卫修复后的重测见"上线后发现的一条"，Task 5 的目录链前后对照见本节末）

计划 `docs/superpowers/plans/2026-10-02-local-first-create.md` 的 Task 2；产品设计、两笔账与
逐条裁定在 `docs/create-local-first-design.md` §3.2/§6。**这一轮只换载荷的容器**：一个快照
从"一棵爆炸式 `fs/` 目录"变成"一个 `fs.tar`"，**读侧两种形状都收**（线上四个可恢复的老快照
全是 `fs/`）。树、记录、卷都不动。

**四个提交**：`893da7e`（Task 2 本体）、`52044b8`（评审轮 1 的两条 Important：store 探针认
tar、解包内存账说准）、`942c5bd`（**只动读侧守卫**的成本修复 + 本节这张验收表的落地）、
`c478ca0`（`mb_per_s` 的分子口径，只改文档）。**下面这张验收表量的是 `52044b8`**（即
`0.1.0-892`）；`942c5bd`/`c478ca0` 之后舰队滚到 **`0.1.0-895-gc478ca0-20261002-134406`**，
带守卫修复重测的 202 档见"上线后发现的一条"。

**上了什么**

| 面 | 位置 | 形状 |
|---|---|---|
| 写 | `envd_service/agent.py::_write_snapshot_tar` | 整棵树一个 tar；临时名 → `fsync` → `rename` → `.complete` 最后；N29 的幂等判据改成"`fs.tar` 或老 `fs/` 已存在" |
| 读（主路） | `c3_agent/materialize.py::_take_snapshot_payload` | tar ⇒ 逐成员解包进**树根**；目录 ⇒ 原合并；tar 缺席而兄弟 `fs/` 在 ⇒ 回落 |
| 读（降级路） | `envd_service/agent.py`（控制面没材料化时 worker 自己建树） | 同上两条腿 |
| 推导 | `control_plane/file_ops.py::derive_materialize` | `copy_from` = `…/_snapshots/<id>/fs.tar` |
| 加固（唯一实现） | `gateway_common/archive.py` | 成员过滤（绝对链接成员丢弃）+ `dest` 包含检查 + 截断/特殊文件具名拒绝；控制面与两个 agent import **同一个函数对象** |
| 只读 store 探针 | `local_first_snapshot_verify.py`、`local_first_capacity_account.py` | 按 `gateway_common.paths.snapshot_payload` 认载荷，每行多 `payload_shape`（不改会把每个新快照报成 "record only"） |

**验收（2026-10-02，`0.1.0-892-g52044b8`，上一版本 `0.1.0-887` 是前测基线）**

| 判据 | 命令 | 读数 |
|---|---|---|
| 一卷上的形状 | `snapshot_create_probe.py --files 1,40,202 --n 3 --keep` + 卷上 `ls`/`stat` | 三档都是 `fs.tar` + `snapshot.json` + `.complete`，**没有** `fs/`；tar 成员是 `workspace/…`；字节 **10 KiB / 90 KiB / 410 KiB**（旧 `du -s`：5 / 161 / 809 KiB ⇒ 202 档**占块减半**） |
| 三档成本（后测） | 同上 | 捕获 p50 **157 / 478 / 1817 ms**（旧 180 / 1614 / 8051）；建箱 p50 **263 / 1426 / 6984 ms**（旧 248 / 1190 / 5725）；每条目 **131.6 / 34.8 / 34.4 ms**（旧 124.2 / 29.0 / 28.2）；`create_attempts` 3 / 3 / 3（本轮 202 档**没有**撞到两副本窗口）。探针的 `mb_per_s` 用**树内容字节**做分子（三档 5 B / 385 B / 2106 B），所以这三档都打印 `0.000` —— 字节口径看载荷本身（上一行），`mb_per_s` 要到 MB 级树才有意义 |
| 新快照往返 | `snapshot_tar_roundtrip_probe.py --modes` | `failures=0`：`workspace/kept.txt`=`kept\n`、`workspace/deep/nested.txt` 在、`workspace/workspace/…` **不存在**、链接仍是链接、目录 **0770**、文件 **0644**；`chmod 664/777` 的文件恢复成 **644/755** |
| fifo | `snapshot_tar_roundtrip_probe.py --fifo --expect-fifo-refusal` | 捕获**成功**、恢复**具名拒绝**（`502 … partial-copy: …/snap_…/fs.tar`）—— 旧读侧在 fifo 上会永久阻塞 |
| 老 `fs/` 快照 | `restore_snapshot_probe.py --check …`（四个 id，每条一个进程） | **4/4**：`46dc`/`4bf1` = `workspace/kept.txt` `kept\n`、`ce90` = `lease/f0000.bin` 512 B、`962c` = `big/f0000.bin` 512 B；`workspace/workspace/…` 四条都不在 |
| 舰队状态 | `kubectl -n sandlock get pods`、`GET /sandboxes` | 9 pod Running、`GET /sandboxes` = `[]`、卷上回到原来 **8 个 id**、`<export>/workspaces` 空 |
| 单测 | `tmp/venv/bin/python -m pytest tests/unit -q` | `3 failed / 2243 passed / 12 skipped`（三条红是基线 macOS-only） |

**两笔账与那条"越深越慢"**：捕获 ×3–4、占块减半；建箱慢 ~20%。根因不是 tar，而是
**读侧的包含检查**：沙箱内同一棵 203 条目的树、同一个 NAS 上，旧读侧 `copytree` 13.0–13.4 s，
stdlib `extractall(filter="data")` 11.5 s、本仓解包器 11.7 s（目的地深度 1）；把目的地埋到
生产深度（5 段）后 `copytree` 仍 13.0 s，而两个 tar 读法变成 **16.0 s** —— `tarfile` 的
`data` filter 对**每个成员**做一次 `realpath`（逐组件 `lstat`），成本随路径深度走，
`copy_tree` 的每条目成本与深度无关。**Task 3 把树搬到节点本地盘后这条自然消失**（同一次
`lstat` 从 NAS 的 4.4 ms 掉到 0.002 ms），详细账见设计文档 §3.2。

**上线后发现的一条（`0.1.0-895-gc478ca0-20261002-134406`，已上线）**：读侧守卫**第一版对
每个成员多调一次 `Path.resolve()`**（同样是逐组件 `lstat`）——沙箱内实测 13.0 s vs stdlib
11.5 s（+1.5 s/203 条目 ≈ +7 ms/条目），正是"上线后建箱慢 ~20%"里**属于本任务**的那一半。
已改成"每个目录检查一次并缓存"（11.7 s，与 stdlib 持平，且比旧读侧 `copytree` 还快），用例
`test_the_parent_chain_is_checked_once_per_directory` 钉住；**这个修复现在在线上**。

**在 895 上重测 202 档**（`snapshot_create_probe.py --files 202 --n 3`，2026-10-02）：
`capture_ms=1703`、`create_p50_ms=6162`、`per_entry_ms=30.354`、`capture_per_entry_ms=8.389`
（n=3）。对照上面那张表（892，慢守卫）的 **34.4 ms/条目** 与老 `fs/` 形状的 **28.2 ms/条目**：
守卫那 +7 ms/条目基本收回（34.4 → 30.4），但 tar 读侧**仍比老形状贵 ~2 ms/条目** —— 差价与
本节"两笔账"里那条同源（`tarfile` 的 `data` filter 对**每个成员**各做一次逐组件 `realpath`，
这条不在缓存能覆盖的范围内），随 Task 3 把树搬到本地盘一起消失。表里其余档位仍是 892 的读数，
没有在 895 上重跑。

两副本的记录不一致（§4.3）本轮又撞到两次：`create_snapshot` 返回后另一个副本短暂
`400 Template … not found`（前测那轮记到 `create_attempts=6`），以及 `delete_snapshot`
只摘当次副本的载荷目录 —— 都按副本各删一次清干净。

**没做的（明说）**

* **按字节上限**（`E2B_TREE_COPY_MAX_BYTES`）与拷贝窗口：brief 没收这一步，按设计文档 §3.1
  的表归 **Task 3**（它的措辞是树的淘汰上限）。成员数上限（tar 索引 ~430 B/成员）同归 Task 3。
* **§4.3 的两副本记录失效**：裁定只观测、不改，本轮照办。
* **读侧守卫的缓存修复**：`942c5bd` 已提交、**已在 `0.1.0-895` 上线**，202 档重测就在
  "上线后发现的一条"里（34.4 → 30.4 ms/条目）；本节那张三档表**没有**在 895 上重跑。
* 老快照那一轮**没有**再出现空 body（§7.29 记的"两条 2000 条目连跑空答"）；一次都没撞到，
  所以它是"这次没复现"，不是"已修"。

**Task 5（同一条路上的另一个病灶：建箱要走的目录链，`9a562ba`，2026-10-02 随
`0.1.0-900-g0079c84-20261002-161409` 上线）**

`c3_agent.materialize` 每要一个目录就从 `/` 逐段打开，而**共享 NAS 上每个组件是一次元数据往返**；
一次建箱要**同一棵树的链 2–3 次**（树根的 `fchmod`、`<root>/workspace`、树已存在时的 leftover
mode pass、`fs.tar` 落完之后的树根 mode pass）。Task 5 给一次 materialize 一个链缓存：从最长的
已缓存祖先起走，**没走过的组件仍走同一个**逐组件 `O_NOFOLLOW` 检查（末尾组件每次都重新开，
叶子永远是新验的）。

判据（`tests/unit/test_agent_materialize.py::test_the_dir_chain_does_not_walk_from_the_root_every_time`）：
一次 materialize 里 `open("/", O_DIRECTORY)` 恰好 **1** 次；三个形状（fresh / 树已存在 /
从 `fs.tar` 建箱）在本任务之前是 **2 / 3 / 3**（把调用换回 `_open_dir_chain` 直接变红）。
集群读数来自 `deploy/scripts/acceptance/dir_chain_cost_probe.py --n 20`（只读：全是
`open`/`fstat`/`close`；建/杀沙箱走公开 API；对它自己建的那个真沙箱在生产路径上的树计时，
两个 agent pod 各一份，p50 ms）：

| 判据（链的成本） | `0.1.0-895-gc478ca0`（**Task 5 未上线**：量的是线上 `_open_dir_chain`） | `0.1.0-900-g0079c84`（**Task 5 已上线**：量的是发货的那份模块） |
|---|---|---|
| 一棵树的链走一次 | 6.66 / 6.85 | 6.54 / 6.84 |
| 建箱（无载荷）：链走两次 | 15.47 / 15.91 | 15.24 / 16.01 |
| **从 `fs.tar` 建箱：链走三次 → 一次** | **22.15 / 22.94 → 11.08 / 11.37** | **21.71 / 23.02 → 10.87 / 11.54** |
| 每组件 | 1.33 / 1.37 | 1.31 / 1.37 |

* 895 那一列量的是**还没有链缓存**的线上 `_open_dir_chain`；900 那一列的**前半份**（after）是
  **发货模块本身** —— 探针会打 `candidate_is_the_shipped_module=True`（candidate 与
  `/app/c3_agent/materialize.py` 的 sha256 都是 `b72c571d…`，两节点一致）。900 的**后半份**
  （before）是探针把老走法**照原样拼出来**的（线上模块里已经没有它了），读数落在 895 那一列上：
  两次运行的 before 互为对照。
* **Task 4（§7.31）在这里看不出来**：它搬的是 `prepare` 的芯片（节点本地 state），而本探针只碰
  `<export>/workspaces/...` —— 树根、目录链、NAS 挂载都没动。两次运行的差 ≤3%，是抖动，
  不是 Task 4 的功劳也不是它的代价；同一台机器上两次 `before` 也差这么多。
* 每次建箱省 ~11 ms；同一张表里 202 档从快照建箱 p50 = **6162 ms**（895）⇒ 这 ~11 ms 是那条
  create 的 **0.18%**。它是**树还留在共享 NAS 的形状**（`E2B_TREES_SHARED=1`）下 Task 3 的
  **补救、不是替代**：树搬到节点本地盘之后，剩下的那一次走（每组件一次本地 dentry）自然消失。
* brief 里记的"每棵树两次 ≈ 17.4 ms"落在上面"链走两次"那一行的分布里（p50 15.2–16.0、max
  到 21）；它是"逐组件往返 × 组件数"，不是常数。

**复跑**（全部在 `deploy/scripts/acceptance/`，不指 `tmp/`）

```bash
deploy/scripts/open-cluster-tunnel.sh          # 通道 + 身份自检（2 节点 / arm64 / +k0s）
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)

# 三档（前/后对照，两个口径）+ 载荷形状
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_create_probe.py --files 1,40,202 --n 3 --keep

# Task 5：建箱的目录链（一棵树从 / 走几次；老走法 vs 链缓存）—— 只读判据，见本节末
# （刚 rollout 完先手工建一个箱预热，否则第一个 create 会因 worker 重新预热镜像而超时）
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/dir_chain_cost_probe.py --n 20

# 形状那一腿：往返 + 模式夹取（--modes）、fifo（--fifo）
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/snapshot_tar_roundtrip_probe.py --modes

# 老快照那一腿（每条一个进程）
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/restore_snapshot_probe.py \
    --check 'snap_46dc467759dbbfb7:workspace/kept.txt:kept\n' \
    --check 'snap_4bf1225dfbf54e0c:workspace/kept.txt:kept\n' \
    --check 'snap_ce90ef9852fc6809:lease/f0000.bin' \
    --check 'snap_962c14802d6cbd50:big/f0000.bin'
```

**回退**：代码只认"两种形状都收"，所以回退镜像**不需要**回退数据 —— 老 `fs/` 快照在任何
一侧都能恢复；反过来，`fs.tar` 的载荷只被新读侧认，回退到 `0.1.0-887` 之前的镜像会让这些
新快照**不可恢复**（记录在案，别只回镜像不停手）。

### 7.31 N57（Task 4）：本节点 state 分家（**2026-10-02 已上线 `0.1.0-900-g0079c84-20261002-161409`**）

计划 `docs/superpowers/plans/2026-10-02-local-first-create.md` 的 Task 4；介质归属、四个
小件的清单与三条裁定在 `docs/create-local-first-design.md` §2.3/§2.4/§5，那一行的目标值在
`docs/create-local-first-layout.md` §1。本节的读数在**Task 5（`9a562ba`）+ Task 4
（`7a90293`/`91ce91d`/`0079c84`）**的镜像上实测；**Task 3（树本地化）没上**，所以这里量到的
是"prepare 不再当长杆"而不是整条的收益（见下面的读数表与"结论"）。

**改了什么**：`prepare` 原先在共享 `E2B_STATE_BASE` 上写三样小东西 —— `.creating` 标记、
uid 认领（`.uid_pool.lock` + `.uid_reservations/`）、`disk-stats` 种子。共享卷是 NFS，一次
元数据往返 ~13 ms（§1.1），这就是 Task 1 量到的 `prepare` **72–76 ms** 的来源。Task 4 把
它们（连 `.route-b/**` 一起）搬到**节点本地**的 `E2B_NODE_STATE_BASE`（k8s hostPath
`/var/lib/e2b/state`，与 `/var/lib/e2b-images` 同一块盘），记录 `_runtime/<id>/sandbox.json`
与 `.checkpoints/**` 留共享 —— 记录是**舰队级 uid 账本的索引**，Task 4 顺手把那个索引从
"枚举树目录名"改成"枚举共享记录目录"（`uid_pool._recorded_uids`；树本地化之后前者只看得见
一个节点）。`E2B_NODE_STATE_BASE` 不设 = 逐字节回到今天（compose / 测试 / `local://` 都不设）。

**上线顺序**：`deploy/k8s-k0s/apply.sh` 先把 `e2b-c3-agent` 滚完（它的 `workspace-root-init`
建 `/var/lib/e2b/state` 并 `chown 65534`），再滚 worker；控制面那条 `E2B_NODE_STATE_BASE`
只进根白名单、不挂卷。`kubectl -n sandlock get pod -l app=c3-agent -o wide` 全 Ready 之后再
看 worker。

**判据与命令**（全部只读或公开 API 建/杀沙箱）：

```bash
deploy/scripts/open-cluster-tunnel.sh && export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"

# ① prepare 那一跳（HTTP 边界）。⚠ 这个探针的 payload 不带 hostUID ⇒ 走的是**回落**形状
#    （acquire：多写锁 + 预留标记，还多读一次共享的舰队账本索引 `_recorded_uids`）；
#    Task 1 的 72–76 ms 与本次的 trace `prepare` 都是**已部署**形状（带 hostUID、走 claim，
#    不碰锁/标记/索引）。所以①只能读作"回落形状的 prepare 跳有多快"（实测 p50 18.8 ms），
#    **不是**"掉了多少"，也不是（同形状下）prepare 本身的耗时——"掉了多少"看 ② 的 trace。
CP=$(kubectl -n sandlock get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    python3 - "$E2B_API_KEY" "$E2B_INTERNAL_API_KEY" --n 10 \
    < deploy/scripts/acceptance/prepare_phase_cost_probe.py

# ② 整条建箱的 p50 + 逐段（看长杆有没有换成 materialize —— 那是 Task 3/5 的腿）
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
    deploy/scripts/acceptance/create_latency_probe.py \
    --base http://172.18.78.49:3000 --key "$E2B_API_KEY" --n 10
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE=1
kubectl -n sandlock logs e2b-worker-0 --since=5m | grep "create trace:" | sort | uniq -c
kubectl -n sandlock set env statefulset/e2b-worker E2B_CREATE_TRACE-   # 用完就关

# ③ 四个小件真的落在节点本地、记录真的还在共享（两个 worker 各看一次）
kubectl -n sandlock exec e2b-worker-0 -- ls -la /var/lib/e2b/state /var/lib/e2b/state/_runtime
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    sh -c 'ls -la /var/lib/e2b-sandboxes/state/_runtime | head'
```

**实测（2026-10-02，`0.1.0-900-g0079c84-20261002-161409`，控制面 pod 内 `--n 10`，先预热）**

| 判据 | 改前（§7.29/§7.30 读数） | 改后 | 结论 |
|---|---|---|---|
| 整条建箱 p50（含预热后的 10 连跑） | 124–135 ms（p95 180–186） | **116 / 117 ms**（两轮；p95 128 / 137；trace 开着的那轮 118） | 只降了 ~8–19 ms —— **没有**按 prepare 省下的量整体下移 |
| trace `prepare`（**已部署形状**：`claim`） | **72–76 ms** | **4.8 / 7.3–7.6 ms**（11 个样本，p50 ≈ **7.4**） | ✅ **长杆没了**：落到 ~10 ms 量级 |
| trace `finalize` | 7.7–8.0 | 8.0–8.9 ms | 没动 |
| trace `prime` | 5.3–11.3 | 3.2–3.5 ms（那一轮的预热样本 50.2） | 没动（略好） |
| trace `record`（不在响应路径上） | ≈51 | 49.7–54.2 ms | 没动 |
| ① prepare 跳（**回落形状**，`acquire`） | ——（未测过） | p50 **18.8 ms**（首样本 58.2 冷启动） | 这条形状比 `claim` 多两件事：写锁/预留标记（现在在节点盘上）+ **读一次共享的舰队账本索引**（按设计必须共享）——所以它不是 ~10 ms，也不该拿来对比 |

**结论（要说清楚的一条）**：`prepare` 从 72–76 ms 掉到 **7.4 ms**，这一节的目标达成；
但整条建箱的 p50 只动了 ~8–19 ms，因为 **worker 那一跳的三段相加现在只有 ~19 ms**
（7.4 + 8.4 + 3.3），而 API 看到的建箱是 ~116–123 ms ⇒ **剩下 ~100 ms 落在 worker 的
trace 之外**：控制面自己的活 + **与 `prepare` 并发的 agent `materialize` 那一跳**
（`c3_agent` 的 `POST …/agent/materialize`，控制面 pod 的访问日志里每次建箱一条）。

那 100 ms 里谁是大头，**我没有直接测到**（控制面侧没有逐段 trace：`create_trace` 只存在于
worker/envd）。能给的是一段**算术**，而且它只在"两跳并发"模型下自洽（Task 7.28 的设计）：
记 `m` 为 materialize、`s` 为 worker 三段之和、`c` 为控制面自己的活，
则 `create ≈ max(prepare, m) + s' + c`。改前 `72–76 + 8 + 5..11 ≈ 86–95`，若两跳**串行**
则 `m ≈ 124–135 − 86..95 − c ≈ 25–45`；改后 worker 只有 `≈19`，同一个 `m` 却要求
`m ≈ 116–123 − 19 − c ≈ 97–104` —— **自相矛盾**。⇒ 两跳是并发的（`max`），且
`m ≈ 100 ms` 在两次读数里是同一个值。所以：**prepare 不再是 floor，floor 是 materialize
（~100 ms，仍在共享树根上），Task 3 才是动它的那一步**。这条是推论（有读数支撑），
要坐实得给控制面那侧也加一段逐段 trace —— 本轮不改代码，记在这里。

③ 的形状（判据，实测见下；**2026-10-03 按 N57 更正，见 §7.34**）：节点本地只出现
`_runtime/<id>/.creating`、`disk-stats`、`.route-b/**` 这三样；共享
`<export>/state/_runtime/<id>/` 里**没有** `.creating`、没有 `disk-stats`，只有 `sandbox.json`
/ `command-logs.jsonl`。（**池自己的 `.uid_pool.lock` / `.uid_reservations/` 不在节点本地** ——
N57 已把它们搬回**共享** `E2B_STATE_BASE`，好让 `acquire` 的临界区跨节点互斥；下面第 1 条读数
里它们出现在节点本地，那是 N57 **之前**的 Task 4 形状。）

**③ 三个具体读数（2026-10-02）**

1. **在飞的 `prepare`**（控制面 pod 打 worker-1 的 agent 口，`phase: prepare` 挂 30 s 再看，
   `phase: cancel` 收回）：节点本地 `_runtime/probe4chips082152/` 里 **`.creating`（0 B）+
   `disk-stats`（`1073741824 0`）**，根下还有 **`.uid_pool.lock`（0600, 0 B）+
   `.uid_reservations/probe4chips082152`（`10000`）**（这条形状走 `acquire`，所以四个文件全在
   —— **这两条锁/标记是 N57 之前的落点，现已回共享 base，见 §7.34**）；
   同一时刻共享 base 的 `.uid_pool.lock` 仍是 **09-26 10:02:39**、`.uid_reservations` 仍是
   **10-01 14:20:10**（都是改前的 mtime）、共享 `_runtime` 里没有 `probe4chips*`。`cancel`
   之后节点本地的 `_runtime/<id>` 与预留标记都消失（配对收尾按设计）。
2. **活的沙箱**（`sbx_d396465479a045c6`，跑过一条命令）：节点本地
   `_runtime/sbx_d396…/disk-stats` = `1073741824 1024`（quota 种子，周期扫描在更新它）；
   共享 `_runtime/sbx_d396…/` 只有 **`sandbox.json`(679 B) + `command-logs.jsonl`(213 B)**，
   `.creating` / `disk-stats` 两个 `ls` 都是 `No such file or directory`。删除之后节点本地
   那一份消失、共享那一份随树一起消失。
3. **`.route-b` 落点**（这条是对"清单约定 ≠ 真实落点"那半的直接检验）：在那条命令跑起来之后，
   worker-1 节点本地出现 **`/var/lib/e2b/state/.route-b/10001/sbx_d396…/{policy.json,program.json}`**
   （08:23:04）；共享 `<export>/state/.route-b/` 在最近 30 分钟里**没有任何**新条目
   （`find -newermt "-30 minutes"` 为空；根下最新写入仍是 06:19 的 uid `10000`）。
   ⇒ 清单里的 `E2B_ROUTE_B_TMP_ROOT=/var/lib/e2b/state/.route-b` 是**真的落点**，不是约定。
   （沙箱杀掉之后那对 slot 文档仍留在节点本地 —— 与共享 `.route-b` 的历史行为一致：
  按 `(uid, instance)` 留着，不随沙箱回收。）

**"在飞的 `.creating`"怎么再看一次**：`prepare_phase_cost_probe.py --hold-s` 把每一轮
`prepare` 挂住再 `cancel`（不落记录、不建树），于是四个小件在那几秒里是**能直接 `ls` 到**的
（注意这条形状走 `acquire`，所以锁与预留标记也会在；挂住期间另一个终端看数据）：

```bash
WIP=$(kubectl -n sandlock get pod e2b-worker-1 -o jsonpath='{.status.podIP}')
CP=$(kubectl -n sandlock get pod -l app=control-plane -o jsonpath='{.items[0].metadata.name}')
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    sh -c 'python3 - x "$E2B_INTERNAL_API_KEY" --worker-url '"http://$WIP:49983"' \
           --n 1 --hold-s 25' \
    < deploy/scripts/acceptance/prepare_phase_cost_probe.py &
sleep 10
kubectl -n sandlock exec -i "$CP" -c control-plane -- \
    sh -c 'ls -la /var/lib/e2b-sandboxes/state/_runtime | grep -c probe4_ || true'   # 共享：0
for p in e2b-worker-0 e2b-worker-1; do
  kubectl -n sandlock exec "$p" -- sh -c \
    'ls -la /var/lib/e2b/state/_runtime /var/lib/e2b/state/.uid_reservations 2>&1 | tail -6'
done
wait
```

**本地可先量的机制读数**（不用集群，`deploy/scripts/acceptance/node_state_split_local_probe.py`，
量的是**回落形状**——它的 payload 不带 `hostUID`，所以计数里含 `pool.acquire` 的锁与预留标记）：
一次 `prepare` 落在共享 base 上的路径操作实测是 **`before=14 / after=4`**（**不是 14 → 0**，
2026-10-03 按实测更正，见 §7.34），只读的两次（`_runtime` 列举 + 邻居记录）留着 —— 那就是
舰队账本的索引。剩下那 **4 笔**正是回落 `acquire` 路径上的池文件写（锁 + 预留标记）：**N57 已
把它们搬回共享 base**（跨节点互斥的代价只落在回落形状上；出厂 `claim` 路径 0 笔），所以不是
"共享 base 上的每一笔写都搬走了"。这是**机制**读数，不是延迟读数，也不是"掉了多少"的分子。

**回退**：把 `E2B_NODE_STATE_BASE` 从两份 k8s 清单里去掉（或把 worker/agent 的镜像退回），
`prepare` 立刻回到共享 base 的写法；已经写在节点本地盘上的那几样是**可再生的残渣**
（marker / stats / lock / reservations 都是临时件），不需要数据迁移。

### 7.32 Task 3：沙箱树搬节点本地盘 —— 翻转 + 流式迁移 + 具名错误（**2026-10-02 已上线 `0.1.0-905-g7331364-20261002-174329`，修复随 `0.1.0-908` 生效**）

计划 `docs/superpowers/plans/2026-10-02-local-first-create.md` 的 Task 3；裁定、上限与
验收读数在 `docs/create-local-first-design.md` §8，介质地图在
`docs/create-local-first-layout.md` §1。三个镜像同 tag
（worker / control-plane-gateway / agent，见
`kubectl -n sandlock get statefulset/e2b-worker -o jsonpath='{.spec.template.spec.containers[0].image}'`），
control-plane 滚动到 revision 147。

**改了什么**

* **判据翻转**：控制面 `E2B_TREES_SHARED=0`；三份清单的 `E2B_WORKSPACE_BASE` 都指
  `/var/lib/e2b/workspaces`（节点本地 hostPath，worker 与 face B 各挂一份；控制面只命名）。
  共享根 `/var/lib/e2b-sandboxes` 一个字符没动：`_snapshots` / `_migrate` / `_volumes` /
  `_images` 还在它下面，`<export>/workspaces` 成了一个**空**目录（旧树根）。
* **迁移在途流式 + 按字节上限**（`E2B_TREE_COPY_MAX_BYTES=1342177280`、
  `E2B_TREE_COPY_WINDOW_BYTES=67108864`，控制面/worker/face B 都设）：控制面
  `client.stream("GET")` 逐块落 `<export>/_migrate/control-plane/<id>.tar.gz`，
  导入用异步生成器分块 POST；worker 的导出/导入/快照捕获都走同一个有界 writer；
  face B 解包前按 `tree_payload_bytes()` 记账，超限 `tree-too-large` → 413。
* **两个具名错误**：`source-node-unreachable`（源节点不回答 = 树不可达且无法事后迁出）
  与 `tree-missing-on-recorded-node`（记录指着节点、那里没有树；修复动作
  `retire-stale-tree-record`，见设计文档 §8.3）。
* **`maint` 的 memory 512Mi → 2Gi**（设计文档 §3.1 的第三条：不动限额就得把上限压到
  ≤256 MiB，比任何默认沙箱配额都小）。
* 清单侧：`workspace-root-init` 建节点本地树根并 `chown 65534` + 严格校验；共享卷上的
  `$workspace-root-init` 不再创建 `<export>/workspaces`（树搬走之后留一个永远空的
  "第二个树根"只会误导后来者）。

**上线顺序**：`deploy/scripts/build-and-push.sh` →
`KUBECONFIG=… deploy/k8s-k0s/apply.sh`（agent 先滚，它的 `workspace-root-init` 建树根并
交属主；然后 worker / control-plane）。上线后 9 pod Running、`e2b-worker` 2/2。

**验收（2026-10-02 17:45–18:05，正是本节版本；先手工建一个沙箱预热）**

| 判据 | 读数 | 判定 |
|---|---|---|
| 跨节点迁移**保文件**（两节点健在） | `e2b-worker-0` → `e2b-worker-1`，`200`，**1047 ms**（201 文件 + 1 目录），迁后逐字读回 `kept.txt` / `small-199.txt` | ✅ |
| 停掉源 worker 后的迁移**具名拒绝** | `scale --replicas=1`（去掉 `e2b-worker-1`，1.8 s / 2 s 的窗口内）→ migrate = **502** `source-node-unreachable: … (it did not acknowledge the runtime stop)`；记录仍在 `e2b-worker-1`，目标节点零字节。**本轮 2/2** | ✅ |
| 同一拒绝的确定性变体（`--remove-node`） | 摘掉节点注册行后立刻迁移：`0.1.0-905` 给 **`502 Node e2b-worker-1 not found`**（泛化 502，不是那个名字）——见下面的"两个入口缺口" | ❌/已修 |
| 复原 | `replicas=2`，两个 worker Running，`GET /sandboxes` = `[]` | ✅ |
| 介质归属（两节点各看一次） | `<id>` 只在记录指的那台节点的 `/var/lib/e2b/workspaces/<id>`（`0770 10000:65534`，各自 `/dev/nvme0n1p2`、inode 不同）；共享 `<export>/workspaces` 两节点都空 | ✅ |
| **元数据密集**（裁定要求） | 沙箱内 200 × 64 B：**0.196 / 0.223 ms/个**（两次样本）vs 共享 NAS 同方法 **12.9986 ms/个** ⇒ **58–66×**；worker 容器同方法 本地 0.0276 vs NAS 12.9986 ⇒ 471×（解释见设计文档 §8.1.1：沙箱的路径中介给两边各加 ~0.13 ms/个的常数） | ✅ |
| 大块顺序写（诚实记录代价） | 沙箱内 900 MiB ×3：**146.9 MB/s**（min 145.4 / max 147.9）vs 共享 NAS 483–493 MB/s ⇒ 慢 ~3.3× | ✅（预期内） |
| 磁盘可解释 | 树盘 worker-0 `26G/74G free`、worker-1 `33G/68G`；镜像缓存各 3.8G；节点 state 100K/16K；共享 `_snapshots` 16M、`_images` 3.7G、`workspaces` 512 B | ✅ |
| 孤儿回收看得见本地树 | 泄漏的那棵树在沙箱 `kill` 后由 agent 巡检 → 控制面判孤儿 → **~90 s** 内收走（17:57:52 → 17:59:23 两个树根都空） | ✅ |
| **迁移释放源节点的树** | **失败**：见下 | ❌ |

**上线当天抓到的新状态：`stale-tree-on-former-source`（设计文档 §8.3.1）**

一次**成功**的迁移在源节点留下了一整棵树（`sbx_85d7e990562423fc`，
`e2b-worker-0` → `e2b-worker-1`）：

```
# worker-0 日志
agent delete … failed: the tree … could not be removed (/var/lib/e2b/workspaces/<id>):
  AgentFileOpsError: the control plane refused remove-workspace … (HTTP 403):
  Sandbox … belongs to node e2b-worker-1, not e2b-worker-0
  "DELETE /agent/sandboxes/<id>?keepVolumeSlices=true" 500
# 控制面日志
node e2b-worker-0 refused the teardown (HTTP 500): … its files are kept
migrated sandbox … from e2b-worker-0 to e2b-worker-1     "POST …/migrate" 200 OK
# 另一个 CP 副本的自愈（90 s 后）
c3 self-heal: 1 tree(s) reported by node … are claimed by a record and are left alone: <id>
```

根因：释放走**源 worker 的 DELETE**，它必须先向控制面申请 `remove-workspace` file-op，
而那条作用域按**记录**判节点 —— F1 为了让目标节点的 provision 通过同一条作用域，早已把
记录切到目标 ⇒ **控制面拒绝了自己刚下的指令**；迁移不检查返回值，于是照样 200，留下的
树被自愈判 `protected`（"claimed by a record"）。影响：每个成功迁移在旧节点留一整棵树
（旧节点按 8 个沙箱卖）；**不是永久泄漏** —— 沙箱 `kill` 之后 ~90 s 由自愈收走。

**修复已提交、尚未上线**：源节点的释放改走 CP→agent 通道（控制面派生路径、指令那台节点
的 agent 删，与 materialize / `scope-slot-document` 同一条），worker 侧作用域一个字符不动；
释放失败时记录里写具名状态、日志 ERROR，迁移本身仍成功。钉子
`tests/unit/test_tree_local_migration.py::test_the_source_tree_is_released_through_that_nodes_agent`。
**`0.1.0-905` 上仍是旧行为**，下一次控制面 rollout 才生效。

**同一个修复还收掉了具名错误的第二个入口缺口**：把源节点的**注册行**摘掉
（`DELETE /nodes/<id>`，两个副本各清一遍）之后立刻迁移，`0.1.0-905` 回答
`502 {"code":502,"message":"Node e2b-worker-1 not found"}` —— 对本地树来说，"节点行没了"
与"节点不回答"是同一件事（树在那台机器的盘上、够不着），所以修复里那条
`nodes.get(...) is None` 分支也走 `source-node-unreachable`
（钉子：`…::test_a_source_whose_node_row_is_gone_is_refused_by_the_same_name`）。

**down 腿的方法学（本轮实测的坑）**：`--stop-source`（缩容）的窗口只有 ~1–2 s
（Autoscaler 的 warm-pool 地板 `E2B_AS_MIN_REPLICAS=2`，~1 s 就重建 pod，autoscaler 日志
`scaled up to warm-pool floor 1 -> 2`），所以探针把轮询收紧到 0.25 s、在 pod 消失的当刻
发迁移；`SIGSTOP` 那条路**走不通**（PID namespace 里的进程不能停 namespace 的 init，
`kill -STOP 1` 返回 0 而 `/proc/1/stat` 仍是 `S`）；`--remove-node` 又要连发几次
（节点注册表是**每个控制面副本各一份内存**，实测 `statuses=[204,204,404,404,404,404]`）。

**复跑命令**（全部只读或公开 API 建/杀沙箱；down 腿要控制者授权缩容）：

```bash
deploy/scripts/open-cluster-tunnel.sh && export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)          # 不要打印
export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)

env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
  deploy/scripts/acceptance/tree_local_migration_probe.py --directions up --files 200

# down 腿：目标沙箱必须落在会被缩掉的那台上（StatefulSet 只去最高序号 = e2b-worker-1）。
# --prepare-source 自己造一个（--files 200）并把树放到那台上，跑完杀掉它。
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
  deploy/scripts/acceptance/tree_local_migration_probe.py --directions down --files 200 \
  --prepare-source e2b-worker-1 --stop-source e2b-worker-1   # 探针自己缩容并复原到 2

# 元数据密集（两行都要量）：worker 容器内 NAS vs 本地
kubectl -n sandlock exec -i e2b-worker-0 -- python3 - \
  --root nas:/var/lib/e2b-sandboxes/workspaces --root local:/var/lib/e2b/workspaces \
  --small-n 200 --skip-seq < deploy/scripts/acceptance/local_first_storage_probe.py
# 沙箱内那一条：把同一个脚本写进沙箱跑 --root sandbox:/workspace
```

> 注意：探针 `--directions down` 会**缩容一个 worker 副本**（这是唯一能测那条腿的办法），
> `finally` 里复原到原副本数并等两个 pod Running 才算完；跑之前先确认舰队里没有别的活沙箱
> 落在那一台上（本轮的 `GET /sandboxes` 是 `[]`）。

### 7.33 发版：建箱存储本地优先（Task 0–5）（**2026-10-02，上线版本 `0.1.0-915-gfb8a74b-20261002-211709`（已被 §7.34 取代）**）

> 本节是本批的**发版记录**：把 §7.29–§7.32 四节（Task 0 / Task 2 / Task 4+5 / Task 3）
> 串成**一次上线**。各节的逐条命令与原始读数仍在原处，本节只给"一个版本、一张验收表、
> 两条硬约束、一份不做清单、一条回退路"。
>
> **本批的红灯按顺序闭合**（§7.33.4 保留全过程）：跨切面冒烟当时是**红灯**，因为 Task 3
> 的验收窗口留下了**没人 kill 的孤儿沙箱**（外加一次冒烟循环被中断），而节点本地形态下
> 一个孤儿沙箱**永久占着它那个节点的名额**；控制者清掉虚高的台账
> （`e2b:node:quota:*`，27 个陈旧 pod 名键 + 两个 worker 键）与 8 个孤儿沙箱、重启 worker
> 之后，**`MULTI-NODE` 与 `DEPLOYMENT` 两条冒烟在 `0.1.0-908` 上都通过**。
> 同一现场里的第二个 bug ——"源节点不可达"的拒绝路径**泄漏目标节点配额**（**N59**）——
> 代码已修（`32f3667`：两个台账都释放 + 注册时对账），**已随 `0.1.0-915` 上线**
> （对账机制的现场复验见 §7.33.4 的 T4）。这一节读作"红灯 → 清理 → 复验通过 → 修复上线"，
> **不是**"当时没红过"。

计划 `docs/superpowers/plans/2026-10-02-local-first-create.md`（Task 0–6）；设计权威
`docs/create-local-first-design.md`；介质地图 `docs/create-local-first-layout.md`。
**本批只改三件事**：

1. **快照载荷** 从爆炸式 `fs/` 目录改成一个 `fs.tar`（共享卷上，读侧两种形状都收）—— Task 2；
2. **沙箱树**从共享 NAS 搬到**节点本地盘**，跨节点迁移经 `<export>/_migrate` 中转
   （流式 + 按字节上限 + 两个具名错误）—— Task 3；
3. **建箱 `prepare` 的三样小件**（`.creating` 标记、uid 认领、`disk-stats` 种子）连同
   `.route-b` 搬到**节点本地 state** —— Task 4。

Task 0 先把"谁挂哪个根"与迁移判据 `E2B_TREES_SHARED` 显式化（默认行为逐字不变），
Task 5 修 `c3_agent` 的目录链（建箱要走的 `<root>/workspace` 那条路）。

**一条版本线**（`deploy/stack/.version`；worker / control-plane-gateway / agent 三个镜像同 tag）：

| 版本 | 内容 | 节 |
|---|---|---|
| `0.1.0-887-g7ef319b-20261002-100406` | Task 0 根重切（`_snapshots` 合一、`_migrate` 上浮、`E2B_TREES_SHARED` 就位）+ `copy_from` 回归修复 | §7.29 |
| `0.1.0-892-g52044b8-20261002-125656` / `0.1.0-895-gc478ca0-20261002-134406` | Task 2 快照载荷打成 `fs.tar` + 读侧守卫修复 | §7.30 |
| `0.1.0-900-g0079c84-20261002-161409` | Task 4 本节点 state 分家（含 Task 5 目录链） | §7.31 / §7.30 末 |
| `0.1.0-905-g7331364-20261002-174329` | Task 3 树本地化 + 流式迁移（**介质翻转**） | §7.32 |
| `0.1.0-908-gd652148-20261002-184859` | Task 3 的两条上线后修复（源树释放改走 CP→agent；释放挪到成功路径最后一步） | §7.32 / 设计 §8.3.1 |
| **`0.1.0-915-gfb8a74b-20261002-211709`** | N59 修复上线（拒绝路径归还目标预约 + 注册时对账）+ 记录更正（`fb8a74b`）—— **当前** | §7.33.4 |

**上线后现在真正的开关取值**（`kubectl -n sandlock get deploy control-plane` /
`get statefulset e2b-worker` / `get ds e2b-c3-agent` 的 env）：

| 开关 | 值 | 在哪 |
|---|---|---|
| `E2B_TREES_SHARED` | **`0`**（判据翻转；树不再共享） | control-plane |
| `E2B_WORKSPACE_BASE` | `/var/lib/e2b/workspaces`（**节点本地 hostPath**，worker 与 face B 各挂一份；控制面只命名） | worker / agent / control-plane |
| `E2B_NODE_STATE_BASE` | `/var/lib/e2b/state`（节点本地；`.creating`/`disk-stats`/`.route-b`/uid 池本地件） | worker / agent |
| `E2B_STATE_BASE` | `/var/lib/e2b-sandboxes/state`（共享；记录 `_runtime/<id>/sandbox.json` 与 `.checkpoints/**`） | 全部 |
| `E2B_TREE_COPY_MAX_BYTES` / `E2B_TREE_COPY_WINDOW_BYTES` | `1342177280`（1.25 GiB）/ `67108864`（64 MiB） | control-plane / worker / agent |
| `E2B_IMAGE_CACHE_MAX_BYTES` | `4294967296`（4 GiB，镜像解包缓存，与树同盘） | worker / control-plane |
| `maint` 的 `memory` limit | **2Gi**（Task 3 从 512Mi 抬上来；face A 仍 256Mi） | agent |

共享根 `/var/lib/e2b-sandboxes` 一个字符没动：`_snapshots` / `_migrate` / `_volumes` /
`_images` / `_secrets` 还在它下面，`<export>/workspaces` 成了一个**空**目录（旧树根）。

#### 7.33.1 本批的验收表（Task 6 的复核；标"复用"的行**不是本轮重测**）

测于 **2026-10-02，`0.1.0-908-gd652148-20261002-184859`**（隧道自检：2 节点 / arm64 /
`v1.36.4+k0s` / sandlock 9 pod）。先手工建一个沙箱预热，再跑下面各条 —— 刚滚完的舰队
第一个建箱会因 worker 重新预热镜像而超时。

| 判据 | 读数 | 来源 |
|---|---|---|
| **plain 建箱 p50**（客户端边界，n=10 ×2 轮） | **70 / 71 ms**（p95 75 / 86，mean 71 / 74；预热那一发 611 ms） | **本轮实测** |
| plain 建箱 p50（平台侧，控制面 pod 内回环，n=10） | **40 ms**（p95 49，mean 43） | **本轮实测** |
| 平台外的那一段（客户端→入口） | 约 **30 ms**（70 − 40） | 本轮实测（两读数相减） |
| 目标 **~60 ms** | **未达成**：客户端边界 70 ms 比目标高 ~10 ms；**平台侧 40 ms 在目标内** | — |
| worker 自己那段（trace） | **未在本轮复测**（要 `E2B_CREATE_TRACE`，那是一次 worker 滚动、本任务未获授权）；复用 §7.31 在 `0.1.0-900` 上的逐段：`prepare` 7.4 + `finalize` 8.4 + `prime` 3.3 ≈ **19 ms** | **复用** |
| ⇒ 平台侧"worker trace 之外"的部分 | ≈ **21 ms**（40 − 19）= 控制面自己的活 + 与 `prepare` 并发的 agent `materialize` 那一跳。对照 §7.31 的改前 ≈97 ms（116 − 19）：翻转主要动的就是这段 | 本轮实测 + 复用推断 |
| **快照捕获每条目 / 每字节**（翻转前，202 档） | 捕获 **1703 ms**、建箱 p50 **6162 ms**、**30.354 ms/条目**（n=3，`0.1.0-895`，树仍在共享） | **复用**（§7.30） |
| **快照捕获每条目 / 每字节**（翻转后，1 / 40 / 202 三档，n=3） | 捕获 **141 / 134 / 152 ms**；**从快照建箱 p50 87 / 123 / 154 ms**；**每条目 43.682 / 2.997 / 0.756 ms** | **本轮实测** |
| **快照建箱（计划点名的 2000 文件档，n=3）** | 捕获 **419 ms**、**从快照建箱 p50 532 ms**（p95 1097、mean 719）、**0.266 ms/条目**；`kept='kept\n'`、`workspace/workspace/…` 不存在 | **本轮实测** |
| 目标"**2000 文件 52 s → 亚秒**" | **达成（p50；p95 越过 1 s）**：同一档 p50 **532 ms**，p95 **1097 ms**（3 个样本里有一个 1097、另两个 528/532），mean 719（对照旧形状 28.2 ms/条目 × 2000 ≈ 56 s ≈ 计划里的 52 s） | — |
| **迁移保文件**（节点健在，`worker-0 → worker-1`，200 文件） | **1047 ms**，迁后逐字读回 | **复用**（§7.32 / 设计 §8.5） |
| 迁移保文件（本轮复跑，同 200 文件） | up 腿 **889 ms**（201 文件 + 1 目录，读回 `kept='kept\n'` / `last='199\n'`）；另一条独立流 **895 ms** | **本轮实测** |
| 停掉源 worker 后的迁移**具名拒绝** | **502 `source-node-unreachable: … (it did not acknowledge the runtime stop)`**；缩容窗口 1.1 s；`replicas` 复原 2、两个 worker Running；`residue=[]` | **本轮实测** |
| **迁移成功是否释放源节点的树**（`908` 的新修复） | **是**：源节点 tree root 上该 id **已消失**、目标节点有它；记录上**没有** `source tree retained`、控制面两副本日志里**没有** `was retained` 的 ERROR | **本轮实测**（复核 `39f28a9` 的泄漏修复） |
| 元数据密集（沙箱内 200 × 64 B，翻转后） | **0.196 / 0.223 ms/个** vs 共享 NAS 同方法 **12.9986 ms/个** ⇒ **58–66×**；大块顺序写沙箱内 **146.9 MB/s**（慢约 3.3×） | **复用**（§7.32 / 设计 §8.5⑥） |

**建箱那 ~60 ms 目标怎么读**：计划的目标写在**平台**这一侧（"建箱 127 → ~60 ms"，
改前 §7.31 的读数 116–117 ms 也是控制面 pod 内量的）。本轮控制面 pod 内 **40 ms**
已在目标内；**70 ms 是从这台开发机经入口量到的"用户可见"值**，其中约 30 ms 是客户端到
入口那一段网络，与平台无关。两条都如实给出，**不要把 70 说成平台没达标，也不要把 40
说成用户看到的是 40**。

#### 7.33.2 两条硬约束与各自的上限配置

1. **容量（节点盘）**：每节点可用盘约 **75 G**，且与约 4 GiB 的镜像解包缓存**同一块盘**。
   调度上限是 `E2B_NODE_DISK_MB=8192` ÷ 每沙箱默认 `E2B_DEFAULT_DISK_MB=1024` ⇒
   **8 个沙箱/节点**；树本地化之后这个数第一次与"节点盘"同源（在此之前心跳报的
   `usedDiskMB` 是 NAS 的几十万 MB）。**上限配置**：`E2B_NODE_DISK_MB`（每节点可卖容量）、
   `E2B_IMAGE_CACHE_MAX_BYTES=4294967296`（镜像缓存，`E2B_IMAGE_CACHE_EVICT_MIN_AGE_S=300`）。
   快照仓**不进节点盘**（设计 §8.4 的容量账：8 沙箱 × 1 GiB × 每天 1 个 × 7 天 = 56 GiB
   已越界），这也是 Task 2 让快照落共享的理由。
2. **页缓存（容器内存）**：容器限额实测 worker **4 GiB**、控制面 **2 GiB**、agent face A **256 MiB**、
   face B `maint` **2 GiB**（Task 3 从 **512 MiB** 抬上来 —— 抬之前一次 900 MiB 的恢复把
   `memory.current` 顶到 **512.0/512 MiB（10/10 次）并真 OOMKilled 过一次**，设计 §3.0/§3.1）。
   **上限配置**：`E2B_TREE_COPY_MAX_BYTES=1342177280`（一次树拷贝的字节上限，`0` = 不限；
   卡在 1 GiB 配额本身会把"装满的沙箱"这一唯一不该被拒的情形拒掉）+
   `E2B_TREE_COPY_WINDOW_BYTES=67108864`（每 64 MiB `posix_fadvise(DONTNEED)`，只影响峰值）+
   `maint` 的 memory limit 2 GiB。两端都不再把整棵树读进内存（控制面 `BoundedTreeWriter`
   流式、worker 落暂存文件、face B 只按 `tree_payload_bytes()` 记账）。

#### 7.33.3 刻意不做（来自计划的 Self-Review，逐条照录）

* 卷数据、`_volumes/_meta`、`_templates`、`_builds`、`_oci/*.oci.tar` **一律不动**；
* `_cow` 保留名不删；
* **checkpoint 本轮仍留共享** —— 它的"本地化"取决于是否禁止迁移 `paused` 沙箱，那条要单独立项；
* **不做**"树本地 + 跨节点冗余"：节点掉线丢树由"沙箱不是持久对象"这条产品语义承担
  （持久面是快照与卷），不靠代码兜底；代价是**排水顺序"先迁走、再下线"是操作纪律**；
* Task 2 评审提出、Task 3 落地的两条**已知、可接受、但要点名**的行为：tar 解包走
  `data` filter 会把文件模式**夹紧**（`0664/0777 → 644/755`，丢 setuid/setgid）；
  `tarfile` 的成员索引约 **430 B/成员**（202 档照不出来，成员数上限归 Task 3 的字节上限那一族）。

#### 7.33.4 冒烟与残留（**先红后绿**：红灯的原因、清理与复验都在这里）

复跑命令（凭据只从 Secret 取、不打印）：

```bash
deploy/scripts/open-cluster-tunnel.sh && export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)   # 不要打印
export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
    -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python deploy/scripts/multinode_smoke.py
env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python deploy/scripts/deployment_smoke.py
```

**下表按发生顺序读，五段共用同一根时间轴**（"时刻"列是判据的一部分）：**A**（本任务自己的
验收窗口，处置之前）→ **T1 红灯**（控制者发现现场时）→ **T2 清理** → **T3 绿灯**（复跑）→
**T4 修复上线**（`0.1.0-915`）。
`GET /sandboxes` **不按状态过滤**，所以"列着 8 条"与"`[]`"不可能同时成立 —— 它们分别是
T1 与 T3 的读数（A 的 `[]` 更早：那时那 8 条还不存在）。

| 时刻 | 冒烟 / 残留 | 结果 | 证据 |
|---|---|---|---|
| **A**（本任务验收窗口：local 17:45–18:05 = UTC 09:45–10:05；**处置之前**，那 8 条还不存在） | `GET /sandboxes`、两节点树根 | `[]`；两节点树根 + 共享旧树根都 0 项 | 本轮实测（§7.33.1 的收尾） |
| **T1 红灯**（控制者读数时约 UTC `12:5xZ` ≈ 本地 `20:5x`；此时已有 8 条没人 kill 的沙箱） | `MULTI-NODE` | **❌ 失败** | 建第 4 个沙箱时 `503: No resources available`（它 `finally` 里 `assert` 预约归零也失败）。根因见下：worker-0 的配额台账虚高 ⇒ 调度选中它时 `_quota_store.reserve` 拒绝，而 `select_and_reserve` **不换下一个候选**（N60） |
| T1 | `DEPLOYMENT` | **❌ 失败** | 3 个沙箱建成功、`NODE DISTRIBUTION` 两个节点都在、命令/文件段过；死在**迁移**那一步：`migrate → 503 {"code":503,"message":"Node e2b-worker-0 has no capacity or is unavailable"}`（目标节点 quota 满） |
| T1 | `GET /sandboxes` | **列着 8 条**，全部 `state: running`、`endAt` = UTC `2026-10-02T11:48:39Z`（另四条 `11:50:39Z`）⇒ 过期约 **65 分钟** | 控制者现场读数（也是他删它们的那条口径；N61 的本体） |
| T1 | 两节点树根 | 各自还留着落在该节点的**活树** | 控制者现场读数 |
| T1 | 节点配额台账 | worker-0：Redis `e2b:node:quota:e2b-worker-0` = memory **3072** / cpu 300 / disk 3072，而同一刻 `/internal/nodes` 的 `reservedMemoryMB` = **1024** ⇒ **两个数互相对不上**（N59 要治的漂移）；worker-1 两边都是 0 | 控制者现场读数（Redis 只读） |
| **T2 清理**（T1 之后立刻，控制者执行） | 三步 | ① 删 `e2b:node:quota:*`（27 个陈旧 pod 名键 + 2 个 worker 键）；② `DELETE /sandboxes/<id>` ×8；③ 重启两个 worker 让 `_rebuild_node_reservations` 重算视图 | 见下"控制者的清理与复验" |
| **T3 绿灯**（同一次窗口内复跑，`0.1.0-908`） | `MULTI-NODE` | **✅ `MULTI-NODE SMOKE OK`** | 4 箱 2+2、commands / files / health / stdin 全过、kill 后两边预约 **0/0** |
| T3 | `DEPLOYMENT` | **✅ `DEPLOYMENT SMOKE OK`** | 命令+文件、**跨节点迁移保文件**、网络配置、远端卷+兄弟卷隔离、模板构建 → registry push → worker pull → rootfs、MCP 网关全过；kill 后两边预约 **0/0** |
| T3 | `GET /sandboxes` / 舰队视图 / 两节点树根 | `[]`；`{}`；`e2b-worker-0` = 0 项、`e2b-worker-1` = 0 项、`<export>/workspaces` = 0 项 | 复跑后的读数 |
| T3（与时刻无关） | pod / `DRY_RUN=1 deploy/k8s-k0s/apply.sh \| kubectl diff -f -` | 9 个全 Running（`e2b-worker` 2/2）；**0 行**（仓库规格 ≡ 线上） | 本轮实测 |
| **T4 修复上线**（控制者，2026-10-02） | 构建 + `apply.sh` → **`0.1.0-915-gfb8a74b-20261002-211709`** | 9 pod Running、`GET /sandboxes` = `[]`（T3 之后舰队一直是空的） | `deploy/stack/.version`；`kubectl -n sandlock get deploy/control-plane ds/e2b-c3-agent sts/e2b-worker -o jsonpath=…` 三个镜像同 tag |
| T4 | **对账机制的现场复验** | worker-0 的 `e2b:node:quota:e2b-worker-0` 注入 **+2048** 幻影（连带当时遗留的 **-1024** 负值）→ 重启 worker → 重注册触发 `reconcile_quota_ledger` → 键回到 **0** | 控制者现场读数。证明语义是"设为真值"而不是"只降"：负值也被纠正 |

**T1 的这条脏残留是新 bug（登记 N59）**：`POST /sandboxes/<id>/migrate` 在
`_stop_source_runtime` 之前就 `nodes.reserve_node(target)`，而"源节点不可达"拒绝走的是
外层 `except Exception` 回滚 —— 那条回滚**只把记录指回源节点、重建源的运行时，从不
`release_quota(target)`**。于是**每一次具名拒绝都泄漏目标节点的一份配额**。
（路径与回滚是读代码直接确认的；份数是推演：Redis 上 worker-0 的 3072 = 上一次 905 验收的
**2 次**拒绝（2 × 1024，记在 Redis、后被 `0.1.0-908` 的滚动从内存台账里抹掉）+
**时刻 A 里复跑 down 腿的 1 次**（1024，两边都记上；证据是 down 腿跑完当场 `/internal/nodes`
就报 worker-0 = 1024，而它此前是 0）。这条泄漏还解释了为什么台账两边对不上：控制面
**没有**按记录重建 Redis 台账的路径，而节点在**每次注册**时只按活记录重建**内存**那一半
（`_rebuild_node_reservations`），Redis 那一半只增不减 —— **本轮已补**：注册时
`reconcile_quota_ledger` 把 Redis 那一行也设成同一个数（见下面的代码修法②）。
**当时（T1）实现者没有清理它** —— 清 Redis 是一次集群写，不在实现任务的授权范围内；
**控制者在 T2 用自己的窗口清了**（见下）。无论谁来清，都**不要**为此放宽"有记录认领的
树不回收"那条保护。

**T2：控制者的清理与 T3 复验（2026-10-02，当场记下顺序）**：现场不止是台账虚高，而且
`GET /sandboxes` 里确实有**八个没人 kill 的沙箱**（Task 3 验收窗口四个 + 一次被中断的冒烟
循环四个）。清的动作是：① 删掉 `e2b:node:quota:*` 里 27 个陈旧 pod 名键与两个 worker 键；
② 用 `DELETE /sandboxes/<id>` 杀掉那 8 个孤儿（两个树根随即又空、`GET /sandboxes` = `[]`）；
③ 重启两个 worker，让注册时的 `_rebuild_node_reservations` 重算视图。之后
**`MULTI-NODE` / `DEPLOYMENT` 两条冒烟在 `0.1.0-908` 全绿**（见上表的复跑行）。

**T1 的红灯为什么"几个孤儿沙箱"会变成"整支舰队不能建箱"**：放大器是
`select_and_reserve` 在配额台账拒绝时**不换下一个候选**、直接 `return None` ⇒ 只要调度
挑中的那个节点满，建箱就是 `503`，哪怕另一台还有空位。这一条**本轮不改**（调度行为要
自己的裁定与验收），登记为 **N60** —— 那一行同时记着它的代价、评审的反方论证（在候选集
耗尽前重试严格更好，跳过时打具名 WARNING 就不会丢可见性）与提议的修法。本轮只做"不改放置
的可见性"：被拒的节点现在会打一条具名 WARNING（含四个维度与"没有试别的候选"）。

**T1 的这 8 条记录为什么一直没被自愈收走 —— 观测，不是机制**（登记 **N61**，本轮只更正记录）：
控制者清掉的记录当时是 **`state: running`**、`endAt` = UTC `2026-10-02T11:48:39Z`
（另四条 `11:50:39Z`），读它们时墙钟约 `12:5xZ` ⇒ **过期约 65 分钟**；两节点树根上还有
活树。而 TTL 扫描**无条件启动**、间隔 **1 s**（`app.py:347`），`_ttl_reapable` 只对
`paused` / `orphaned` 免疫，其余按 `end_at` 判过期；记录被删时 `add_on_removed
(_release_node_quota)` **同时归还两边台账** —— 对 `running` 记录，机制上本该一秒内收走。
它没有，所以本草**不写"因为没有 TTL / 没有对账"这种机制**（那句是错的、也是这轮更正的
东西），只写观测 + 待查清单（claim 单飞、存储里 `end_at` 的解析/时区、存储里的 state 与
API 报的是否不同、两副本视图不一致），全部在 **N61**。`E2B_ORPHAN_RECORD_TTL` 本部署
**没有设**（默认 0）—— 它只关"`orphaned` 永不回收"，**不会**救这 8 条 `running` 记录。
⚠ **时区教训**（写给下一个读这些记录的人）：沙箱记录里的时间戳是 **UTC**，而验收输出
前后是**本地时间**；控制者第一次就是按本地时间读，把"过期 65 分钟"看成"过期 7 小时"。

**代码修法（`32f3667`，已随 `0.1.0-915` 上线；见上表 T4）**：① `migrate` 的目标预约由一个**唯一**的回滚点释放
（`target_reserved` 标志 + 外层 `except` 里的 `release_quota(target)`），所以
`source-node-unreachable`、导出握手失败、记录重指向写失败都还回去，且**没有任何路径会释放两次**
（原来的 inner handler 负责释放，但那块**根本到不了**拒绝路径）；两个台账（节点视图 +
Redis）由 `release_quota` 一次写。② 配额台账补上**注册时对账**：
`_rebuild_node_reservations` 先按活记录 `set_reserved`（内存视图，原有行为），再
`nodes.reconcile_quota_ledger(...)` 把 Redis 那一行**设成同一个数**（WATCH/MULTI，
有改动就打 WARNING 并打印带符号的 delta）—— 于是"泄漏的名额"会像视图一样在 worker
重启/重注册时自愈，不必再手工删 key。为什么不只降不升：两边对不上本身就是这次要修的病，
而对账发生在**节点重新注册**的那一刻 —— 那台节点的运行时就重建过，**有记录认领的预约才是在服务的**，
所以记录在这一刻是权威；代价已具名记在 `_rebuild_node_reservations` 的 docstring 里
（比"在途建箱"更宽：任何**这个副本此刻读不到记录**的预约 —— 建箱/迁移的记录还没写下来、
或还没传播到这个视图 —— 都会在对账里被抹掉）。**没有改** `select_and_reserve` 的
"不换候选"（登记 **N60**；本轮不为"不改"写理由，那一行里有代价、反方论证与提议的修法）。

#### 7.33.5 回退

* **介质翻转**（Task 3）的回退**不是改一个字符**（这是设计 §8.6 的净亏条件成立时的出口），
  最少要做三件事，缺一件就是静默空树的形状（不导出 → 记录改指 → 目标端建空树）：
  1. **判据**：把控制面的 `E2B_TREES_SHARED` 从 `0` 翻回 `1`；
  2. **树根**：`E2B_WORKSPACE_BASE` 从 `/var/lib/e2b/workspaces` 改回 `<export>/workspaces`
     （= `/var/lib/e2b-sandboxes/workspaces`）。**四条 env 一条都不能剩**：
     `deploy/k8s/worker.yaml`、`deploy/k8s/control-plane.yaml`、`deploy/k8s/c3-agent.yaml`
     的面 B，以及同一份清单里 `workspace-root-init` 的 `WORKSPACE_BASE`；
  3. **数据步骤**（除非先把舰队排空）：活树在**节点本地** `/var/lib/e2b/workspaces/<id>`，
     共享形状读的是 `<export>/workspaces/<id>`，而记录里的 `workspace_dir` 也还指着节点本地
     那条路径 —— 要把树搬过去、并把记录一起改指。**没有脚本做这次搬移**
     （`migrate-state-base.sh` 只处理 `_snapshots`/`_migrate`）。
  节点本地的 `workspace-root`/`node-state` 挂载翻回后是**惰性**的，可以留着不删；
  **`E2B_TREE_COPY_MAX_BYTES`/`_WINDOW_BYTES` 不是** —— 翻回共享后它们照样管着快照载荷的
  `fs.tar` 写入与拆包（两种形状都走 `gateway_common/archive.py`），不要顺手删掉。
  `workspace-root-init` 那条严格 `chown` + `exit 1` 会转而跑在 NFS 上（本集群恰好通过）。
  代码路径与验收探针都不用改。
* **Task 0 的根重切**回退：`state/.state-base-migration.journal`（0600）+
  `deploy/scripts/migrate-state-base.sh --rollback --apply` 原路退回；**回退之后必须同时
  回退镜像**（`308b543` 之后的代码只认新位置）。
* **Task 2 的快照 tar**：读侧两种形状都收，回退镜像**不需要**回退数据（老 `fs/` 快照在
  任何一侧都能恢复）；反过来，新写的 `fs.tar` 只被新读侧认，回退到 `0.1.0-887` 之前会让
  这些新快照**不可恢复** —— 回退要一并停手。
* **Task 4 的本节点 state**：把 `E2B_NODE_STATE_BASE` 从两份清单里去掉（或退回 worker/agent
  镜像），`prepare` 立刻回到共享 base 的写法；节点本地盘上留下的那几样是**可再生的残渣**。

### 7.34 发版：N57/N60/N61/N62/N63 收口（**2026-10-03，当前版本 `0.1.0-931-g4181bb4-20261003-013625`**）

计划 `docs/superpowers/plans/2026-10-02-open-issues-fix.md`（Task 1–5 + 收尾）。本节是这一批的
**发版记录**：一个版本、四条收尾读数、两条冒烟、两次非预期读数（含自愈前后）与一条操作教训。
五条改动各自的设计与先红钉子仍在原位（`docs/open-issues.md` 的 N57 / N60 / N61 / N62 / N63
行与各自的 task 报告），本节只记"它上线时现场看到了什么"。

**版本线**（`deploy/stack/.version`；worker / control-plane-gateway / agent / quota-agent 四个
镜像同 tag）：

| 版本 | 内容 | 节 |
|---|---|---|
| `0.1.0-915-gfb8a74b-20261002-211709` | 上一批（建箱存储本地优先 + N59 修复）—— 本批的**基线** | §7.33 |
| **`0.1.0-931-g4181bb4-20261003-013625`** | 本批收口（N57 uid 池回落分配器跨节点互斥 / N60 放置逐候选重试 / N61 TTL 可见性埋点 + 只读探针 / N62 快照记录跨副本可见 / N63 tar 成员数上限）—— **当前** | 本节 |

**上线顺序（照命令实际发生的次序）**

1. **通道 + 身份闸门**：`deploy/scripts/open-cluster-tunnel.sh` 自检必须报 2 节点 / arm64 /
   含 `+k0s` / `sandlock` 9 pod（拒绝错集群）。**任何 `kubectl` / `apply.sh` 都在同一条命令里
   带 `KUBECONFIG="$PWD/tmp/k0s/kubeconfig"`**（见 §7.34.1 的操作教训）。
2. **四个镜像先在 ACR 就绪**：`build-and-push.sh` 的单平台分支恒用 `--load`（只有 `PLATFORMS`
   含逗号才 `--push`），所以 `control-plane-gateway` 之外的三条（worker / agent / quota-agent）
   这轮是**另行 `docker push` 补上去的**；四条都 `docker buildx imagetools inspect` 复核为
   `linux/arm64` 之后才 apply。
3. **apply**（唯一一条，无任何 flag）：
   `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" deploy/k8s-k0s/apply.sh` —— 7 个镜像引用 pin 到本版，
   内建顺序 agent DaemonSet → worker StatefulSet，退出码 0。
4. 补等 `control-plane` / `seccomp-installer` 的 rollout（`kubectl rollout status`，同样带前缀）。
5. **两条冒烟**（`multinode_smoke.py` / `deployment_smoke.py`，调用形状与凭据取法见 §7.33.4 的
   复跑命令块）。

**四条收尾读数（全部只读）**

| # | 读数 | 结果 |
|---|---|---|
| 1 | 三处镜像 tag ≡ `deploy/stack/.version` | ✅ `control-plane-gateway` / `agent` / `worker` 全是 `0.1.0-931-g4181bb4-20261003-013625`（redis 保持 `redis:8-alpine`、buildkit 保持 `buildkit:rootless`） |
| 2 | `kubectl -n sandlock get pods` | ✅ **9/9 Running**：control-plane ×2（2/2）、e2b-c3-agent ×2（2/2）、e2b-worker-0/1（1/1）、redis（1/1）、seccomp-installer ×2（1/1） |
| 3 | `GET /sandboxes` | ✅ **`[]`** |
| 4 | `DRY_RUN=1 apply.sh \| kubectl diff -f - \| wc -l` | ✅ **0 行**（仓库规格 ≡ 线上） |

**N61 的探针现场读数**（本节上线时，`deploy/scripts/acceptance/probe_ttl_sweep_reap.py`，只读）：
舰队 **0 条记录** ⇒ record 侧空转；`e2b:ttl:sweep` 60 次采样 **59/60 被持有** ⇒ 扫描器确在运行，
**排除"根本没在问"这一支**。那 8 条为什么没被收的**定因**仍留批 B。

**两条冒烟**

- `MULTI-NODE SMOKE OK`：4 箱 2+2、commands / files / health / stdin 全过，kill 后两边预约
  **0/0**。
- `DEPLOYMENT SMOKE OK`：六段全过（命令+文件、跨节点迁移保文件、网络配置、远端卷+兄弟卷隔离、
  模板构建 → registry push → worker pull → rootfs、MCP 网关），
  `after kill reservations: {'e2b-worker-0': 0, 'e2b-worker-1': 0}`。

**两次非预期读数（都如实记，别抹掉）**

1. **`deployment_smoke` 第 1 次卡在模板构建段**：`404: Template build bld_2e943c72adb355a3
   not found`。这是 `docs/reports/fix-c-report.md` 记过的**跨副本 poll 形态**：两个控制面副本的
   访问日志里 `logsOffset=0..31` 全部由 `…-r5nhz` 答 `200 OK`，唯一一次 `logsOffset=34` 落到
   另一个副本 `…-k6cqz` 时答 `404 Not Found`；事后该 build 记录在**两个副本**上都完整
   （14661 字节）⇒ 单次可见性/时序窗口，不是记录真丢。**重跑即过。**
2. **第 1 次重跑的末条断言红**：`after kill reservations: {'e2b-worker-0': 1024,
   'e2b-worker-1': 0}` —— 六段功能全过，但收尾"每节点预留归零"没满足。`e2b-worker-0` 的
   **内存节点视图**（`GET /internal/nodes` 的 `reservedMemoryMB`）残留一份默认规格预留
   （**1024 MB / 100 CPU / 1024 MB disk / 256 processes**）且持久不回落；而 Redis 台账
   `e2b:node:quota:e2b-worker-0` **全 0**、`GET /sandboxes` **空** ⇒ **视图与台账漂移**，
   没有活沙箱认领它。**走自愈路径**：
   `KUBECONFIG="$PWD/tmp/k0s/kubeconfig" kubectl -n sandlock delete pod e2b-worker-0`
   （StatefulSet 秒级重建、重新注册触发 `_rebuild_node_reservations` 的 `set_reserved`）后，
   BEFORE **`1024/100/1024/256`** → AFTER **全 0**；`deployment_smoke` 重跑 **EXIT=0**、
   末条 `0/0`、`DEPLOYMENT SMOKE OK`。

**日志取证：未命中。** `release_quota … found no node record`、`quota store refused …` 等点名
搜两个控制面副本 `--since=3h` **零命中**；两个 sandbox 都是完整的 `migrate 200 → DELETE 204`
闭环，没有半截生命周期。**这条漂移在控制面日志里不留痕。**

**怀疑机制（未证实，登记 N70）**：节点行是一个**共享 Redis 整行**（`_load_locked` 读整行 →
改 → `_persist_locked` 整行 put，**无 CAS/版本号**），而配额台账是**另一个 key** 的原子
`HINCRBY`（`e2b:node:quota:<node>`）。心跳（`heartbeat()` 的 load→改→persist）与 delete 的
`release_quota` 并发时丢更新：副本 A 把台账 `HINCRBY -1024` → 0 并把行 put 为 `reserved=0`，
副本 B 在 A 之前读到旧行、在 A 之后把**带着旧 `reserved=1024` 的整行** put 回去 ⇒ 行 = 1024、
台账 = 0、**无 WARNING**、只有重注册时按活记录 `set_reserved` 能把它压回 0。**要坐实得按该
时序做并发复现，或在 `_persist_locked` 上加版本/CAS —— 都超出本批范围，只登记。**

**本批的缓解（N60）**：逐候选重试让"一个节点漂移"不再变成**全舰队 503** —— 被拒的候选只让
那次放置**跳过该节点**并打具名 WARNING，另一台还有空位就能落地（§7.33.4 里那两次红灯正是
"漂移 + 不换候选"叠出来的）。**但它没有治漂移本身**，那是 N70。

#### 7.34.1 操作教训：`apply.sh` 没有 `-h` 分支，探用法只能读脚本头注释

`deploy/k8s-k0s/apply.sh` **没有 `-h/--help` 分支**：传任何参数它都照走正常流程。上线准备阶段
一位实现者为探它的用法跑了 `apply.sh -h`，而那条命令**没带 `KUBECONFIG`** ⇒ 对**默认 context
的阿里云 ACK 集群**执行了真实 apply，在那边新建了 `sandlock` namespace（`2026-10-02T17:37:07Z`）
与整套起不来的负载，外加一个 Bound 的 50Gi NAS PVC（`sandbox-shared`，SC
`alibabacloud-cnfs-nas`）。**目标 k0s 集群未受影响**；ACK 侧的清理需要用户批准、不在本批范围。

规则：**探用法只能读脚本头注释**；**任何 `kubectl` / `apply.sh` 调用都必须在同一条命令里带
`KUBECONFIG=$PWD/tmp/k0s/kubeconfig`**（写进命令里，不要靠 shell 已有的环境），否则会打到默认
context 的 ACK 集群。本节所有命令示例都带这个前缀：

```bash
KUBECONFIG="$PWD/tmp/k0s/kubeconfig" kubectl -n sandlock get pods
KUBECONFIG="$PWD/tmp/k0s/kubeconfig" DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null \
    | KUBECONFIG="$PWD/tmp/k0s/kubeconfig" kubectl diff -f - | wc -l   # 期望 0
```

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
`e2b-priv-broker` DaemonSet 里（§7）。集群层设计的全貌见 `docs/k8s-deployment.md` ——
**读它时注意**：那份指南里 §13（N13 共用 base）与 §23.3/§24（N27 回退、C1 属主迁移）
测的是**介质翻转之前**的形状，各自已经加了"历史记录 / 前提已被取代"的框；当前形态以本文件
的 §7（含 §7.34）为准。

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
