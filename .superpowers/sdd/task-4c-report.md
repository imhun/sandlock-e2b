# Task 4 片 C 报告：D21 选项 2 —— compose 车道的身份改由内核推导

**范围**：`control_plane/worker_identity_source.py`（新 `KernelWorkerIdentitySource`）、
`control_plane/api/internal.py`（记声明 + 下发锚点）、`control_plane/c3_agent_client.py`
（指令体带锚点）、`deploy/c3_agent/lookup.py`（内核读取 + `resolve-worker` 入口）、
`deploy/c3_agent/app.py`（face B 的锚点分支）、`deploy/c3_agent/config.py`（resolver 身份旋钮）。
**不动**：k8s 车道（`K8sWorkerIdentitySource` 与指令报文逐字不变）、任何部署清单（compose 三栈的
`pid: host` 是片 B 落的，本片只消费它）。

## 1. 形状（与裁定的对应关系）

```
worker 上报 pidNamespace + workerUID/GID ─→ CP：pidNamespace 落节点记录（D9.3，形状校验），
                                            workerUID/GID 记为**待内核确认的声明**
worker 请求 file-op ─→ CP 用自己的记录推导路径/uid，取**自己记录的** pidNamespace 作锚点
                    ─→ 指令体 worker={uid, gid, node_id, pid_namespace}
face B：有锚点 ⇒ 子进程（以 worker 的 uid 运行）按锚点找宿主进程、读 /proc/<pid>/status
        的 Uid/Gid 第二列 ⇒ **内核值**就是身份；声明不一致/无进程/多进程 ⇒ 具名 502，不 exec
        无锚点 ⇒ 用 CP 送来的值（k8s：pod spec 已校验），与片 A 之前逐字相同
```

「声明不可信」的落点：值从未被当作答案使用——compose 里它只与内核值比较；不相等即拒。因此
「worker 点名别人的 uid」得到的是拒绝，不是别人 uid 的树/`policy.json`（C3 §14.3）。

## 2. 为什么读取跑在**子进程**里（片 C 实测出来的内核约束）

`readlink(/proc/<pid>/ns/pid)` 走 `ptrace_may_access`：只有**同 uid**（或持 `CAP_SYS_PTRACE`）
的进程能读别人的命名空间。实测（root，docker 默认能力集，**无** `CAP_SYS_PTRACE`）：

```
/proc/<worker>/ns/pid  readlink → EACCES      # face B 今天读不到
以 65534 运行的进程读同一个 link → pid:[4026532458]   # 同 uid，可读
```

face B 的能力集是 `CHOWN/DAC_OVERRIDE/FOWNER`（D22/C1 原样），且与**控制面同机 + `pid: host`**：
给它 `CAP_SYS_PTRACE` 就能 ptrace 控制面 → 比本条要收的口子更糟，**排除**。所以：

- 读取放在 face B 的**子进程**里，子进程按 `E2B_C3_AGENT_RESOLVER_UID`/`_GID`（默认
  `65534:65534`，= 三个 compose 栈 worker 的 `user:` 与 worker 镜像的 `USER`）运行；
- 子进程只做一件事：读 `/proc`，打印一行 JSON（`{"uid","gid"}` 或 `{"error":…}`），env 由父进程
  从零构造（**不带** `E2B_C3_AGENT_TOKEN`）；
- 旋钮配错 = 每条 op 具名拒绝（"holds no process this identity resolver can see (it runs as
  …)"），不会退化成"信任上报值"。

## 3. 具名拒绝（全部实测）

| 情形 | 名字（逐字） |
|---|---|
| 声明与内核不一致 | `worker <id> claims uid/gid (a, b), but the kernel says (c, d) for pid:[…]: refusing (a worker does not name the identity its privileged steps act as)` |
| 锚点无进程 | `worker <id>'s pid namespace (pid:[…]) holds no process this identity resolver can see (it runs as U:G): refusing to derive its own uid/gid` |
| 锚点多于一个进程 | `worker <id>'s pid namespace (pid:[…]) holds more than one process: refusing (ambiguous)` |
| 内核值 0 | `worker <id>'s process in pid:[…] runs as uid/gid (0, 0) according to the kernel: refusing (a worker may not run as root)` |
| 锚点不是身份 / status 不可读 | 各自的具名句（见 `lookup.py`）；CP 侧还有"节点没有锚点可确认"的 503 |

消息里**不带**宿主机 pid（带 ns 锚点与两个身份）——这样 unit 与真容器两条车道都能做**精确断言**。

## 4. 用例与 RED → GREEN

`tests/unit/test_c3_worker_kernel_identity.py`（21）：内核值即身份（含"读的是第二列/有效身份"）、
声明不一致、只有 gid 错、root worker、锚点无进程、锚点多进程、锚点非法、status 不可读、消息构造器、
两种 resolver（含子进程判答：不是一条 uid/gid 即拒、具名拒绝逐字转发）、以及 face B 端到端
（锚点→env 是内核值、三种拒绝都**不 exec**、无锚点= k8s 行为不变、锚点缺 `node_id` 是形状拒、
self-heal 的 `rm` 无身份不受影响）。

`tests/unit/test_c3_fileops_forwarding.py`（+4 条，共 46）：compose 形状落声明 + 指令带锚点、
无锚点时机 503 且不拨号 agent、两个形状的 `kernel_verified` 标记、`hostname` 模式构建出
`KernelWorkerIdentitySource`。

`tests/unit/test_c3_agent_manifest.py`（+1 条 pin，共 24）：三个 compose 栈里**每个 worker service
的 `user:` 必须等于 agent shipped 的 `E2B_C3_AGENT_RESOLVER_UID/_GID` 默认**——这条耦合（解析
子进程要按 worker 的 uid 运行才能过内核的 ptrace 规则）今天靠默认值成立，改一个 worker 的 `user:`
就会让该栈每个文件操作变成具名拒绝，所以钉住。改默认到 65533 即红。

`tests/contract/test_c3_worker_kernel_identity.py`（7，真容器/真内核）：worker 容器（65534，单进程）
+ `--pid=host` 的 reader（**docker 默认能力集**，并断言 `CapEff` 无 bit19）：
① 先证 premise——reader 自己 `readlink` 全 `/proc` 得到的候选是**空**；
② 内核答案是 `[65534, 65534]`；③ 冒充池 uid `10007` ⇒ 具名"内核说不一致"；
④ gid 错 ⇒ 具名；⑤ 锚点 `pid:[999999999]` ⇒ 具名"无进程"；
⑥ `docker exec` 加第二个进程后 ⇒ 具名"多进程"。

### RED（改之前）
- 两条 unit/contract 车道在**收集期**红：`ImportError: cannot import name 'ProcWorkerIdentityResolver'`
  / `'KernelWorkerIdentitySource'`（新代码不存在）。
- **"验证被删掉"的专项**：把 `_worker_identity()` 的解析改回"信任送来的值"（一行 `return
  body.worker.uid, body.worker.gid`），三条拒绝用例立刻红：
  `test_a_claim_the_kernel_does_not_confirm_execs_nothing`（`assert 200 == 502`）、
  `test_an_anchor_that_names_nothing_execs_nothing`、`test_an_anchor_that_names_two_processes_execs_nothing`
  —— 也就是说"退回去信上报值"会让 agent 真的去 `chown --worker` 10007。还原后 98 passed。
- **部署耦合的 pin 也有 RED**：把 resolver 默认改成 65533（worker 仍是 65534）⇒
  `test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane` 红。

### GREEN
```
$ .venv/bin/python -m pytest tests/unit/test_c3_worker_kernel_identity.py \
    tests/unit/test_c3_fileops_forwarding.py tests/unit/test_c3_agent_manifest.py \
    tests/contract/test_c3_worker_kernel_identity.py -q
98 passed

# 受影响的既有文件（14 个）
$ .venv/bin/python -m pytest <c3 相关 13 个 unit 文件 + tests/contract/test_internal_identity.py> -q
222 passed

# 整棵 tests/unit（宿主 macOS）
$ .venv/bin/python -m pytest tests/unit -q --continue-on-collection-errors
49 failed, 1888 passed, 30 skipped, 1 error
# 49 failed 全部是既有环境项（priv_helpers/priv_broker_protocol 需 root+file caps、xfs_*、
#  migrate 脚本、gateway、c2 探针）；抽验报错为 os.chown EPERM（macOS 非 root），与本片无关。
# 1 error = 既有的 fakeredis/redis 收集错误（任务书已注明）。
```

## 5. 没覆盖 / 残留

1. **锚点仍是 worker 自报**：同机同 uid 的 worker 可互读 `ns/pid`，被攻陷的 worker 理论上能冒充
   同一 uid 的另一个 worker 的名字空间（k8s 有 `pod<uid>` cgroup 证，compose 没有等价物）。
   影响窄（同机、同 uid、身份值相同或仅 gid 不同），已记 `§11.2.1 第 9 条`；收口需一条
   compose 侧 cgroup/hostname token。
2. **compose 的锚点要求"该命名空间里恰好一个进程"**：三个栈都设 `E2B_PID_NS=true`（每沙箱自己的
   pid ns），所以 worker 的命名空间只有它自己的进程；若某部署把它关掉，沙箱进程会与 worker 同
   ns ⇒ 该部署的文件 op 会**具名拒绝**（多进程），不会猜。
3. **真容器车道不覆盖**：CP→agent 的 HTTP hop、compose 清单本身、face B 的 `pid: host`（用
   reader 的 `--pid=host` 代替）；这些由 unit 车道 + 清单 pin 覆盖。
4. **resolver 旋钮**：`E2B_C3_AGENT_RESOLVER_UID/_GID` 默认 `65534:65534`，与三个栈的 worker
   `user:` 一致（本片未改清单；改 worker 的 uid 时必须同步，否则是**每个 op 的具名拒绝**）。
