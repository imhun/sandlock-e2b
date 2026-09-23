# chroot 形态下 shebang 脚本不能直接执行：谁拒的、为什么、以及三条出路

**2026-09-23。证据文档 + 一个待拍的决定，不含代码改动。** 它回答 backlog 的 **N35②**
（"chroot（生产）形态下执行 workspace 里自己刚写的文件被拒 EACCES，到底是中介拒还是
Landlock 拒"），顺带把 **N35①**（ETXTBSY 一拍窗口）在同一个形态下问了个明白。

结论先行：**不是中介的 `can_read` 闸门，也不是 workspace 缺少可执行覆盖；拒的是内核自己
解析 `#!` 解释器时的那次查找** —— 中介只把 *被 exec 的那个目标* 换成注入的 fd（这也是
"workspace 里的 ELF 能跑"的原因），内核随后按 `#!` 那一行的**字面路径**去解析解释器
（`/bin/sh`、`/usr/local/bin/python3`…），这一步**没有 seccomp 通知**、中介插不上手，
而且实测它**既不在宿主的"文件存在"语义里、也不在 guest 的路径空间里**（§2.2）。
最可能的落点是 chroot 形态下 Landlock 的规则路径**全部被 `chroot_root` 翻译到镜像
rootfs 之下**，因而宿主拼写的解释器路径没有规则、白名单语义下被拒（实证见 §2.4，
最后一层归属还差一条 trace）。产品级后果：**image-rootfs 形态下任何 shebang 脚本都跑不
起来**（含镜像里的 `pip3`/`npm` 之类 console script、以及用户自建脚本）。

## 1. 实测（`e2b-sandlock-test:latest`，prod-shaped lane，root worker + route B 槽位）

探针：`tmp/k0s/probe_n35_exec_gate.py`（每条腿一个独立 executor，逐条命令带超时）；
lane 入口：`tmp/k0s/n35-lane.sh`；原始日志：`tmp/k0s/n35-chroot{,2,3,4,5,6}.log`、
`tmp/k0s/n35-pure.log`。镜像里的 `python:3.11-slim` rootfs 由 `resolve_test_rootfs` 提供。

| 腿 | chroot（生产）形态 | pure（无 chroot）形态 |
|---|---|---|
| 对照：镜像里的 ELF `/bin/echo` | OK | OK |
| workspace 里的 **shebang 脚本**（同一条命令内写+chmod+exec） | **EACCES，rc 126** | OK（`script-hi`） |
| 同一个脚本，但用 `sh <file>` 解释执行 | OK | OK |
| workspace 里的 **ELF 二进制**（`cp /bin/echo` 后 exec） | **OK** | OK |
| **沙箱启动前就存在**的 workspace 脚本（探针进程预先写、root 拥有） | **EACCES，rc 126** | OK |
| 写在 **镜像 rootfs** 里的 shebang 脚本（`/usr/local/bin/n35_rootfs_tool`） | **EACCES，rc 126** | 不适用 |
| 写 20 个脚本 + 逐个 exec（同一命令内，N35 原探针形状） | 20/20 EACCES（**无** ETXTBSY） | 20/20 OK |
| 写 5 个脚本 + 逐个 exec（同一命令内，缩短版） | 5/5 EACCES | — |
| 20 个脚本拆两条命令：一条只写、另一条只 exec（跨过 events pump 一拍） | 20/20 EACCES | 20/20 OK |
| 写 20 个 **ELF 二进制** + 逐个 exec（同一命令内） | **20/20 OK，无 ETXTBSY** | — |
| 精确 errno（guest 的 python 直接 `os.execv`） | **13 EACCES**（不存在的路径是 2 ENOENT） | — |

两个**判别性**的腿（用来定位"内核在哪个路径空间解析解释器"）：

* `#!/usr/local/bin/python3.14` —— 这个解释器**只存在于宿主 lane 容器**（镜像是 3.11）：
  结果是 **126 EACCES，而不是 127 not found** ⇒ 内核翻的是宿主空间，且**在路径遍历阶段**
  就被拒（所以"文件不存在"与"文件存在"给出同一个 EACCES）。
* `#!/workspace/interp_bin` —— 这个解释器**只存在于沙箱自己的 workspace**（探针刚拷进去的
  ELF）：同样是 **126 EACCES**，不是"运行了那个二进制" ⇒ 再次说明解析没有走 guest 空间。

对照的 pure 形态里，"`#!/usr/local/bin/python3`"跑出来的是 **`py (3, 14)`**（宿主 lane 的
python），并且**成功** —— 同一个"宿主空间解析"，在 rules 用宿主拼写时是放行的。这一对
对比把变量锁死在**规则的路径拼写**上，而不是"脚本 / 位置 / 写读时序"。

## 2. 机制：哪些是实测钉住的，哪些是读码推断的

**实测钉住的（不再有别的解释）**：

1. **拒的是"解释器那一步"，不是脚本自己那一步**：把同一个 shebang 脚本放进**镜像 rootfs**
   （`/usr/local/bin/n35_rootfs_tool`，`ls -l` 在沙箱里可见）也是 126 ⇒ 与脚本位于哪个
   mount、以及它的权限位无关。
2. **解释器不是在 guest 的路径空间里解析的**：两个方向的探针各说一半 ——
   `#!/usr/local/bin/python3.14`（**只存在于宿主** lane 容器）得到 **126 EACCES**，
   而**不是 127 not found**（若是 guest 空间，这个解释器根本不存在，应当 ENOENT）；
   `#!/workspace/interp_bin`（**只存在于沙箱 workspace** 的 ELF）同样是 126，
   而**不是"跑起来那个二进制"**（若是 guest 空间，它应当成功）。⇒ 那次查找既不落在这两个
   guest 可见位置，也不以"宿主文件是否存在"为准。
3. **中介在这条路上没有机会**：`crates/sandlock-core/src/chroot/dispatch.rs::handle_chroot_exec`
   只处理**被 exec 的那个目标**（打开成 fd；ELF 再改写 `PT_INTERP`），随后把调用方内存里的
   路径改写成 `/proc/self/fd/N`。**shebang 是内核在同一个 syscall 内部自己接着做的**，
   没有第二次 seccomp 通知，fork 里也**确实没有** shebang 分支（`read_pt_interp` 只认 ELF）。
   顺带这也解释了"workspace 里的 ELF 能跑"—— 它不是靠路径规则，而是靠这条路的内核侧 fd 注入。

**读码推断的（方向明确，但最后一层的归属还差一步 trace）**：

4. 给定 policy 的**拼写**（实测 dump：`chroot=<rootfs>`、
   `fs_readable=["/usr","/lib","/bin","/opt","/"]`、`fs_writable=["/tmp/tmpXXXX-ws"]`、
   `fs_mount=[...,"/workspace:/tmp/tmpXXXX-ws",...]`），`landlock.rs` 第 4 步在 chroot 形态会把
   **每条路径翻译成 `chroot_root.join(path)`**：`"/"` 变成 rootfs 根（存在，装规则），
   而 `fs_writable` 里的**宿主路径**被拼在 rootfs 之下（不存在 ⇒ `continue`，**不装规则**）。
   ⇒ 规则集只覆盖**镜像 rootfs 子树**；内核去解析 `#!` 时用的宿主路径**一条规则都没有**，
   白名单语义下就是 `EACCES`。
5. **平台侧早就知道这条**：`envd_service/runtime/context.py` 里 MCP gateway 的启动注释写着
   "the sandlock chroot exec handler supports ELF binaries only, so a shebang script cannot
   be exec'd directly in image rootfs mode (EACCES on the script path)"，并因此**显式用解释器
   启动**（`["/usr/local/bin/python3", gateway_bin, ...]`）。今天这份文档补的是**机制**
   （拒在内核的解释器查找，而不是"handler 只支持 ELF"这个更含糊的说法）与**波及面**。

**留给修法作者的最后一步**（不影响 A/B 的取舍，但落 B 之前要看清）：中介的 fd 注入路径
（workspace / memfd 上的 ELF）在"只覆盖 rootfs 的规则集"下**为什么能过**，与第 4 条的推断
并不完全自洽 —— 要么那一层还有别的放行口径，要么子进程的 Landlock 域与这里推断的不同。
一条 `SANLOCK_EVENT_TRACE=1` + 规则集 dump 就能定案（在 `landlock.rs` 第 4 步按
`rule_path` 打印每一条实际装上的规则，跑同一个脚本腿对比）。

## 3. 为什么一直没人撞上（测试面）

* chroot 形态的用例只做**读**和**ELF exec**：`tests/security/test_template_isolation.py`
  读 `template-marker.txt` / `/etc/os-release`、`test_sandlock_isolation.py:223` 探 `/dev/*`，
  没有一条 exec 过 shebang 脚本。
* 唯一"装 CLI 再跑"的用例 `test_user_cli_install_within_workspace_persists` 用的是
  `base_image=None, image_rootfs=None`，也就是 **pure 形态** —— 而 pure 形态恰好是好的
  （§1 表）。
* 平台自己那条真实发生过的路径（MCP gateway 是 shebang 脚本）**已经被绕开**（§2.5），
  所以线上没有症状可看。

## 4. 影响面（为什么值得单独立项）

image-rootfs 形态就是**生产清单**（`E2B_BASE_IMAGE=python-mcp:3.14`）下的形态，而下面这些
都是最常见写法：

* 镜像里的 console script：`pip`、`pip3`、`uv`、`npm`/`yarn` 的 shim、`poetry`……（`pip3`
  在 `python:3.11-slim` 里就是 `#!/usr/local/bin/python3` 的脚本）；
* `pip install --user` / `uv tool install` 之后的**用户级命令**，落在 workspace 里，
  正是被测用例的名字所说的工作流；
* 用户自己的 `run.sh` / 构建脚本（`docker`-less 的自定义入口、`make` 里的脚本 target）。

今天的规避写法只有一种：**显式用解释器调用**（`/usr/local/bin/python3 script.py`、
`sh run.sh`）—— 平台内部能做，**用户代码做不到**（他们不知道要这么写，也不该知道）。

## 5. 三条出路（决定待拍）

| 路线 | 做法 | 代价 / 风险 |
|---|---|---|
| **A. 真根（推荐）** | 落地 N14：mount ns + `pivot_root` 换掉"虚拟根"。之后 `#!` 里的 `/bin/sh` 在沙箱自己的根下解析，而根正是规则覆盖的那棵树 | 是形态级改动：路径中介、`fs_mount`、`/proc/self/root`、N27 的"平台状态不可见"都要一起复核；但**一次收掉一整类**（不只 shebang：内核侧任何路径解析都落到被覆盖的树） |
| **B. 中介补 shebang** | 在 `handle_chroot_exec` 里解析 `#!`：把解释器也从镜像 rootfs 打开并注入 fd，然后把 exec 改写成"解释器 fd + 脚本 fd + 原 argv[1:]"，复刻内核对可选参数的处理 | 局部、不动形态；但要在 Rust 里重实现内核的 shebang 规则（可选参数、参数长度上限、`argv[0]` 形态），且**每多一条内核自己解析的路径就要再补一次**（今天是 `#!`，明天可能是 `binfmt_misc`） |
| **C. 放宽规则** | 把宿主 `/bin`、`/usr/local/bin` 也加进可读/可执行集合 | **不行**：等于把宿主解释器暴露给沙箱，与"宿主文件系统不可达"（`test_image_rootfs_cannot_reach_host_filesystem` 钉住的隔离）直接冲突 |

推荐 A（并把 B 当成"真根落地前的止血选项"记录在案，不默认做）。无论走哪条，**测试要先补**：
在 chroot 形态加一条"写脚本 → 直接 exec"的用例（今天**没有**这条覆盖，见 §3），它现在会红
——如果要先落地，可以先用 `xfail(strict=True)` 把期望写死。

## 6. 顺带把 N35① 问明白了：ETXTBSY 窗口不属于这个形态

N15 中介化实验里看到的 `errno 26`（中介持有写描述符，`sandlock-supervise` 的 events pump
一拍后才放手）在这次测量里**没有**在生产形态复现：

* 同一命令内 `cp /bin/echo` + `chmod` + exec，20/20 成功（`binloop`）；
* 一条命令写、另一条命令 exec（跨过一拍），20/20 成功（`sloop`，chroot 与 pure 都是）；
* 脚本那一路**看不到** ETXTBSY，因为它更早地死在解释器查找上（EACCES）。

所以结论是：**这个窗口属于"被中介的 pure 形态"这条路线（N15 的方向），而不是今天的
image-rootfs 形态**。N15 的收尾人仍要处理"写描述符什么时候能放手"——但那是 N15 自己的
验收条件，不必阻塞在 N35 上。

## 7. 两条未结的线（不算结论，留给下一个人）

1. **两次 30 s 卡死，未复现**：最初两轮（`SANLOCK_EVENT_TRACE=1`、前面还跑过十来条腿）
   里，20 次"写 + 拒绝 exec"的循环卡在 `access(2)` 的通知上（supervisor 一侧空闲），同一段
   命令随后在独立容器里重跑（`loop` 单独、`script,loop`、`loop+binloop`、`sloop`）
   都是几秒完成。因此**不作为结论**记录；若要跟，起点是
   `SANLOCK_EVENT_TRACE=1` 下重复"前面先跑几条腿、再跑 loop"的形状。
2. **`timeout` 包一层就变味**：`timeout 10 /usr/local/bin/pip --version` 报的是
   `timeout: failed to run command '/proc/self/fd/5': No such file or directory` —— 中介把
   路径改写成 `/proc/self/fd/N` 之后，**由上层工具自己再 exec 一次**的形状会撞上这个改写。
   今天只作为观察记录（它是"谁在 exec"的边界，不是 shebang 问题的一部分）。
