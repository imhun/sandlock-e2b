# F1 可行性探针报告：非 root worker 如何起「以任意 host uid X 运行」的槽位

状态：**完成**（只测不改；产品代码、镜像、清单均未改动，全部实验在 `tmp/f1probe/` 与临时镜像里）
日期：2026-09-11 ｜ 结论先行：**走 A（file capabilities）；B（subuid+userns）在本机不可用，且即使勉强可用也要 SYS_ADMIN + 改 fork 自检。**

## 0. 环境与方法（校准噪声源）

| 项 | 实测值 | 说明 |
|---|---|---|
| 引擎 | Docker 29.4.0 / Server containerd v2.2.2 / runc 1.5.1 / OrbStack，storage driver overlay2 on btrfs | `docker version`、`docker info` |
| 内核 | `7.0.14-orbstack-00380-ga7e0a2dc9535`（x86_64） | OrbStack 自带内核 |
| 被测 worker 镜像 | `e2b-sandlock-worker-nonroot:e5.1-test`（Debian 13 trixie，镜像 USER=65534） | 内含真实产品二进制 `/usr/local/lib/python3.14/site-packages/sandlock/bin/sandlock-supervise` |
| 实验镜像 | `f1probe-worker:local` = 上述镜像 + `libcap2-bin`+`uidmap`+`gcc` + 4 个带 file cap 的 helper + 一个 root-owned `4755 /usr/local/bin/suidsh` + 一个 30 行 userns launcher | `tmp/f1probe/Dockerfile.worker-probe`（仅探针，不进产品） |
| 日志 | `tmp/f1probe-A1.log`…`tmp/f1probe-B5.log`（首行 `ENV-HEADER`，末行 `EXIT=`）；脚本在 `tmp/f1probe/`，`tmp/f1probe/run.sh` 负责加头尾 | 正文引用日志内的原始输出 |

**必须校准的两点（否则结论会跑偏）**

1. 部署清单实际声明的是 `security_opt: seccomp=unconfined`
   （`deploy/compose/docker-compose.prod.yml:147`、`deploy/k8s/worker.yaml` 的 `seccompProfile: type: Unconfined`）。
   Docker **默认** seccomp profile 会拦 `unshare(CLONE_NEWUSER)`（实测 `unshare: unshare failed: Operation not permitted`，A3a），
   但这不是部署形态；在 `seccomp=unconfined` 下非特权 userns **可以创建**（实测 `unshare -U -r id` → `uid=0`）。
   任务书里"默认 seccomp"的表述与仓库清单不一致，本报告按**清单**判定，两种形态都给了数据。
2. 本机容器 `/proc/self/uid_map` = `0 0 4294967295`，即容器与 OrbStack VM **共用 initial userns**。
   因此本文的"宿主侧 uid"= 容器宿主命名空间（OrbStack VM）看到的 uid。

---

## A. file capabilities 路线

| # | 问题 | 命令（要点） | 原始结果 | 判定 |
|---|---|---|---|---|
| A1-1 | 镜像文件系统支持 `security.capability` xattr？ | 构建期 `setcap cap_setuid,cap_setgid+ep /usr/local/bin/caphelper-setuid`；`getcap`；`python3 -c 'os.getxattr(...)'` | `getcap` → `/usr/local/bin/caphelper-setuid cap_setgid,cap_setuid=ep`；raw xattr 20 字节 `01000002c0000000000000000000000000000000`（rev2+effective，permitted=0xc0） | **支持**（overlay2 on btrfs，`/` 为 `rw,relatime`，无 `nosuid`） |
| A1-2 | 运行时还能不能打 cap？ | 以 root 进容器：`cp /usr/bin/setpriv /tmp/livehelper; setcap cap_setuid,cap_setgid+ep /tmp/livehelper` | `setcap rc=0`，`getcap` 读回 `cap_setgid,cap_setuid=ep`；对照（无 CAP_SETFCAP 的 uid 65534）`setcap rc=1` | **两者皆可**：构建期或 entrypoint（root）打 cap 都行 |
| A1-3 | 默认 BND 含 SETUID/SETGID 吗？ | `capsh --print` / `grep CapBnd /proc/self/status` | `CapBnd=0xa80425fb` = `cap_chown,cap_dac_override,cap_fowner,cap_fsetid,cap_kill,cap_setgid,cap_setuid,cap_setpcap,cap_net_bind_service,cap_net_raw,cap_sys_chroot,cap_mknod,cap_audit_write,cap_setfcap` | **含**（但 `--cap-drop ALL` 会把它清零，见 A2a） |
| A2-1 | `--user 65534` 下 file cap 是否真的进 CapEff？ | `docker run --user 65534 --cap-drop ALL --cap-add SETUID --cap-add SETGID … /usr/local/bin/caphelper-setuid 10001` | 外层 `CapEff=0`（Docker 对非 root 清空 effective，符合任务书背景）；helper 自己打印 `after setgid+setuid(10001): uid=10001 euid=10001 gid=10001 egid=10001`，`rc=0` | **是**，file cap 生效 |
| A2-2 | 带 file cap 的 `setpriv` 副本能起任意 X 吗？ | `caphelper-setpriv --reuid 10002 --regid 10002 --clear-groups id` | `uid=10002 gid=10002 groups=10002`，`rc=0` | **能**（这就是 route-B 槽位启动所需形态） |
| A2-3 | 对照：没有 file cap 的同名二进制 | `setpriv --reuid 10003 …`、`cp /usr/bin/setpriv /tmp/plainhelper …`、`chown`、`cat` | `setpriv: setresuid failed: Operation not permitted`（rc=127）；`cp` 副本同样失败；`chown` → `Operation not permitted`；`cat` 读 0700 目录 → `Permission denied` | 对照成立：能力确实来自 file cap，不是环境宽泛 |
| A2-4 | 另两个已实测约束（workspace chown、租户 0700 遍历）同一机制可行？ | `/usr/local/bin/caphelper-chown 10005:10005 f`；`/usr/local/bin/caphelper-cat /var/lib/f1probe-dac/secret`（目录 0700 root） | `after: 10005:10005`；输出 `dac-secret` | **可行**：`cap_chown+ep` / `cap_dac_override+ep` 私有 helper |
| A2-5 | 最小 cap 声明规则 | `--cap-drop ALL --cap-add SETUID` 下跑"file caps={setuid,setgid}"的 helper，以及"file caps={setuid}"的 helper | 前者 `Operation not permitted`，`rc=126`（exec 被拒）；后者成功 `uid=10001`；再要求它改 gid → `setresgid failed: Operation not permitted` | **规则：file caps 必须是容器 BND 的子集，否则连 exec 都 EPERM**；且进程只能用自己实际持有的 cap |
| A2-6 | `--cap-drop ALL` 单独用会怎样 | `--cap-drop ALL`（不加回） | BND=0，`caphelper-setuid` exec → `Operation not permitted`，`rc=126` | **`--cap-drop ALL` 必须跟 `--cap-add`**，否则 file caps 路线直接死 |
| A3-1 | no_new_privs？ | `grep NoNewPrivs /proc/self/status`（各形态） | 默认/部署形态 `NoNewPrivs: 0`；`--security-opt no-new-privileges` → `NoNewPrivs: 1` | 部署形态安全；**绝不能引入 no-new-privileges** |
| A3-2 | NNP=1 时 file caps 还灵吗 | 同上，`caphelper-setuid 10001` | `setgroups/setgid/setuid: Operation not permitted`，`rc=1` | **灵不了**（file caps 被忽略）→ 硬约束 |
| A3-3 | Docker 默认 seccomp 是否拦 `setuid`/`capset` | 不加 `seccomp=unconfined`，`Seccomp: 2`，跑 file-cap helper | helper 正常 `uid=10001`，`rc=0`（同形态 `unshare -U` 被拒） | **不拦 setuid/capset**；默认 profile 只影响 userns（B 路线关注点） |

**A 判定：在本机 Docker（OrbStack，`--cap-drop ALL` + 只加声明的那几条、`seccomp=unconfined`）下 file caps 路线完全可行。**
最小声明集合 = file caps 用到的并集，落在 **BND** 上：

```
--cap-drop ALL --cap-add SETUID --cap-add SETGID \      # 槽位启动 helper
               --cap-add CHOWN --cap-add DAC_OVERRIDE   # workspace chown / 租户 0700 管理面
--security-opt seccomp=unconfined                        # 保持现状
# 不要 --security-opt no-new-privileges
```

helper 侧（实测的 cap 组合）：`setcap cap_setuid,cap_setgid+ep`（`setpriv` 私有副本）、`setcap cap_chown+ep`（chown 副本）、`setcap cap_dac_override+ep`（读取/删除副本）。

---

## B. subuid + newuidmap 路线（rootless 容器模型）

| # | 问题 | 命令（要点） | 原始结果 | 判定 |
|---|---|---|---|---|
| B1-1 | 镜像里有 `newuidmap`/`newgidmap` 吗？ | 原镜像 `command -v newuidmap newgidmap; ls -l /etc/subuid /etc/subgid` | 两个二进制**不存在**；`/etc/subuid`、`/etc/subgid` 存在但**空文件**（0 字节）；`unshare from util-linux 2.41.5` | 需要装 `uidmap` 包（本机可装） |
| B1-2 | 装上后是否 setuid？非 root 能用吗？ | `apt-get install uidmap` 后 `ls -l /usr/bin/newuidmap`；`unshare -U -r id` | `-rwsr-xr-x 1 root root /usr/bin/newuidmap`（`newgidmap` 同）；`unshare -U -r` → `uid=0(root)`，`rc=0` | setuid 位**生效**；非特权 userns 创建**可用** |
| B2-1 | `/etc/subuid` 是哪一份、要写什么行？ | `cat /etc/subuid`；`stat`/`/proc/mounts`；再用 `-v $PWD/tmp/f1probe/subuid:/etc/subuid:ro` 注入 | 是**容器自己的 /etc**（overlay 上的 inode），不是 OrbStack 宿主；bind mount 注入同样被读到（`-rw-r--r-- 1 nobody nogroup … nobody:100000:65536`） | 由镜像或 compose 挂载提供；**不需要动宿主** |
| B2-2 | 没有 subuid 行会怎样？ | 挂载空的 `/etc/subuid` 后 `unshare --user --setgroups=deny --map-users 0:100000:1 …` | `newuidmap: uid range [0-1) -> [100000-100001) not allowed`，`rc=1`（策略拒绝，符合预期） | 委托行是必需前置 |
| B3-1 | 有委托行时，非 root 能建 userns 并映射到 X 吗？ | uid 65534 + `nobody:100000:65536`，`unshare --user --setgroups=deny --map-users 0:100000:1 --map-groups 0:100000:1 sh -c id` | `newuidmap: open of uid_map failed: Permission denied`（补 `DAC_OVERRIDE` 后变成 `write to uid_map failed: Operation not permitted`），`rc=1` | **不行**（与 cap 集合无关，见 B5 矩阵） |
| B3-2 | 非委派范围（任意 X）能映射吗？ | `--map-users 0:10001:1`、`0:0:1` | `newuidmap: uid range [0-1) -> [10001-10002) not allowed`；`-> [0-1) not allowed` | 策略层就拒绝"任意 host uid X" |
| B3-3 | 本机唯一能用的 userns 形态是什么？ | uid 65534：`unshare -U -r`（self-map 0→自身 65534） | `uid=0(root)`；`stat` 产物 → `outer owner: 65534:65534` | 可用，但**只能映射 worker 自己的 uid**（即 F18 self-map，无法给每个租户不同 uid） |
| B4-1 | 真实 `sandlock-supervise --uid X` 自检：euid==X | `--user 10001`：`sandlock-supervise --uid 10001 --control-fd 0 --policy /nonexistent.json` | `policy read failed: … No such file or directory`（说明 uid 自检已通过） | 现有形态正确 |
| B4-2 | euid!=X | `--user 10001` 但 `--uid 10002` | `refusing to start: euid 10001 does not match --uid 10002; the launcher must drop privileges before exec …` | 拒绝，符合设计 |
| B4-3 | **userns 槽位（ns 内 0 / 宿主 65534）会怎样** | `unshare -U -r sh -c '<sup> --uid 65534 …'` 与 `<sup> --uid 0 …` | `--uid 65534` → `refusing to start: euid 0 does not match --uid 65534`；`--uid 0` → 通过（随后 `policy read failed`） | **现有自检拒绝 userns 形态**；源码 `third_party/sandlock/crates/sandlock-supervise/src/main.rs:153-161`（`geteuid() != cli.uid`），F18 self-map 见 `src/serve.rs:659-676` |
| B5-1 | 非 root worker + setuid-root broker（标准 rootless 形态）到底行不行？ | 逐 BND 跑 `tmp/f1probe/rootless_slot.py`（worker fork→unshare→stop，root-owned `suidsh` 写 `0 X 1`，子进程 ns 内 `setuid(0)` 后写文件，最后宿主侧 `stat -c %u`） | **deploy-min {SETUID,SETGID,DAC_OVERRIDE,CHOWN}**：broker `printf: I/O error`，无产物 ｜ **Docker 默认 BND**：同样失败 ｜ **+SETFCAP**：失败 ｜ **+SYS_PTRACE**：失败 ｜ **+SYS_ADMIN**：`uid=0(root) … inner sees: 0:0`，`uid_map now: 0 100000 1`，**`OUTER owner: 100000:100000`** ｜ **--cap-add ALL**：同样成功 ｜ 标准工具 `newuidmap` 在所有形态下都失败 | **条件性可行，且唯一解锁项是 CAP_SYS_ADMIN**：宿主侧 uid = X **成立**（`100000:100000`），但需要 SYS_ADMIN + setuid-root broker |
| B5-2 | 对照：root worker 做同样的事 | `--user 0`（默认 cap）跑 `write_child_map.py` | `child uid_map inode owner=0 mode=644`；`setgroups=deny: ok`；`uid_map write: ok -> 0 100000 1`；`gid_map write: ok` | root 路径本来就通（这也解释了 fork 里"只有 root 能 map"的现状） |

**B 判定：**

* 非 root worker **不能** 用 `newuidmap` 走通（在本机，无论 `--cap-drop ALL + 加若干`、Docker 默认 BND、还是 `--cap-add ALL`，`unshare --map-users` / `newuidmap` 都失败）。
* 同一条路线换**自研 setuid-root broker** 后**可以走通**并把宿主侧 uid 变成 X（`OUTER owner: 100000:100000`），但**必须给容器 `CAP_SYS_ADMIN`**（SETFCAP / SYS_PTRACE / SETPCAP 单独加都不行）。
* 即使走通，`sandlock-supervise --uid X` 的现有自检也会**直接拒绝**（B4-3），必须改 fork。
* 本机还观察到一类环境级怪癖（标注为**观察，非结论**）：对 `/proc/self/{setgroups,uid_map}` 的**自写**被拒（连 uid 0 也 `Operation not permitted`），而由"仍在父命名空间的另一个特权进程写子进程的 map"可以成功；`unshare --map-users`（内部走自写/工具路径）因此在这台机器上不可用，而手写 broker 可用。

---

## C. 成本对比与建议

### C1 两条路线各要改什么

| 面 | A：file capabilities | B：userns + 委托 uid |
|---|---|---|
| 镜像 | 加 `libcap2-bin`（构建期 `setcap`，或 entrypoint 以 root 打一次）；放 3 个**私有** helper（setpriv 副本 `cap_setuid,cap_setgid+ep`、chown 副本 `cap_chown+ep`、读/删副本 `cap_dac_override+ep`） | 加 `uidmap` 包（或自研 broker）；`/etc/subuid`、`/etc/subgid` 写委托段；**再放一个 setuid-root broker**（与现在"镜像里不放 setuid 二进制"的姿态冲突） |
| compose | `--cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE`（保持 `seccomp=unconfined`、不加 no-new-privileges） | 需要 `--cap-add SYS_ADMIN`（+ 保持 setuid broker 可执行） |
| k8s | 容器 `securityContext.capabilities.add: [SETUID, SETGID, CHOWN, DAC_OVERRIDE]`（`capabilities.add` 对非 root **不产生 CapEff**，所以这里必须靠 file caps，见 A2-1；`add` 的作用只是把 BND 撑开） | `add: [SYS_ADMIN]` + 保证 setuid 位不被 `no_new_privs`/`allowPrivilegeEscalation:false` 抹掉 |
| envd | 槽位 launch 从 `setpriv …` 换成 `caphelper-setpriv …`（一行 spawner 改动；`RouteBConfig.spawner` 本来就是注入口） | 槽位改走 userns launcher/broker（新增一套启动路径与失败模式） |
| fork | **不需要改**（helper 真的把 euid 变成 X，`--uid X` 自检天然满足） | **必须改** `--uid` 自检（`crates/sandlock-supervise/src/main.rs:153-161`）：现在比的是命名空间视角的 `geteuid()`；userns 槽位是"ns 内 0 / 宿主 X"，必须先放宽或新增判据（例如读 `/proc/self/uid_map` 反推外层 uid，或加 `--uid-host X` / `--userns-mapped` 开关），否则槽位起不来。F18 的 self-map（`serve.rs:659-676`）语义也要重新对齐 |

### C2 安全面

* **A（file caps）**：等价于"容器内任何能 exec 该文件的进程拿到这些 cap"。worker 容器里正常只有我们的进程，但**沙箱进程若可达该路径就能拿到 cap_setuid** ⇒ 威胁模型必须写明：helper 必须放在沙箱不可达的路径（并在 Landlock/chroot 白名单之外），且不带 g+s 目录。收益是能力面极窄（4 条 cap，且各自只授予一个专用二进制）。
* **B（userns + newuidmap/setuid-root broker）**：`newuidmap` 这类 setuid-root 二进制本身就是既有风险面（任何能 exec 它的非 root 进程都能尝试映射），本机实测还**额外需要 SYS_ADMIN**——那是容器逃逸级别的能力，与 A6"把 SYS_ADMIN 从清单里删掉"的方向直接相反。另有两点需要重新验证：`--peer-uid` 走 SO_PEERCRED 时，未映射的 peer uid 会显示为 overflow（65534），恰好与我们约定的 worker uid 相同，可能**偶然通过**；共享卷/NFS 上"宿主侧 uid=X"的落盘语义未测。

### C3 建议路线

**走 A（file capabilities），fork 保持不动；B 作为"宿主提供真正 rootless 能力"之后的备选。**

理由（按证据强度排序）：

1. A 在同一硬件/引擎/镜像上端到端实测通过，且最小 cap 集合就是 4 条窄 cap；B 在非 root 下**任何**"不加 SYS_ADMIN"的形态都失败。
2. B 会与 A6 的既定方向（删 SYS_ADMIN）冲突，且要引入 setuid-root 二进制。
3. B 即使跑通也要改 fork 的 identity 边界（自检），而 A 把"identity by construction"完整保留——`--uid X` 自检继续成立，等于零 fork 改动。
4. `--cap-drop ALL` 在两条路线下都是坑：A 需要按 A2-5 的"BND ⊇ file caps"规则把 4 条 cap 加回 BND；B 需要 SYS_ADMIN。

**推荐落地顺序（不在本次探针范围，仅清单）**

1. 镜像：`deploy/docker/Dockerfile.envd` 装 `libcap2-bin`；构建期（或 entrypoint 首行）对 3 个私有 helper 打 cap；helper 放在沙箱白名单外。
2. envd：`RouteBConfig.spawner` 默认实现改成"用 file-cap helper 起槽位"。
3. 清单：compose/k8s 加回 `SETUID,SETGID,CHOWN,DAC_OVERRIDE` 到 BND（BND 撑开，CapEff 仍为空是预期）；保持 `seccomp=unconfined`；明确禁止 no-new-privileges。
4. 回归：`deploy/scripts/test-prod-shaped.sh` 的 unprivileged phase（uid 65534 + `--cap-drop ALL`）需要按新形态加参数，否则它会证明"file caps 不可用"。
5. 若将来要做 B：**第一步**改 `main.rs:153-161` 的自检（新增"被映射"判据），第二步再谈 setuid broker 与 SYS_ADMIN 的引入。

**若某条无法验证（如实记录）**

| 未验证项 | 原因 | 需要什么环境 |
|---|---|---|
| k8s/containerd 生产集群上的 file caps（`allowPrivilegeEscalation`、`no_new_privs`、PodSecurity） | 本机只有 Docker/OrbStack | 目标 k8s 集群 + 受限 Pod 试跑 |
| 共享卷/NFS 上"宿主侧 uid = X"的落盘与配额归属 | 本机 worker 卷是 docker volume（overlay），无 NFS | NFS 或 XFS prjquota 环境 |
| AppArmor 限制 userns 的失败模式（fork 注释里提到的 `apparmor_restrict_unprivileged_userns`） | 本机内核无 apparmor 模块（`/sys/module/apparmor` 不存在） | Ubuntu 24.04 宿主 |
| rootless dockerd / 宿主级可用 `newuidmap` 环境下的 B 路线 | 本机容器内自写 map 被拒 | rootless dockerd 或普通 Linux 宿主（无此自写限制） |

## 附：产物索引

* 探针脚本与 lab 镜像：`tmp/f1probe/`（`run.sh`、`a1..a3`、`b1..b5`、`Dockerfile.worker-probe`、`setuid_helper.c`、`userns_launcher.c`、`rootless_slot.py`、`write_child_map.py`）
* 原始日志（首行 `ENV-HEADER` / 末行 `EXIT=`）：`tmp/f1probe-A1.log`、`-A2`、`-A3`、`-B1`、`-B2`、`-B3`、`-B4`、`-B5`（同内容另存于 `tmp/f1probe/f1probe-*.log`）
* 仓库代码/清单/镜像：**未改动**（仅新增本报告与 `tmp/` 下探针产物）
