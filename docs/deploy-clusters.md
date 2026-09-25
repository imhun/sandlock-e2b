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
`worker-headless`（ClusterIP）。

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

## 7. 当前部署状态（2026-09-25 实测，改部署前先复核）

**版本**：`0.1.0-495-gcbe55df-20260925-100509`（= `deploy/stack/.version`；`apply.sh` 就是按它渲染的）。

**别把 `python-mcp:3.14` 当稳定引用**：`deploy/docker/Dockerfile.mcp-base` 用的是
`pip install --no-cache-dir mcp uvicorn`，**没有钉版本**，所以每次重建它都可能产出不同内容 ——
2026-09-25 这次重建后该 tag 指向 `sha256:e91b0ae2…`，而集群的 `E2B_BASE_IMAGE` 钉的仍是
`sha256:3675662d…`（**刻意保留**：这一轮只改"真根"一件事，不同时动沙箱基底；旧 digest 依旧
可解析，push 之后 worker 还成功预热过它）。**换基准镜像时按 digest 换，不要按 tag 换**，
否则会静默换掉所有沙箱的基底。

**真根（N35/N14）已上线**（2026-09-25 单节点灰度 → 推广，两台 worker）：

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

overlay 改了什么、为什么（NAS PV 必须 NFSv4.0、worker `runAsUser: 0`、容量与 resources、
Calico VXLAN 只能建集群时定）见 `deploy/k8s-k0s/README.md`；集群层设计的全貌见
`docs/k8s-deployment.md`。

---

## 9. checkpoint/restore 的上线记录（2026-09-25）

**版本**：`0.1.0-525-g65ad183-20260925-212439`（= `deploy/stack/.version`）。这一轮改了三样
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

**验收状态**（脚本 `tmp/k0s/checkpoint_acceptance.py`，每步都断言）：

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
  再 exec（`tmp/k0s/checkpoint_acceptance.py` 已按此写）。

**怎么再跑一遍**（密钥从集群里取，不写进仓库）：

```bash
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
export E2B_API_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d)
export E2B_INTERNAL_API_KEY=$(kubectl -n sandlock get secret e2b-secrets -o jsonpath='{.data.E2B_INTERNAL_API_KEY}' | base64 -d)
.venv/bin/python tmp/k0s/checkpoint_acceptance.py
```

（脚本最后会把沙箱 `kill` 掉；想留下现场排障就用 `tmp/k0s/probe_restore_state.py`，它不 kill，
并打印沙箱 id 与宿主 pod。）

**重建链条（改了 fork 就要从第一步走）**：`deploy/scripts/build-sandlock-wheels.sh`
（交叉编两个 arch 的 wheel + supervise + restore-stub，约 4 分钟）→ `deploy/scripts/build-and-push.sh`
（镜像推 ACR，层缓存命中时 1 分钟）→ `KUBECONFIG=... deploy/k8s-k0s/apply.sh`（滚两台 worker +
预热 base image，约 3–5 分钟）。
