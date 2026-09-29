# C3 判据 13 / 16 真机验收报告（compose multinode，3 worker 同机）

- 工作树：`/Users/polus/project/ai/sandlock-e2b/tmp/wt-c3`，分支 `feat/c3-consolidation`
- 起点：`9ae9d83`；本轮的三个提交：`746ad6b`（agent 镜像缺 httpx）、
  `833d20c`（槽位子进程补 `setresgid` 的一半 + 单测钉）、`6779588`（判据 13/16 的驱动 + hop 仪器）
- 未 push、未 merge、**未碰 k0s 集群**；镜像全部本地构建、栈跑在本机 OrbStack（linux/amd64）上
- 原始日志：`tmp/acc-13-16/logs/`（清单见 §8）

## 0. 结论

| 判据 | 结论 | 一句话 |
|---|---|---|
| **13** 反查 = `NSpid` 命中 **且** 目标 worker 身份 | **通过**（正臂 2 条 + 反面臂 2 条，全部真跑） | 两个 worker 各有一个**容器 pid 300** 的子进程，agent 分别解出宿主 pid `3640303`（worker-1 的 ns）与 `3640363`（worker-2 的 ns）；同一个 pid 拿 worker-3 的身份问、以及只有 worker-2 有的 pid 317 拿 worker-1 的身份问，都**逐字**具名拒。 |
| **16** CP 中继不把并发建箱串行化 | **通过**（正臂 + 串行基线 + 反面臂） | N=3（本形态 worker 数），5 轮并发：全成功、零队列超时、总耗时 2.37–2.54s **优于**串行基线 4.11–4.24s；同一套用例把 `E2B_C3_AGENT_MAX_CONCURRENCY` 压到 1 后，3 轮里 `max_in_flight` **恒为 1**（复现排队）。 |
| 并发默认值 `E2B_C3_AGENT_MAX_CONCURRENCY` | **保持 64，不改** | 量到的最大并发授权数就是形态的 worker 数 3；车队口径的上限（autoscaler 16 副本、create 准入 100、agent 线程池 40）都要求它 ≥16 且不至于成为第一个等的；64 落在中间。 |

**但这次验收的主要产出不是"绿"，而是四个阻塞项**（§6）：前三个让 compose 车道**一条都建不成**、
或建成了也跑不起来；第四个是设计性的，直接限制了该车道"每 worker 同时只能有一个活槽位"。
其中两个已修（`746ad6b` / `833d20c`，各带证据与钉子），两个**需要裁定**（D2 / D4）。

---

## 1. 环境与命令

### 1.1 本机形态

```
$ docker version --format '{{.Server.Version}} {{.Server.Arch}} {{.Server.Os}}'
29.4.0 amd64 linux          # OrbStack 的 Linux VM，内核支持 userns / Landlock / pid ns
$ docker compose version
Docker Compose version v5.1.2
```

### 1.2 镜像（全部从本树构建，`PUSH=0`）

```
$ docker buildx build --load -f deploy/docker/Dockerfile.control-plane-gateway \
      -t e2b-sandlock-control-plane-gateway:c3-acc .
$ docker buildx build --load -f deploy/docker/Dockerfile.agent     -t e2b-sandlock-agent:c3-acc .
$ docker buildx build --load -f deploy/docker/Dockerfile.envd      -t e2b-sandlock-worker:c3-acc .

$ docker run --rm --entrypoint sh e2b-sandlock-agent:c3-acc -c \
    'getcap /var/lib/e2b-priv/as_uid /var/lib/e2b-priv/e2b-maint; id'
/var/lib/e2b-priv/as_uid cap_setgid,cap_setuid=ep
/var/lib/e2b-priv/e2b-maint cap_chown,cap_dac_override=ep
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)

$ docker run --rm --entrypoint sh e2b-sandlock-worker:c3-acc -c \
    'id; ls /var/lib/e2b-priv 2>&1 | head -1; python -c "import sandlock, envd_service.route_b; print(1)"'
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
ls: cannot access /var/lib/e2b-priv: No such file or directory
1
```

**`wheels/fork/` 的来源（必须记明）**：本树没有 `third_party/sandlock` 子模块
（`fd -H -t f -e whl sandlock` 为空、子模块目录为空），离线构建不出 fork wheel。所以
`wheels/fork/sandlock-0.9.0b0-cp314-cp314-manylinux_2_34_x86_64.whl` 是**从本机已有的
`e2b-sandlock-test:latest` 里把安装好的 `sandlock` 包与 dist-info 原样重新打包**的
（该镜像的构建历史里就是 `COPY wheels/fork/ /tmp/wheels/` → `pip install`；`sandlock`
0.9.0b0，含 `bin/sandlock-supervise` 与 `bin/restore-stub`）。这与 C3 的改动无关（fork 代码
本轮未动），但"worker 镜像不是用本树管子里的 wheel 建的"这句话必须留在这里。

### 1.3 起栈（compose multinode + 本地 override）

```
$ WORKER_IMAGE=e2b-sandlock-worker:c3-acc AGENT_IMAGE=e2b-sandlock-agent:c3-acc \
  docker compose -f deploy/compose/docker-compose.multinode.yml \
                 -f tmp/acc-13-16/compose.override.yml -p c3acc up -d --no-build
$ docker ps --filter name=c3acc --format '{{.Names}}\t{{.Status}}'
c3acc-worker-1-1        Up
c3acc-worker-2-1        Up
c3acc-worker-3-1        Up
c3acc-control-plane-1   Up
c3acc-c3-agent-1        Up
c3acc-c3-agent-maint-1  Up
c3acc-c3-agent-proxy-1  Up
```

`tmp/acc-13-16/compose.override.yml`（不落进任何清单）做四件事，每件都写了理由：

1. 三个服务的镜像指向本树标签 `:c3-acc`（`--no-build`，不会误建/误拉）；
2. 每节点容量提到 `4096/400/8192/1024`（清单原值 2048/200/4096/256 + 每箱 256 进程 ⇒ 每
   worker 只放得下 1 个箱子，判据 16 的 N=3 同时建箱会被自己的准入挡成 503）；
3. **`c3-agent-proxy`**：测量仪器（§1.5）；
4. **一处对本树形状的偏离**：给 face B（`c3-agent-maint`）加了 `SETUID`/`SETGID` —— 见 §6 D2，
   这是缺陷，不是便利；没有它 compose 车道连一个箱都建不成。

### 1.4 判据 13 的跑法

```
$ python3 deploy/scripts/acceptance/c3_accept_13_reverse_lookup.py \
      --logdir tmp/acc-13-16/logs --shared-pid 300
```

驱动自己先把三个 worker `--force-recreate`（干净锚点，见 §6 D4），再：
① 并发建 3 个箱（每 worker 一个，从 `/internal/routes` 核对分布）→ ② 在 worker-1/worker-2
里各起一个"rent-a-pid"子进程停在**同一个容器 pid**（`c3_accept13_slot_child_harness.py`：
fork 到指定 pid，再 exec **生产**子进程程序
`envd_service.slot_identity --uid X --unshared-fd N -- c3_accept13_slot_report.py`；
报告脚本扮演 `sandlock-supervise` 的第一步：读自己的 `policy.json`）→ ③ 四条臂，每条都由
worker 用自己的 `envd_service.priv_helpers.request_identity(pid, sandbox_id)` 上报，
走完 **worker → CP → `grant-slot` → agent → `as_uid`**。

### 1.5 判据 16 的跑法（含测量仪器）

```
$ python3 deploy/scripts/acceptance/c3_accept_16_concurrent_slots.py \
      --logdir tmp/acc-13-16/logs --rounds 5 --baseline-rounds 3 --counter-rounds 3 \
      --hop-delay 0.25
```

N 从形态的 compose 文件里数出来（`^  worker-\d+:` ⇒ **3**），不写死。每轮 = "每 worker 各建 1 个
沙箱 → 同时起它的槽位"，**测的是槽位启动**（本形态下建箱本身不 fork 槽位：route B 的
`acquire` 在沙箱第一次被用到时才发生，`envd_service/executors/sandlock.py:1811`）。

仪器 `deploy/scripts/acceptance/c3_compose_hop_proxy.py`：控制面把
`E2B_C3_AGENT_URL` 指向它（agent 的 `E2B_C3_AGENT_NODE_ID` 同步改成 `c3-agent-proxy`，
D12 要求两者一致），它原样转发到真 agent，逐请求记 `{op,start,end,status}`。
"并发"于是是个**可判对错的二进制事实**（区间是否重叠），不必从端到端耗时里猜；
`/log/delay` 可在同一进程里改延迟，保证正反两臂用同一套测量。

---

## 2. 判据 13：观察（四个臂，全部真跑）

`9ae9d83` + `833d20c` 之后的完整一次运行（`tmp/acc-13-16/logs/13-run-postfix.log`）：

```
worker pid namespaces: {1: 'pid:[4026533492]', 2: 'pid:[4026533712]', 3: 'pid:[4026533601]'}
sandbox placement {1: 'sbx_b232...', 3: 'sbx_8e50...', 2: 'sbx_5290...'} uids {1: 10000, 3: 10001, 2: 10002}
   worker-1: [shared1] HARNESS-READY container_pid=300 pidns=pid:[4026533492]
   worker-2: [shared2] HARNESS-READY container_pid=300 pidns=pid:[4026533712]
   worker-2's private child: container pid 317
```

| 臂 | 上报 | 结果 |
|---|---|---|
| **正臂 A** | worker-1 身份 + 容器 pid **300** | `200`：`hostPid=3640303`，`pidNamespace=pid:[4026533492]`，`asUid="C3-ASUID-OK pid=3640303 uid=10000"`；从 agent（`pid: host`）读 `/proc/3640303/ns/pid` = worker-1 的 ns ✔ |
| **正臂 B** | worker-2 身份 + **同一个**容器 pid 300 | `200`：`hostPid=3640363`（≠ 正臂 A），`pidNamespace=pid:[4026533712]`；`/proc/3640363/ns/pid` = worker-2 的 ns ✔ |
| **反面臂 1** | worker-3 身份 + 容器 pid 300（worker-3 没有这个 pid） | `502`，逐字：`the control plane refused the slot-identity report for sandbox sbx_8e50… (HTTP 502): the agent for node worker-3 refused the grant: container pid 300 is not in worker worker-3's pid namespace (pid:[4026533601]): refusing` |
| **反面臂 2** | worker-1 身份 + 容器 pid **317**（只有 worker-2 有） | `502`，逐字：`… the agent for node worker-1 refused the grant: container pid 317 is not in worker worker-1's pid namespace (pid:[4026533492]): refusing` |

两条反面臂的意义不同，都要：① 证明"只有 `NSpid` 命中"不够 —— 300 的 `NSpid` 链**存在**
（worker-1/2 各一条），身份不在里面就是拒；② 证明"命中属于**另一个** worker"也是拒 ——
317 的宿主进程确实在、链也命中，唯一缺的是身份。

被授予后的子进程自己的样子（同一个真实 grant 之后的 `REPORT-IDS`）：

```
[shared1] REPORT-IDS Uid=10000 10000 10000 10000 Gid=10000 10000 10000 10000 Groups=65534 NSpid=300
[shared1] REPORT-POLICY readable-groups=[10000]
[shared2] REPORT-IDS Uid=10002 10002 10002 10002 Gid=10002 10002 10002 10002 Groups=65534 NSpid=300
[shared2] REPORT-POLICY readable-groups=[10002]
```

（`readable-groups` 是 104 个 `owner=worker, group=<uid>, mode 0440` 探针里能打开的那些 ——
正是 `sandlock-supervise` 读 `policy.json` 需要的那个访问。**修 `833d20c` 之前这里是
`Gid=65534` 且 `readable-groups=[]`**，见 §6 D3。）

---

## 3. 判据 16：每轮数字（两次运行，正臂 / 串行基线 / 反面臂）

`total_seconds` = 该轮"建箱阶段 + 槽位启动阶段"的墙钟；`mif` = 仪器里同时打开的
`grant-slot` 请求数的最大值（区间重叠）；每轮 grant 数应等于 N=3。

### 3.1 主运行：`--hop-delay 0.25`（正反两臂同一仪器同一延迟，退出码 0）

正臂（pool=**64**，出厂默认）：

| 轮 | create (s) | slot (s) | total (s) | grants | mif | status |
|---|---|---|---|---|---|---|
| 1 | 1.164 | 1.246 | **2.410** | 3 | 3 | 200 |
| 2 | 1.173 | 1.196 | **2.369** | 3 | 2 | 200 |
| 3 | 1.229 | 1.164 | **2.394** | 3 | 1 | 200 |
| 4 | 1.150 | 1.224 | **2.374** | 3 | 3 | 200 |
| 5 | 1.323 | 1.217 | **2.539** | 3 | 2 | 200 |

串行基线（pool=64，逐个起 3 次）：

| 轮 | create (s) | slot (s) | total (s) | grants | mif |
|---|---|---|---|---|---|
| 1 | 3.032 | 1.079 | **4.111** | 3 | 1 |
| 2 | 3.158 | 1.068 | **4.225** | 3 | 1 |
| 3 | 3.118 | 1.121 | **4.239** | 3 | 1 |

反面臂（`E2B_C3_AGENT_MAX_CONCURRENCY=**1**`，同一条用例）：

| 轮 | create (s) | slot (s) | total (s) | grants | mif | status |
|---|---|---|---|---|---|---|
| 1 | 2.145 | 1.773 | **3.918** | 3 | **1** | 200 |
| 2 | 1.284 | 1.682 | **2.966** | 3 | **1** | 200 |
| 3 | 1.227 | 1.604 | **2.831** | 3 | **1** | 200 |

- ① 全部成功：5/5 轮、15/15 个 create 201、15/15 次槽位启动 `exit 0`；**零**
  `E2B_CREATE_QUEUE_TIMEOUT_S`（30s）命中（最差一轮 2.539s）；
- ② 无超线性退化：并发最差 **2.539s** vs 串行基线最差 **4.239s**（并发还快 40%）；
- ③ 反面的判别性是硬事实：**正臂 mif 达到 3，反面臂每轮恒为 1**（"同一时刻只有一条 grant"）
  —— 排队不是猜出来的，是仪器区间不重叠。

### 3.2 复跑（`--hop-delay 0`，无延迟注入）

| 组 | 每轮 total (s) | mif |
|---|---|---|
| 正臂 pool=64 | 3.571 / 2.915 / 2.422 / 2.282 / 2.282 | 3 / 1 / 3 / 2 / 1 |
| 串行基线 | 5.187 / 4.256 / 4.131 | 1 / 1 / 1 |
| 反面臂 pool=1 | 2.768 / 3.206 / 2.952 | **1 / 1 / 1** |

同一结论：并发不慢于串行；反面臂恒 1。（无延迟时正臂有 2 轮 mif=1 —— 一跳只有几毫秒，
建箱阶段把三次启动摊开了；这正是不注入延迟就"看不全"的原因，也是主运行用 0.25s 的理由。
反面臂在两种设置下都恒为 1，所以判别力不依赖延迟。）

### 3.3 每轮的形态

每轮 3 个箱都落在 3 个不同 worker 上（`placement` 每次都打出来，如
`{worker-3: sbx_edd3…, worker-1: sbx_bae9…, worker-2: sbx_3a82…}`），所以"3 条 grant 并发"
就是"3 个 worker 同时在起槽位"。

---

## 4. 并发默认值的裁决：**保持 64，不动**

`control_plane/config.py::c3_agent_max_concurrency` 出厂 64 的下界理由是"≥ 一个控制面同时
可能挂起的槽位启动数"。**本轮实测到的上界只有形态的 worker 数**：

- 本形态：N = 3（3 个 worker 同机）⇒ 最大同时 grant 3；仪器实测 mif 最多 3，与之一致。
- 车队口径：autoscaler 上限 16 个 worker 副本、create 准入 100（`E2B_CREATE_QUEUE_MAX`）；
  agent 侧同步 handler 走 anyio 线程池（默认 40）。
- 64 ≥ 16（车队副本上限）、≥ 3（本形态实测），且 < 100（不先于准入容量成为第一个等待的）。

⇒ **不需要移动**。需要留意的是：在 compose 车道，真正的并发天花板**不是**这个池，而是 §6 D4
（锚点要求"该 worker 的 pid namespace 里恰好一个进程"）——它把该车道压到**每 worker 1 个活槽位**，
所以本形态能出现的并发授权数永远是 ≤ 3；把池调到 16 或 64 在这条车道上都不产生差别。

---

## 5. 判据 13/16 之间的一条结构性事实（先说清，免得误读）

**"建箱"本身不走 CP→agent 的槽位授权那一跳**：route B 的槽位是在沙箱**第一次被使用**时
才 fork 的（`envd_service/executors/sandlock.py::_open_route_b_instance`）。只跑
`POST /sandboxes` 的话，仪器里一条 `grant-slot` 都不会出现（本轮实测：一次"建箱 + 杀箱"
的 hop 日志为空，只有 face B 的 `chown`/`rm`）。所以判据 16 的"槽位启动"必须把第一次 exec
算进同一轮 —— 本驱动就是这么做的，每轮的 `slot_seconds` 就是那一跳。

---

## 6. 四个阻塞项（这次验收的主要产出）

### D1 已修：agent 镜像缺 `httpx` ⇒ 服务根本起不来

本树构建的 agent 镜像里 `deploy.c3_agent.app` 的 import 就失败（Task 6 的 `scan.py` 无条件
import httpx，而 Dockerfile 的 pip 行还是 Task 2 的三个包）：

```
File "/app/deploy/c3_agent/app.py", line 105, in <module>
  from deploy.c3_agent.scan import InventoryScanner, scanner_for
File "/app/deploy/c3_agent/scan.py", line 46, in <module>
  import httpx
ModuleNotFoundError: No module named 'httpx'
```

⇒ 服务 crash-loop，**每一条 CP→agent 指令都是 502**；k8s 车道用同一个 Dockerfile，
同样起不来（也就是说 C3 当时在任何形态上都不可用）。修：`httpx==0.28.1`（取
`requirements.txt` 的 pin）加进同一条 pip 行 + 注释说明理由（`746ad6b`）。

### D2 **待裁定**：face B 的固定能力集（`CapEff=0xb`）生不出身份解析子进程

`SubprocessWorkerIdentityResolver` 用 `user=65534, group=65534` 起子进程
（`E2B_C3_AGENT_RESOLVER_UID/_GID`），而从 root 切到 65534 需要 `CAP_SETUID/SETGID`；
face B 的能力集被钉死为 `CHOWN+DAC_OVERRIDE+FOWNER`（判据 4）。实测（本树 agent 镜像）：

```
$ docker run --rm --user 0:0 --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE \
      --cap-add FOWNER --entrypoint python3 e2b-sandlock-agent:c3-acc -c '…user=65534…'
euid 0 CapEff 000000000000000b
child spawn refused: PermissionError [Errno 1] Operation not permitted

$ … 再加 --cap-add SETUID --cap-add SETGID
euid 0 CapEff 00000000000000cb
child: 0 uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
```

现场表现（本机 19:2x，override 尚未加那两个 cap 时）：

```
envd_service.agent_fileops.AgentFileOpsError: … (HTTP 502): the agent for node
worker-2 refused the chown: could not run the identity resolver as 65534:65534:
Operation not permitted
```

⇒ compose 车道**每个带锚点的文件操作**都拒（建箱路径的第一步就是 `chown-workspace`），
也就是**一个箱都建不成**。本轮为了把判据 13/16 量出来，在**本地 override**里给 face B 加了
`SETUID`/`SETGID`（§1.3 第 4 条，改动可删、已记录）。两条路，都要裁定：

1. 给 face B 加 `SETUID/SETGID`（就等于**改判据 4** 的"恰为 0x0b"，需要配套改
   `deploy/k8s/c3-agent.yaml` 与那份 pin）；或
2. 把"读内核"这一步挪到 face A（它本来就是 65534、本来就有那两条 cap，且它已经
   `pid: host`）—— 代价是 face B 要有一条到 face A 的调用（不是 worker↔agent 那条禁用通道，
   但确实是一条新的进程间通道，属于设计决定）。

### D3 已修：槽位子进程只拿了身份的一半（没有 `setresgid`）

见 `833d20c`。摘要：`as_uid` 写 `X X 1` 进 **uid_map 与 gid_map**，route-B 的槽位文档是
`owner=<worker>, group=X, mode 0440`，而 `sandlock-supervise` 起来第一件事是读
`policy.json`；新的 agent-grant 子进程只 `setresuid(X)`，egid 停在命名空间里未映射的 65534。
真机证据（修之前，同一个真 grant 之后）：

```
REPORT-IDS Uid=10001 … Gid=65534 65534 65534 65534 Groups=65534
REPORT-POLICY readable-groups=[]
e2b: Command exited with code 127 … sandlock-supervise: policy read failed:
  read policy file /var/lib/e2b-sandboxes/.route-b/10000/<sbx>/policy.json: Permission denied
```

对照旧路 `deploy/priv/slot_spawn.c`：`setgroups([]) -> setgid(X) -> setuid(X) -> execve` —— 两半都设。
修后同一条臂：`Uid=10000 Gid=10000 Groups=65534`、`readable-groups=[10000]`，
`commands.run("echo hello-slot")` 返回 0。钉子：
`tests/unit/test_route_b_slot_identity.py::test_the_child_takes_both_halves_of_its_identity`
（成对调用、gid 先、第一次 uid 失败后成对重试、**绝不**调 `setgroups` —— `as_uid` 必须写
`setgroups=deny`，调它只会 EPERM 卡死）。

### D4 **待裁定**：compose 的锚点谓词（"该命名空间里恰好一个进程"）与运行中的槽位互斥

`ProcLookup.worker_uid_gid` 要求锚点命名空间里**恰好一个**进程。而槽位的
`sandlock-supervise`（uid = 槽位 uid）**就活在 worker 的 pid namespace 里** —— 于是只要该
worker 上有一个活槽位，任何**带 worker 身份**的文件操作都会被具名拒成
"holds more than one process (ambiguous)"。实测：

```
# 4 个箱（每 worker 2 个）的标准 smoke：
after kill reservations: [('worker-3', 1024), ('worker-2', 0), ('worker-1', 0)]
e2b.exceptions.SandboxException: 502: Node worker-3 failed to provision:
# 对应 worker 日志（同一个根因，两条路径都点名）：
… refused chown-workspace for sandbox sbx_4082… (HTTP 502): the agent for node
  worker-3 refused the chown: worker worker-3's pid namespace (pid:[4026533705])
  holds more than one process: refusing (ambiguous)
… refused walk-workspace  for sandbox sbx_92df… (HTTP 502): … holds more than one
  process: refusing (ambiguous)

# 该 worker 的命名空间里到底有谁（容器内 root 视角）：
--- worker-3
   pid=1   uid=65534 state=S python -m envd_service
   pid=461 uid=10000 state=S …/sandlock/bin/sandlock-supervise --policy …
   pid=474 uid=10000 state=S …/sandlock/bin/sandlock-supervise --policy …
   pid=475 uid=10000 state=S …/sandlock/bin/sandlock-supervise --policy …
   pid=476 uid=10000 state=T /bin/sh -c trap '' TERM HUP INT QUIT USR1 USR2 PIPE; …
```

同一根因还有两个更轻但更常见的触发面：

* **任何 `docker exec` / `kubectl exec` 进 worker**（运维看一眼、exec 型健康检查）都会让该
  namespace 瞬间有 2 个进程 ⇒ 那段时间里这个节点所有带身份的 C3 文件操作全部具名拒；
* **未被回收的子进程（zombie）也算**：本轮我自己 SIGKILL 掉一个"rent-a-pid"子进程后，
  `/proc/<pid>/stat` 的 `state=Z` 仍被枚举计数，于是该 worker 在容器重建之前一直拒
  （证据见 §8 的 `D4` 与 13 的运行日志）。

影响：compose 车道**每 worker 同时只能有一个活槽位**（第二个箱的 `scope-slot-document` /
`chown-workspace` 会拒），且"锚点"把"worker 自己的进程"与"worker 命名空间里恰好只有一个
进程"混为一谈。判据 13 的反查本身是对的（它用 ns 身份挑候选），但这条**身份锚点谓词**在这条
车道上不成立。收口方向（仅供参考，属裁定）：锚点换成"命名的那个 worker 进程"（compose 侧
需要一条与 k8s `pod<uid>` cgroup 等价的 token —— `docs/c3-privilege-relocation.md`
§11.2.1 第 9 条自己记过这条残留），或至少把谓词从"恰好一个"改成"能唯一定位 worker 的那一个"。

---

## 7. 差距与限制（说清哪一段不是"原样形状"）

1. **验收跑在"最小修复过"的树上**：HEAD `9ae9d83` 本身过不了（D1 让 agent 起不来、D3 让每个
   槽位读不到 policy）。本报告的数字是 `746ad6b` + `833d20c` 之后的，加上 §1.3 的本地 override
   （D2 的两个 cap + 容量 + 仪器）。三个提交都可单独回退；D2 的 cap 只在本机 override 里，
   没有落进任何清单。
2. **`wheels/fork/` 来自本机已有镜像**（§1.2），不是本树管子构建的；fork 代码本轮未动。
3. **判据 13 的臂不经过 CP 的"三步校验"里的源 IP 因子之外的东西**：它走的是真实的
   `POST /internal/nodes/{worker}/slot-identity`（fleet key + hostname 寻址 ⇒ 源 IP 因子按生产
   方式满足），但 worker 上报的 pid 是**测试造的 rent-a-pid 子进程**，不是某个真实沙箱的槽位
   子进程（真实槽位的容器 pid 不可控，拼不出"两个 worker 同一个 pid"）。被验的是 agent 的反查
   与身份匹配，这部分是生产代码。
4. **判据 16 的"建箱"是真实的，槽位启动也是真实的**，但 N 只能取到 3（形态的 worker 数）——
   这正是计划要求的 N；更大的 N 在这条车道上被 D4 挡住（每 worker 1 个活槽位）。
5. **未覆盖**：`deploy/stack/docker-compose.prod.yml` 那条栈没跑（同一套代码，但它是
   `worker-1/worker-2` 两 worker 形态）；判据 6③ 与 1 需要集群，不在本轮范围。
6. **栈还开着**（资源占用中）。拆：`docker compose -f deploy/compose/docker-compose.multinode.yml
   -f tmp/acc-13-16/compose.override.yml -p c3acc down -v`。
7. 本轮**没有**碰 k0s 集群、没有 push、没有 merge（本地标签 `:c3-acc` 只在本机）。

---

## 8. 原始证据（`tmp/acc-13-16/`，不入 git）

| 文件 | 内容 |
|---|---|
| `logs/13-run-postfix.log` | 判据 13 完整一次运行（四臂 + 断言，退出码 0） |
| `logs/13-arms.json` | 四条臂的上报/回答原文、三个 worker 的 ns、每个箱的 uid |
| `logs/13-harness-worker{1,2}.log` | 被授予后子进程自己的 `REPORT-IDS` / `REPORT-POLICY` |
| `logs/13-agent.log` | agent 侧的 4 条 `grant-slot` 访问记录（2×200 = 两条正臂，2×502 = 两条反面臂） |
| `logs/16-run-delay.log` | 判据 16 主运行（`--hop-delay 0.25`，5+3+3 轮，退出码 0） |
| `logs/16-run.log` | 判据 16 复跑（`--hop-delay 0`） |
| `logs/16-summary.json` | 判据 16 的全部每轮数字（正臂/基线/反面臂） |
| `logs/16-hop-instrument.jsonl` | 仪器原始记录（每条 CP→agent 请求的 start/end/status） |
| `logs/D2-face-b-caps.log` | `CapEff 0xb` vs `0xcb` 的两条实测 |
| `logs/D3-prefix-diff.txt` | 修之前/之后的 `_await_identity` 差异（缺的那一行） |
| `logs/D4-anchor-vs-slot.log` | 锚点被活槽位/exec/zombie 破坏的三处原始输出 |
| `dirs 与 file：tmp/acc-13-16/` | `compose.override.yml`、跃点仪器/栈的渲染配置、探针脚本、SDK venv |
| `hop-log/hop-*.jsonl` | 仪器在容器内的落点（bind mount），`16-hop-instrument.jsonl` 是它的副本 |

复现（判据 13 与 16 各一条命令）：

```bash
cd /Users/polus/project/ai/sandlock-e2b/tmp/wt-c3
# 1) 建镜像（§1.2）+ 起栈（§1.3）
# 2) 判据 13
python3 deploy/scripts/acceptance/c3_accept_13_reverse_lookup.py --logdir tmp/acc-13-16/logs
# 3) 判据 16（正臂 + 串行基线 + 反面臂；自身会切 pool 64→1→64）
python3 deploy/scripts/acceptance/c3_accept_16_concurrent_slots.py \
    --logdir tmp/acc-13-16/logs --rounds 5 --baseline-rounds 3 --counter-rounds 3 --hop-delay 0.25
```

---

# 附录：裁定 D25 之后（同一台机、同一栈、**无能力 override**）

裁定 **D25**（container id + world-readable `/proc` 解析）同时关掉 D2 与 D4；实现是
`ce6f3d6`（代码 + 测试 + 三份 compose 的约束注释 + §11.2.1）。本附录是**改完之后**重新量的
数字 —— 与本报告正文的唯一差别是：**`tmp/acc-13-16/compose.override.yml` 里给 face B 加的
`SETUID`/`SETGID` 已经删掉**（那两行是 D2 未定时的本地探针），face B 跑的就是清单里的能力集。

## A.1 形状确认（先证"没有靠能力换答案"）

```
$ docker exec c3acc-c3-agent-maint-1 grep CapEff /proc/self/status
CapEff: 000000000000000b            # = CHOWN | DAC_OVERRIDE | FOWNER，判据 4 原样
$ docker exec c3acc-c3-agent-maint-1 id
uid=0(root) gid=0(root) groups=0(root)

$ docker exec c3acc-c3-agent-maint-1 python3 -c "import deploy.c3_agent.lookup as l"
Subprocess resolver (must be False): False      # 降 uid 的子进程解析器已删除
WorkerAnchor: True  resolver_uid: False         # E2B_C3_AGENT_RESOLVER_* 旋钮已删除
```

契约车道在真内核上把这两条事实立成前提（`tests/contract/test_c3_worker_kernel_identity.py`，
7 passed）：同一个 face B 形状的进程 **`readlink ns/pid` 被拒**（`CapEff == 0xB`），
而 `/proc/<pid>/cgroup` / `/proc/<pid>/status` **读得到**，且 cgroup 里逐字带着容器 id。

## A.2 判据 16：正臂 / 串行基线 / 反面臂（N=3，`--hop-delay 0.25`，退出码 0）

| 组 | 每轮 total (s) | 每轮 `max_in_flight` | grants/轮 | status |
|---|---|---|---|---|
| 正臂 pool=**64** | 2.385 / 2.372 / 2.502 / 2.361 / 2.619 | **3 / 3 / 3 / 3 / 3** | 3 | 200 |
| 串行基线 | 4.341 / 4.128 / 4.279 | 1 / 1 / 1 | 3 | 200 |
| 反面臂 pool=**1** | 3.030 / 3.609 / 3.034 | **1 / 1 / 1** | 3 | 200 |

（warm-up：3.364s、`max_in_flight=3`。四条断言全过：全部成功、最差一轮 2.619s ≪ 30s 的
`E2B_CREATE_QUEUE_TIMEOUT_S`、并发不慢于串行（2.619 vs 4.341）、反面臂恒 1 条在飞。）

复跑（`--hop-delay 0`，同一结论）：正臂 2.294 / 2.002 / 2.186 / 2.225 / 2.237s
（`max_in_flight` 3/2/3/3/2），串行基线 3.587 / 3.582 / 3.848s，反面臂 2.610 / 2.405 / 2.056s
（`max_in_flight` 恒 1）。

正臂每一轮的 `max_in_flight` 从正文那一版的 1–3 变成**恒 3**：D25 去掉了一次子进程与一次
uid 切换，授权那一跳更短更均匀，三次启动落在同一个窗口里。反面臂不受影响（池在 CP 侧）。

原始日志：`tmp/acc-13-16/logs/16-run-d25.log`、`16-run-d25-nodelay.log`、
`16-summary.json`（被后一次运行覆盖，逐轮数字以上面两张表为准）、`16-hop-instrument.jsonl`。

## A.3 `multinode_smoke`：**现在通过**（4 个箱、每 worker 2 个）

```
$ E2B_API_URL=http://127.0.0.1:3100 E2B_SANDBOX_URL=http://127.0.0.1:3100 \
  E2B_API_KEY=local-key E2B_INTERNAL_API_KEY=internal-key \
  python deploy/scripts/multinode_smoke.py
NODE DISTRIBUTION: {'http://worker-1:49983': 2, 'http://worker-2:49983': 1, 'http://worker-3:49983': 1}
ALL sandboxes: commands + files + health through gateway OK
stdin through gateway OK
after kill reservations: [('worker-1', 0), ('worker-2', 0), ('worker-3', 0)]
MULTI-NODE SMOKE OK
```

改前它在同一处具名拒（`holds more than one process: refusing (ambiguous)`，见
`logs/D4-anchor-vs-slot.log`），并且 worker-1 上**两个箱**正是 D4 说的那个形状 —— 所以
"4 个箱"同时是 D4 的验收。日志：`tmp/acc-13-16/logs/smoke-after-d25.log`。

## A.4 判据 13：复跑仍全绿（槽位路径按裁定未动）

`--shared-pid 300`：worker-1/worker-2 各有一个容器 pid 300 的子进程 → 分别解出宿主 pid
`3663037`（worker-1 的 ns）与 `3663111`（worker-2 的 ns）；worker-3 用同一个 pid、
worker-1 用只有 worker-2 有的 pid 317，都**逐字**具名拒；被授权的两个子进程
`Uid=10000 Gid=10000 Groups=65534 readable-groups=[10000]` /
`Uid=10002 Gid=10002 … readable-groups=[10002]`（`833d20c` 的 gid 半边仍然成立）。
日志：`tmp/acc-13-16/logs/13-run-d25.log`。

## A.5 D25 之外仍然成立/新记的一件事（放进正文的 D2/D4 里读）

**D2/D4 都关了，但锚点本身仍是 worker 自报的**：`containerID` 在 register/heartbeat 里上报，
CP 只做形状校验。它比 pid namespace **强**在可被内核复核（agent 要求它出现在候选进程的
cgroup 路径里，所以"报一个不存在的 id"是具名拒，不是错身份），**弱**在它不证明"这个进程
就是那个 worker"（同机、同 uid 的候选彼此可读）。影响面窄（同机、同 uid、身份值相同或
仅 gid 不同）；收口方向仍是 k8s 那条 `pod<uid>` 式的更强 token。文档写在
`docs/c3-privilege-relocation.md` §11.2.1 第 9 条末尾。

另：**同一个容器的进程并不共享一个 uid**（D25 的一步发现，实测 `uid=65534 NSpid=[host,1]`
与 `uid=10000 NSpid=[host,71]` 同在一个 cgroup）—— 所以"候选之间一致"必须**配合**
"候选必须是容器 init" 才够；单靠一致性会把每个正常 worker（有一个活槽位时）判成歧义。
这条实测与理由在 `ProcLookup.worker_uid_gid` / `_is_container_init` 的注释里，并有
`test_a_busy_container_still_resolves_to_the_workers_identity` 与契约车道的
`test_a_sandbox_shaped_process_does_not_hijack_the_identity` 两条钉子。

## A.6 D25 轮的提交

| 提交 | 内容 |
|---|---|
| `ce6f3d6` | feat(c3)：face B 的 worker 身份改由 container id + cgroup 解析（D25）—— 代码、三个 compose 栈的 `hostname:` 约束注释、§11.2.1、单测 26 + 契约 7（含 D4 回归钉） |

（本轮之前的三个提交见正文 §0：`746ad6b` / `833d20c` / `6779588`。）
