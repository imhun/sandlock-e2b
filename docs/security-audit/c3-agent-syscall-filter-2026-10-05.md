# c3-agent 的 syscall 过滤：为什么 RuntimeDefault 就够，以及为什么两个 DaemonSet 不该合并

写给下一个动 `deploy/k8s/c3-agent.yaml` 的人。收的是 `remediation-SEC-R3-01.md` §8.6
STATIC-6 那五条里落地的**四条**（seccompProfile / SA token / 可写根 / face A 的 gid）；
第五条 `hostPID` 是设计上保留的，理由见 §5。

## 0. 口径

所有读数来自本机 Docker **29.4.0**（引擎 kernel `7.0.14-orbstack-…`，x86_64），镜像
`e2b-sandlock-agent:c3-task1-test`。**不是**生产节点：线上一台是 Rocky 10.2 /
kernel `6.12.0-211.34.1.el10_2` / aarch64 / containerd 2.3.4。§4 列了必须上节点复验的两条。

被攻击/被授予的进程形态统一为：`uid 65534`、`--cap-drop ALL`、BND 里放
`SETUID`/`SETGID`（file capability 必须是 BND 的子集，内核才肯在 `exec` 那一步给），
权限来自一个 `setcap cap_setuid,cap_setgid+ep` 的二进制 —— 与镜像里 `as_uid` 同形。

## 1. seccomp 档不设 NNP，所以它不破坏 file capability

**这条是 STATIC-6 第一项能修的全部理由。** `c3-agent.yaml` 的注释一直写着"不得出现
`allowPrivilegeEscalation`：它会让内核静默忽略 file capabilities"——结论对，但没说清是
哪一个机制，于是很容易被读成"加了 seccomp 就不能给 face A 加过滤"。

生产路径的形状是**后续 exec**：容器 PID 1 是 `python3 -m c3_agent`（没有 cap），它后来才
exec 带 file cap 的 `as_uid`。测量因此都必须用"未加 cap 的 PID 1 再 exec 那个二进制"这个
形状跑：

| 档 | `Seccomp` | `NoNewPrivs` | 结果 |
|---|---|---|---|
| Docker 默认档（= `RuntimeDefault` 的同类） | 2 | **0** | `GRANT-PATH-OK 10001 10001 []` |
| `deploy/seccomp/sandlock-worker.json` | 2 | **0** | `CapEff 0000000000000080`，授予路径通 |
| 上面任一 + `--security-opt no-new-privileges` | 2 | **1** | `PermissionError: [Errno 1] Operation not permitted` |

结论：**加载 seccomp 过滤器不需要 NNP**（运行时有别的路：容器 init 在掉权限之前就
带着 `CAP_SYS_ADMIN` 把档装上了）。所以 seccomp 档与 file capability 之间没有冲突；
冲突只在 `NoNewPrivs` 上，而它由 `securityContext.allowPrivilegeEscalation: false` 触发。

**一个容易测错的地方（本计划实施时踩过）**：把带 cap 的二进制直接当 entrypoint，NNP
也拦不住它 —— NNP 是在容器**第一次 exec 之后**落到进程上的，entrypoint 自己那次不受影响。
只有"后续 exec"才反映生产路径。拿 entrypoint 当控制组，会得到"NNP 无害"的错误结论。

## 2. 运行时的默认档已经够 face A 用，所以不需要第二份 profile

面的授予原语只要求三件事：`setgroups([])`、`setresgid`、`setresuid`（`as_uid.c` 走的就是
这条），加上写 `/proc/<pid>/uid_map`。这些都在运行时的默认档里，实测
（§1 表格第一行）在默认档下 `GRANT-PATH-OK`。

于是：

* **不新增** `deploy/seccomp/sandlock-agent.json`；
* **不要求** `seccomp-installer` 往节点上铺第二份档；
* **不引入** "profile 必须先于 agent 就绪"这个新的 apply 顺序；
* 不加 `pidfd_getfd`、`unshare`、`mount`/`umount2`/`pivot_root` 这些 worker 档为了建箱
  才放开的项 —— agent 一个都不需要，默认档把它们留在了能力门后面。

**不要把 worker 档套到 agent 上**：`sandlock-worker.json` 为 N35 真根无条件放开了
`mount`/`umount2`/`pivot_root`（`deploy/seccomp/README.md` 有更正记录）。那是给一个
**非 root、要建沙箱**的容器用的；给 face B（root + `DAC_OVERRIDE`）等于发一把挂载权。

## 3. 跨 pod 隔离只押在"face B 没有 CAP_SYS_PTRACE"上 —— 所以两个 DaemonSet 不该合并

"把 `seccomp-installer` 合进 `e2b-c3-agent` 那个 pod"看起来只是省一个 DaemonSet
（每节点 10m CPU / 16Mi，且它 300 秒才醒一次）。实测给出的答案是别合：

两个容器：目标跑在**自己的 pid ns**、uid 0、把一份卷挂在 `/secrets`；攻击者用
`--pid=host`、uid 0、face B 的完整能力集（`CHOWN`/`DAC_OVERRIDE`/`FOWNER`）。两组对照
都排除了干扰项 —— 两侧 `user:[4026531837]`（**初始 userns**，`uid_map` 恒等）、
两侧 `apparmor=unconfined`、`ptrace_scope=0`。

| 攻击者额外持有的能力 | 读目标挂载 | 写目标挂载 |
|---|---|---|
| 无（= face B 的实际能力集） | ✗ `Permission denied` | ✗ `Permission denied` |
| `CAP_SYS_PTRACE` | ✓ | ✓ `WROTE-OK` |

也就是说，**今天 face B 够不到 `seccomp-installer` 容器的挂载，唯一的依靠是它没有
`CAP_SYS_PTRACE`**（那条恰好也被
`tests/unit/test_c3_agent_manifest.py::test_face_b_is_the_file_face_with_c1s_capability_set`
钉着）。

合并会把 **写 `<kubelet --root-dir>/seccomp/sandlock-worker.json`** 的能力交进这个红框。
把 worker 的档换成 `{"defaultAction": "SCMP_ACT_ALLOW"}` 就能让 L6 消失 —— 而按
`security-framework.md` 自己的说法，L6 是**唯一通到宿主的那一层**。这是降级，不是提权：
不需要内核缺陷，只需要一次写文件。收益（省一个 DaemonSet）与代价不在一个量级上。

真正的收获是另一件事。给 face B 加上 `SYS_PTRACE` **确实**会让两条用例变红（实测：
`test_face_b_is_the_file_face_with_c1s_capability_set`，能力集精确相等；
`test_the_worker_and_every_agent_container_stay_outside_the_forbidden_set`，
`FORBIDDEN_TOKENS` 里就有 `SYS_PTRACE`）。但这两条守的是 **manifest 的措辞与能力集**，
不是"跨 pod 真的够不到"这个事实本身。它意味着：**换一条路拿到同样的可达性，没有任何
用例会变红** —— 比如让 installer 与 agent 共享一个挂载、给 installer 挂上 agent 已有的
某根 hostPath、或者干脆合并两个 DaemonSet（§3 开头那条路）。那才是缺的那条测试。

## 2b. 另外两条卫生字段的实测

`readOnlyRootFilesystem` 与 `automountServiceAccountToken: false` 的依据是**代码审读 +
一次运行时读数**，不是推理：

* **代码审读**：`c3_agent/` 不写自身 rootfs。会临时落盘的两处都在挂载点内 ——
  `gateway_common/paths.py` 的原子写在**目标目录旁**下 `.tmp`，`gateway_common/archive.py`
  的 staging 写在**目标树同级**；`app.py` 没有 `UploadFile`，不会往 `/tmp` spool；日志走
  stderr；`materialize.py` 全走 `dir_fd`。`__pycache__` 写不进去是 CPython 容忍的（非致命）。
* **运行时读数**（同一口径）：`docker run --read-only` + `uid 65534` + `--cap-drop ALL`
  下，uvicorn 打印 `Application startup complete.` 与
  `Uvicorn running on http://0.0.0.0:49985`，探针回
  `SERVICE-OK ('127.0.0.1', 49985)`；不带 `E2B_C3_AGENT_TOKEN` 时仍然
  `refusing to start without auth`（fail-closed 守卫没被只读根影响）。
* `automountServiceAccountToken`：`c3_agent/` 无任何 in-cluster config 或 API 调用，身份
  来自 `spec.nodeName` 的 env，`e2b-secrets` 走 `secretKeyRef`（kubelet 注入）。
  `deploy/k8s` 里只有 `control-plane` 声明了 `serviceAccountName`。

## 4. 上节点必须复验的三条

1. **containerd 的 `RuntimeDefault` 与 Docker 的默认档不是同一份实现**。§1/§2 用它代表
   `RuntimeDefault`，方向不会错，但 face A 的授予一旦被挡就是**静默**失败（表现为第一次
   建箱时 `as_uid` 拿不到 cap）。上线前在节点上跑一次真实授予，或至少跑
   `tests/security/test_agent_image_privilege.py` 那条 lane 并把它的
   `seccomp=unconfined` 换成该节点实际的档。
2. **§3 的第二行（`CAP_SYS_PTRACE`）不要在集群上跑** —— 它会真的写到另一个容器的挂载。
   要在集群上验的是第一行（无 `SYS_PTRACE` 时 `Permission denied`），且用一次性路径。
3. **只读根**（§2b）：它今天成立靠的是"这个镜像不写自己的 rootfs"。换基础镜像、加一个
   收 body 的 endpoint、或引入任何用 `/tmp` 的依赖，都会把它变成运行时故障 —— 而
   manifest 里那句 "Nothing in this image writes to its own rootfs" 不会自己变旧。
   换镜像时要重跑 §2b 的那条读数（`--read-only` 下服务真的绑定成功），不是只看代码。

## 5. 这次落地了什么、没落什么

落地（`deploy/k8s/c3-agent.yaml`，全部由
`tests/unit/test_c3_agent_manifest.py` 逐容器钉住）：

| 字段 | 值 | 位置 |
|---|---|---|
| `seccompProfile` | `RuntimeDefault` | 四个容器（两个 face + 两个 root init） |
| `automountServiceAccountToken` | `false` | pod |
| `readOnlyRootFilesystem` | `true` | 四个容器 |
| `runAsGroup` | `65534` | face A（此前落到运行时默认值） |

没落，理由都在上面：

- `allowPrivilegeEscalation: false` —— §1 的第三行就是它的反例；而且
  `test_the_agent_manifest_never_names_a_forbidden_privilege` 是**文本级**禁令，
  `test_the_worker_and_every_agent_container_stay_outside_the_forbidden_set` 上一轮 review
  特意把它从"只有 worker 和 face A"扩到了 init 容器。对已经是 uid 0 + `DAC_OVERRIDE`
  的容器，这条的收益接近零；留着毯式禁令，以后有人把带 file cap 的二进制搬进别的容器时
  才兜得住。
- 第二份 agent profile / 改 `seccomp-installer` —— §2。
- 合并两个 DaemonSet —— §3。
- namespace 级 default-deny NetworkPolicy —— 需要一份完整的东西向流量清单
  （CP↔redis/buildkit、gateway 入站、autoscaler、集群 DNS…）。没上集群核对就写下去，
  风险是下一次 `apply` 直接断服务。留给独立计划。
- `hostPID: true` —— pod 级字段，face A 的 pid 反查需要它；manifest 里已点名接受。
