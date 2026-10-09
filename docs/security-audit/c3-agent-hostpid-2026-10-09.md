# c3-agent 的 `hostPID` 收敛评估（2026-10-09）

**结论：`hostPID: true` 拿不掉 —— 它是面 A 身份授予与面 B 两处反查的承重件，拿掉等于"建箱
全挂"（own identity 是唯一被接受的形态，`E2B_IDENTITY_GRANT=agent-grant`）。**

**但它的可达面可以收窄，而杠杆不在 `hostPID` 上，在"谁跟它共用 uid 65534"。** 实测（本地 lane，
见 §2）：同一 uid 的进程对它是**完全可读**的（`environ` / `maps` / `mem` 有效地址 / `kill`），
异 uid 的只剩元数据（`status` / `cgroup` / `cmdline`）。而 65534 这个全节点集合里包含
**控制面主容器**（`deploy/k8s/control-plane.yaml` 的 `runAsUser: 65534`）⇒ 一个被攻破的 agent
可以读控制面进程的内存，**不需要任何 capability**。

这条把 `control-plane.yaml` 里已经点名的那个代价（"CP 与 worker 在文件系统层不再可区分"）
补上了第二半：它们在 **ptrace 意义上**也不可区分，而 agent 是唯一同时具备"hostPID + 同 uid"
的那个组件。

## 1. 依赖图：谁在读宿主 `/proc`

三处读点都在 `c3_agent/lookup.py`，调用点在 `c3_agent/app.py`（授予）与 `c3_agent/cgroups.py`（委派）：

| # | 读点 | 干什么 | 靠什么 | 没有宿主 pid ns 的后果 |
|---|---|---|---|---|
| 1 | `ProcLookup.host_pid` | 把 worker 报的**容器 pid** 反查成**宿主 pid**，交给 `as_uid` 写 `uid_map` | `NSpid` 链（>1 段且末段 == 报来的 pid）+ `ns/pid` inode 精确比对 + k8s 下 pod UID 的 cgroup | 宿主 pid ns 是唯一能把"两个 worker 各自的 pid 42"分开的视图（multinode compose 就是三 worker 同机）⇒ 每个候选都不成立，全部**具名拒绝** |
| 2 | `ProcLookup.worker_uid_gid` | compose 车道（那里没有 pod spec 可读）从世界可读文件里读出 worker 自己的 uid/gid | `/proc/<pid>/cgroup`（0444）+ `/proc/<pid>/status`（0444） | 看不到 worker 的进程，反查失败 |
| 3 | `ProcLookup.worker_container_cgroup` | N83 Phase 1 的 cgroup 委派锚点（决定 chown 给 worker 哪棵树） | 同上 + 容器 init 判别 + CP 下发的 worker uid 判别式 | 同上 |

外围的两道门（不是 `hostPID` 给的，但决定它的后果）：面 A 无任何 capability（`CapEff=0`，
`SETUID`/`SETGID` 只作为 file capability 出现在 `as_uid` 上）、面 B 是 root + `DAC_OVERRIDE`
但**没有** `CAP_SYS_PTRACE`；agent 只应答 CP（NetworkPolicy + 服务自己的 token 两半）。

`hostPID` 是 pod 级字段 ⇒ 面 B 一起拿到，这一点在 `deploy/k8s/c3-agent.yaml` 的文件头与
`tests/unit/test_c3_agent_manifest.py` 里都被点名接受并 pin 住（`pod["hostPID"] is True`）；
同一个测试反向钉住 **worker pod 里不得出现它**（`assert "hostPID" not in pod`）—— 那条是
下面 §3 的关键对照。

## 2. 实测：两条臂

探针：`deploy/scripts/acceptance/probe_c3_agent_hostpid_reach.sh`（可重跑，几十秒，自清理）。
形状按 k8s 复刻：目标容器各自私有 pid ns，watcher 以 uid 65534 跑，`--pid=host` 与否对照。

**臂 ① 可见性**（开关是二值的，不是"少看一点"）：

| watcher | 可见 pid 数 | uid 0 目标 | 同 uid 65534 目标 | 池 uid 10000 目标 |
|---|---|---|---|---|
| 无 host pid ns（= `hostPID: false`） | 3 | 不可见 | 不可见 | 不可见 |
| 有 host pid ns（= 今天的形态） | 38 | 可见 | 可见 | 可见 |

**臂 ② 可达面**（uid 65534、有 host pid ns，逐文件读；`mem` 取 `maps` 首段起始地址再读，
这样才能把"权限被拒"与"地址没映射"分开）：

| 目标 | status | cgroup | cmdline | environ | maps | mem（有效地址） | `kill -0` |
|---|---|---|---|---|---|---|---|
| **同 uid 65534** | 可读 | 可读 | 可读 | **可读** | **可读** | **可读** | **允许** |
| uid 0 | 可读 | 可读 | 可读 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 池 uid 10000 | 可读 | 可读 | 可读 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

⇒ **同 uid = 等效 ptrace**（`environ` 里有环境变量、`maps` 泄漏布局、`mem` 在有效地址上真能读出
字节、`kill` 能打）；异 uid 只泄漏"存在与形状"（`status`/`cgroup`/`cmdline`）。

`mem` 那条是本评估里最要紧的一格：有效地址可读意味着 `PTRACE_MODE_ATTACH` 已经通过，
不是"读了个未映射页拿到 EIO"。

## 3. 增量为什么落在控制面上

今天的 65534 集合（每节点）：agent 面 A、所有 worker pod、**控制面主容器**。

* **面 A 必须是 65534**：`uid_map` 的 owner 规则（`docs/c3-privilege-relocation.md` §14.2.7）——
  写者的 euid 必须等于目标 user namespace 的 owner，而那个 owner 是 fork 出 slot 的 worker 的 euid。
  这条把"agent 的 uid"钉死了：换 uid 就授不出身份。
* **worker 与控制面都必须是 65534/65534**，但理由不同：worker 是"沙箱树与平台态的属主"，
  控制面是 C3 Task 5 之后不再以 root 跑、改为**以创建者的身份**写平台态
  （`control-plane.yaml` 里 `E2B_IMAGE_CACHE_OWNER_UID` 那段注释 + 同文件"CP 与 worker 不再
  可区分"的显式代价）。
* **worker 打不到 agent/控制面**：worker 没有 `hostPID`（被 pin），它自己的 pid ns 里看不见
  别人的进程 —— 即使 uid 相同。
* **agent 打得到控制面**：hostPID + 同 uid ⇒ §2 臂 ② 那一行。

也就是说：**让面 A 能工作的那个属性（看得见别人的 pid），顺手把控制面放进了 agent 的信任域**；
而 agent 是这套设计里唯一的 root 邻接组件（同 pod 的面 B 是 root）。

**同源的另一半（文件）早就被点名了**：`control-plane.yaml` 那段注释自己写着"CP 与 worker 在
文件系统层不再可区分"—— 同一个 uid 里，`0600`/`65534` 的文件对任何同 uid 进程都可读，
所以"把秘密换文件 + `chmod 0600`"从来不是这条的缓解。本评估补上的正是另一半：**进程内存**
同样分不开，而且不需要 capability。

## 4. 被否决的路径（逐条给理由，避免下一轮重走）

| 路径 | 为什么不行 |
|---|---|
| 去掉 `hostPID`（改一行） | §1 三处读点全断 ⇒ 建箱全挂；臂 ①a 的读数就是它的直接后果 |
| 给节点 `/proc` 挂 `hidepid=2` | 正是把面 A 需要的"别人进程可见性"一起藏掉 |
| 拆成两个 DaemonSet（只有面 A 带 `hostPID`） | 可达面的增量在面 A 身上，拆了不减少；面 B 也读宿主 `/proc`（读点 2/3），拆开要各自一套 token/端口/NetworkPolicy；而面 B 是 root + `DAC_OVERRIDE`，比"pid 可见性"更重 |
| pidfd 从 worker 交接宿主 pid | 写 `uid_map` 要的是 **pid 数字**（procfs 路径）；pidfd 给不出路径，只能借道共享卷上的 unix socket + fdinfo 反读 —— 在 NFS 上不可靠，还引入一条新信道 |
| 让 agent 换个 uid（不与 worker 同 uid） | owner 规则（§14.2.7）⇒ 换 uid 就授不出身份 |
| 把 `SETUID`/`SETGID` 交回 worker，让 worker 自己写 `uid_map` | 那正是 C3 搬走的权限；worker 拿的是**宿主 userns** 里的 `CAP_SETUID`，等于能映射任意 uid |

## 5. 唯一有真实收益的收敛：把控制面移出 uid 65534

**收益**：agent 的"等效 ptrace"可达集从 {worker, 控制面} 缩到 {worker}；控制面内存里的
internal key / secret master key / 租户凭据不再落在 agent 的可读范围内。

**代价（必须点名，不是一行配置）**：平台态（`state/**`、`_images`、`_secrets`、`_templates`、
`_builds`、`_snapshots`…）今天都是 `65534:65534`，控制面写得动的前提是它就是这个属主。
移出去有两条：

1. **专用 uid**：给控制面一个自己的 uid，并把平台态属主迁过去 —— 形状与
   `deploy/scripts/migrate-state-owner.sh`（C1 wave 2 Task 6，递归 `chown` 的一次性迁移）同类，
   但要连带推演 worker/agent 对这些路径的读权限（现在靠属主同 uid，之后要靠权限位或建组）。
   这正是 `control-plane.yaml` 里写的"a dedicated uid plus a second migration package"。
2. **建组**：平台态保持 65534 属主、加一个共享组并给 `g+w`，控制面以另一个 uid + 该组写。
   改动面小一些，但要在同一批里把 setgid 位与每个写点的 umask 一起钉住（否则新文件又落回
   65534 单一属主）。

两条都需要**维护窗口 + 迁移脚本 + 回退杆**，并且要按 §6 重跑差分。**本轮不做**（它是 production
配置变更，得单独拍板）。

（这条已单独评估：**可行，且比当时的估算便宜** —— 机制要从"chown 迁移"改成"共享组 + setgid"，
因为 `_images` 是控制面与 worker 的双写目录；两条过期阻塞项已在那边订正。见
[`control-plane-uid-2026-10-09.md`](control-plane-uid-2026-10-09.md)。）

## 6. 关闭判据（对照 `docs/security-architecture.md` §11 那一行）

那一行的原文判据是 `差分确认面 A/B 与 storage-init 行为不变`：

* **本次没有改动任何配置** ⇒ 该差分自动成立；本轮的产出是"不改，且把代价写清"这个决定，
  加上 §2 的可达面读数。
* 若采纳 §5 的收敛，差分必须重跑：面 A（授予身份）、面 B（改文件）、`storage-init` 三条，
  外加平台态读写（它正是迁移会动的那部分）。

## 7. 残余不确定性（诚实边界）

* 节点上 `/proc` 的挂载选项（`hidepid`）**没有**在集群上直接读过。推理是：面 A 在生产可用 ⇒
  worker 的 pid 可见 ⇒ `hidepid` 不是 2。要落死这条，一条 `kubectl exec` 读
  `/proc/self/mountinfo` 即可（属只读操作）。
* 本读数是本地 docker lane（macOS Docker Desktop 的 Linux VM，cgroup v2）。uid/ptrace 的判定规则
  与架构无关，但"同 uid 进程是否 dumpable"这类细节受运行时默认值影响 ⇒ 采纳任何改动前，用同一支
  探针在 pod 内复核一次。
* 探针只量了"读"与 `kill -0`，没有单独量 `ptrace attach` 本身（本地镜像没有 gdb/strace）；
  `mem` 在有效地址上可读已经是 `PTRACE_MODE_ATTACH` 通过的证据。
