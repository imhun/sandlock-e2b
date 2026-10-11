# N95：写路径的 errno 塌缩 —— 缺父目录时答 EACCES，应为 ENOENT

> 登记 2026-10-10｜状态：**已修（fork 侧 + 本地车道已验；舰队未滚动）**｜读数取自本地多节点车道
> （`deploy/compose/docker-compose.multinode.yml`，project `cpuid`，宿主 3110）
>
> 收口读数（2026-10-10）：fork 单测 `test_resolve_in_root_names_the_parent_errno` +
> 集成判据 `test_chroot_create_paths_answer_the_kernels_errno`（RED `ERR 13` → GREEN `ERR 2`）；
> 探针四条腿转 **ENOENT**、`/opt` 仍 EACCES；`byteplan-python-template` + 4096MB 沙箱里
> `npm install -g @larksuite/cli` **rc=0**、`lark-cli version 1.0.97` 跑起来。改动在
> fork 的 `chroot/resolve.rs` 与 `chroot/dispatch.rs`（把 errno 一路带出来），
> 门禁四相位全绿（core_lib 945 / core_integ 574 / oci 157 / supervise_root 4 / mediation_2uid 9）。

## 0. 一句话

沙箱里对"**父目录还不存在**的写路径"（`mkdir` / `symlink` / `readlink`）答的是 **EACCES(13)**，
而标准库与安装器依赖的是 **ENOENT(2)** —— 前者让调用者直接放弃，后者才让它逐层往上把父目录建出来。
于是沙箱里跑不动任何"递归建目录"的标准装法：`npm install -g <pkg>` 是最容易复现的那一个。

## 1. 现象（读数）

探针（`deploy/scripts/acceptance/probe_write_path_errno.py`，`base` 模板、纯 stdlib）：

```
  mkdir 直接子目录（父存在）            ok
  mkdir 两层（父不存在）               errno  EACCES     ← 期望 ENOENT
  mkdir 三层（父不存在）               errno  EACCES     ← 期望 ENOENT
  mkdir 写区外（/opt）                errno  EACCES     ← 期望 EACCES（这条是对的）
  symlink 目标父不存在                errno  EACCES     ← 期望 ENOENT
  readlink 父不存在                   errno  EACCES     ← 期望 ENOENT
  open 读不存在的文件（父存在）          errno  ENOENT     ← 读路径是对的
  open 写（父存在）                    ok
  shell mkdir -p                     ok              ← 逐层调用，每层父都在
```

同一条读数用 python 直接看 errno：

```
mkdir 父不存在      errno=13 (EACCES) Permission denied     ← 应为 2 (ENOENT)
mkdir 父存在        OK
open 读不存在(父在)  errno=2 (ENOENT) No such file or directory
```

### 1.1 端到端：`npm install -g` 死在这一条上

在 `byteplan-python-template`（镜像里 node 20.20.2 / npm 10.8.2 / curl / GNU tar 齐全，
`npmjs` 出口也通）的干净沙箱里：

```
$ npm install -g @larksuite/cli --prefix /home/user/.local
npm error code EACCES
npm error syscall mkdir
npm error Error: EACCES: permission denied, mkdir '/home/user/.local/lib'
```

换更深的 prefix 也一样，下一次撞在
`/home/user/.local/lib/node_modules/@larksuite/cli/node_modules/@clack/prompts`。
**npm 的调用形状是死的**：`@npmcli/arborist` 的 `_createSparseTree` 对每个依赖目录做**一次**
`await mkdir(node.path, { recursive: true })`（`reify.js:624`，同文件 811/1173 也是同一形状）—— node 的 recursive mkdir 在
`ENOENT` 时才回头去建父目录，拿到 `EACCES` 就抛。`bin-links` 那边同理：它用 `readlink` 探 bin 路径，
只认 `ENOENT`/`EINVAL`，其余一律报成假的 `EEXIST: file already exists`
（`bin-links/lib/check-bin.js:25,46`，实测那个文件根本不存在）。

把 errno 语义在 node 侧补回来（recursive mkdir 逐层重试 + symlink 预建父目录 + readlink 的
EACCES→ENOENT）之后，npm 的 sparse tree 与 bin 链接都能过；剩下的失败点全部落在 §7 的两条**形状**
上（`TMPDIR` 指到可写区、postinstall 的 `tar` 补 `--no-same-owner`）。四处补齐后
`npm install -g @larksuite/cli` 是 **`added 7 packages`、rc=0**，`lark-cli version 1.0.97`
跑起来，真请求拿到服务端的 `20048` —— **没有任何一处需要改 npm 或改 CLI**，四处都在沙箱这一侧。

## 2. 根因：errno 被丢两次

两条都在 fork 的 chroot（生产形态）中介层里：

1. **解析层把 errno 扔了** —— `chroot/resolve.rs:206` 的 `resolve_in_root_nofollow` 用
   `openat2_in_root(<parent>, O_PATH|O_DIRECTORY)` 打开**父目录**，失败时
   `resolve.rs:232` 是 `Err(_) => None`：父目录不存在（ENOENT）、父目录不可搜索（EACCES）、
   中间分量是文件（ENOTDIR）、符号链接环（ELOOP）在这一行合成同一个 `None`。
   （`resolve_in_root`（`resolve.rs:184`）在"整条路径不存在"时正是回落到这个函数 —— 注释自己写了
   "resolve the parent and append the missing filename … needed for O_CREAT targets"。）
2. **处理器把 `None` 换成 EACCES** —— `chroot/dispatch.rs:739` 的 `read_and_resolve`：

   ```rust
   let (host_path, virtual_path) =
       resolve_chroot_path(notif, dirfd, &path, ctx).ok_or(NotifAction::Errno(libc::EACCES))?;
   ```

   `mkdirat` 分支（`dispatch.rs:1642`）拿到的就是它。同一文件里
   `read_and_resolve_existing`（`dispatch.rs:773`）给"必须存在"的调用者答的是 **ENOENT**，
   说明这个区分在本文件里本来就有。

顺带记一条对照：`can_write` 的拒绝（`dispatch.rs:1647`）也用 EACCES，**那条是对的** ——
所以本条的修法不能顺手把写区外也改成 ENOENT，`/opt` 那条腿必须保持 EACCES。

## 3. 谁依赖这个区分

* **Node**：`fs.mkdir(p, {recursive: true})`（npm 的 sparse tree、`fs.mkdirSync` 的 recursive 形态）；
* **Go**：`os.MkdirAll`（同一形状：ENOENT 才回退到父目录）；
* **npm 生态**：`cacache` 写 `_cacache/content-v2/sha512/<2hex>/<2hex>`（一次递归 mkdir）、
  `bin-links` 的 `readlink` 探测、任何 postinstall 里的 `mkdir -p` 语义；
* 读路径不受影响（`open` 一条腿实测就是 ENOENT），所以这个 bug 只在"**创建**"这一侧。

## 4. 先例：同一类问题在 exec 路径上已经被 pin 过

`docs/chroot-workspace-exec.md` §(N35/FUP-26 那条) 记的正是这个家族：`init/mod.rs::spawn` 的 exec
分支在 `execvp` 失败后先 `open` trace 文件做记录，而那次 open 的 EACCES **盖掉了** execvp 真正的
ENOENT，于是 127 契约破掉。当时的处理是把它当 bug 修（而不是改契约），并把 errno 矩阵留在
`deploy/scripts/acceptance/probe_127_errno.py`。本文是同一个家族在**写路径**上的第二处：
同一条纪律（精确 errno），不同的处理器。

## 5. 修法

**方案 A（已落地）：把 errno 一路带出来。**

* `resolve_in_root_nofollow` / `resolve_in_root` / `resolve_chroot_path` 从
  `Option<(PathBuf, PathBuf)>` 改成 `Result<(PathBuf, PathBuf), i32>`，`Err(_) => None` 换成
  `Err(errno)`；
* `read_and_resolve` / `read_and_resolve_nofollow` 用拿到的 errno 回 `Errno(errno)`，
  不再 `ok_or(EACCES)`。

实际改动（2026-10-10，fork 工作树）：

* `chroot/resolve.rs`：`resolve_existing_in_root` / `resolve_in_root_nofollow` /
  `resolve_in_root` 三个签名换成 `Result<_, i32>`；`openat2_in_root` 的 errno 直接
  `?` 出来，`read_link(/proc/self/fd/N)` 失败答 EIO、`to_virtual_path` 失败答 EXDEV
  （父目录已经打开，剩下的就是"监督进程没能给打开的东西命名"）；
* `chroot/dispatch.rs`：`resolve_chroot_path{, _nofollow, _existing}` 与 `ChrootCtx::resolve_mount*`
  同样换成 `Result`；`read_and_resolve*` 三个助手 `map_err(NotifAction::Errno)`；
  12 处直接调用点从 `None => Errno(EACCES)` 改成 `Err(e) => Errno(e)`；
  `build_virtual_path` 失败（连名字都给不出）保持 EACCES —— 那条不是文件系统的判决。

行为上"父不存在→ENOENT、不可搜索→EACCES、中间是文件→ENOTDIR、链接环→ELOOP、
逃逸→EXDEV"各自归位；`can_write` 的 EACCES 一个字没动。

**副作用（有意）**：`read_and_resolve_existing` 以前把**任何**失败都答成 ENOENT（读族的口径），
现在答解析器给的 errno。常见情形（叶子不存在）两边都是 ENOENT，差别只出现在"内核拒绝解析"
（逃逸 EXDEV、链接环 ELOOP）——那是以前被编造的 ENOENT 盖住的事实。fork 的四条相位对此全绿。

**方案 B（不动签名）：按"最深存在的祖先"判定。** 解析出最深存在的祖先，父目录缺一层就答 ENOENT，
其余仍答 EACCES。好处是调用点不动；坏处是要多走一次 stat，且要小心别放宽 symlink 逃逸的约束
（`O_NOFOLLOW`/`confine` 那条线不能动）。

两个方案都**不能**把写区外的拒绝（`can_write` 的 EACCES）改掉。

## 6. 验收判据（RED → GREEN）

**读数（2026-10-10，全部实跑）**

1. **fork 单测**：`chroot::resolve::tests::test_resolve_in_root_names_the_parent_errno`
   —— 29 passed（`cargo test -p sandlock-core --lib chroot::resolve`）。
2. **fork 集成判据**（新增 `test_chroot::test_chroot_create_paths_answer_the_kernels_errno`，
   用 helper 的 `legacy-*` 动词，它们打印**数值** errno）：
   * RED（把两处源码改动 stash 掉，只留判据）：`mkdir under a missing parent` 实得
     **`ERR 13`**，判据要 `ERR 2` —— 一行读数复现 N95；
   * GREEN（补丁回来）：`OK` / `ERR 2` / `ERR 2` / `ERR 2` / `ERR 13`（只读区那条仍 EACCES）全中。
   ```
   deploy/scripts/fork-gate.sh --one 'test_chroot::'      # 51 passed
   ```
3. **fork 四条相位**（数字已按 `third_party/sandlock/docs/test-baseline.md` 登记 +1）：
   默认相位 `core_lib 945 / core_integ 574 / ffi 104 / cli 100 / supervise 57 /
   supervise_cost 3 / cli_build 0 / python 465`；`--oci-root 157`、`--supervise-root 4`、
   `--mediation-2uid 9`。
   * 本机注意：root 相位不能走 `fork-gate.sh`（它固定降权到 65534），要按 fork 文档的正规形态
     `docker run --privileged --entrypoint bash … 'sh scripts/test-all.sh --oci-root'`；
     `--mediation-2uid` 还必须用带 `setcap` 的 `sandlock-dev:latest`（`sandlock-dev-f17`
     缺 `setcap`，会红在 `run setcap: Os { code: 2, kind: NotFound }`）。
4. **本地多节点车道的判据探针**：四条腿从 EACCES 变 **ENOENT**，`/opt` 那条**仍是 EACCES**
   （策略没放宽）：
   ```
   python3 -u deploy/scripts/acceptance/probe_write_path_errno.py [--json]
   ```
   （两个 URL 默认就指本地车道，可用 `E2B_API_URL` / `E2B_SANDBOX_URL` 覆盖；
   **两个都要给** —— 只给 `E2B_API_URL` 时命令通道会退回 create 应答里的 `domain: localhost`，
   每条命令都以 `tls handshake eof` 结束。）
   车道要用**重建后的镜像**：`build-sandlock-wheels.sh` → `build-images.sh
   (REGISTRY=local/e2b VERSION=cpuid PLATFORMS=linux/amd64)` →
   `compose … up -d --force-recreate worker-{1,2,3} image-cache-init`；重启容器会丢
   节点本地的模板解包缓存，得先用 `deploy/scripts/warm_base_image.py` 预热。
5. **端到端**：`byteplan-python-template` + 4096MB 的沙箱里，**没有任何 node 侧 shim**，
   ```
   TMPDIR=/home/user/tmp TAR_OPTIONS=--no-same-owner \
     npm install -g @larksuite/cli --prefix /home/user/.local
   ```
   **rc=0**（`added 7 packages`），`/home/user/.local/bin/lark-cli --version` → `lark-cli version 1.0.97`；
   带一份假 config 的 `lark-cli api` 能走到服务端（回 `20048`）。
   三个环境旋钮都是**普通配置**、不是 errno 绕行：`TMPDIR`（`/tmp` 只读）、
   `--prefix`（镜像的 `/usr/local/lib` 只读）、`TAR_OPTIONS`（§7a 的 chown）。

## 7. 相邻但**不是**本条内容的四件事（同一轮量到，单独记着）

| # | 现象 | 性质 | 绕行 |
|---|---|---|---|
| a | `tar -xzf` 以 root 解包时按包内 uid chown → `EINVAL`，tar 退 2（CLI 的 postinstall 就死在这） | **设计使然**（per-sandbox uid 隔离下不能 chown 到任意 uid），不是本条 | `TAR_OPTIONS=--no-same-owner`（GNU tar 会把 `TAR_OPTIONS` 里的词前置到每次调用，所以它是**环境变量**、不用换 tar 也不受 b 的 PATH 改写影响；实测 `npm install -g` rc=0） |
| b | 平台**强制改写 PATH**（调用方传的 PATH 被换成镜像默认），而 PATH 里的目录全只读 ⇒ 换不掉 `tar`/`curl` 这类工具 | 形状 | 只能从进程内（node 侧）补 |
| c | `/tmp` 只读；npm 的 global prefix（`/usr/local/lib`）只读 | 形状 | `TMPDIR`、`--prefix` 指到 `/home/user` |
| d | 沙箱 `memoryMB=1024`（出厂默认）时，`node` 一执行 JS、Go 二进制一启动就被 **SIGKILL** | 另一条独立问题：`max_memory` 按 **mmap 请求量**记账（`PROT_NONE\|MAP_NORESERVE` 的纯预留也全额计入，`crates/sandlock-core/src/resource.rs:862-870`） | 要 `memoryMB ≥ 2048`；是否把"预约型 mmap"排除出账本，值得单独立一条 |

d 是这四条里唯一可能值得单独登记的：它决定了"沙箱里能不能跑 node/go 程序"，
而当前出厂默认（`E2B_DEFAULT_MEMORY_MB=1024`，`E2B_MAX_SANDBOX_MEMORY_MB=2048`）下
**node/go 负载一律被杀**。

## 8. 出处

* 探针：`deploy/scripts/acceptance/probe_write_path_errno.py`（本轮入库；索引在
  `deploy/scripts/acceptance/README.md`）
* 改动前代码：`third_party/sandlock/crates/sandlock-core/src/chroot/resolve.rs`（`Err(_) => None`）、
  `chroot/dispatch.rs`（`ok_or(NotifAction::Errno(libc::EACCES))`）
* fork 判据：`crates/sandlock-core/src/chroot/resolve.rs`（单测）、
  `crates/sandlock-core/tests/integration/test_chroot.rs::
  test_chroot_create_paths_answer_the_kernels_errno`（集成）；
  基线登记在 `third_party/sandlock/docs/test-baseline.md`（core_lib 944→945、core_integ 573→574）
* 先例：`docs/chroot-workspace-exec.md`（exec 路径的 errno 覆盖）、
  `deploy/scripts/acceptance/probe_127_errno.py`
* npm 侧那四条诊断补丁（recursive mkdir 逐层重试、symlink 预建父目录、readlink 的
  EACCES→ENOENT、`execFileSync('tar')` 补 `--no-same-owner`）**只在定位阶段用过、没有入库**；
  修完之后同一条安装命令不再需要其中任何一条（`TAR_OPTIONS` 是环境变量、不是补丁）

## 9. 上线（未做）

**舰队没有滚动**：本轮只到"fork 已修 + 本地多节点车道已验"。上线的形状与 cp-uid 那次同一纪律
（`AGENTS.md`：线上只做本地证明不了的那些形状事实，且带回退杆）：重建两架构 wheel →
`build-images.sh` 推 `e2b-sandlock-{worker,agent,quota-agent}` → 滚 k0s →
跑 §6 的探针与一次 `npm install -g`。要人拍板才动，不是"改完自动上线"。

## 10. 上线读数（2026-10-11：**已滚**）

**版本**：`0.1.0-1185-g39d3460-20261011-065341`（写入 `deploy/stack/.version`；四个工作负载的镜像 tag
一起从 `1183` 推到 `1185`：`control-plane` / `e2b-worker` / `e2b-c3-agent` / `seccomp-installer`）。
走的是既有闸门：先 agent DaemonSet、后 worker StatefulSet，再每节点预热基础镜像。滚动前
`Sandbox.list()` = 0；两个 worker 的 base image `peek cached=true`（没白拉）。

**本轮只带 N95**：fork 工作区当时还叠着另一处未提交改动（会话 DNS 网关常开）。按"那处等本机全量
车道绿了再上"的顺序，构建前把它从工作区取了下来（留档 `tmp/dns-gateway-always-on.patch`），
所以 `1185` 只含 N95。构建时的证据：staged 上下文里 `resolve.rs` 是 `Result<_, i32>` 签名、
`sandbox.rs` 不含那处常量（staged 内容 = 工作区，docker 的编译层缓存命中 ⇒ 该内容此前正是这么编的）；
产出的 `.so` 与 18:58 那份（含两处改动的构建）哈希不同。

**判据 1 —— 探针**（线上入口 `http://172.18.78.49:3000`，`base` 模板）：

```
  mkdir 直接子目录（父存在）   ok
  mkdir 两层（父不存在）       errno ENOENT   ← 修前 EACCES
  mkdir 三层（父不存在）       errno ENOENT   ← 修前 EACCES
  mkdir 写区外（/opt）         errno EACCES   ← 策略没放宽（正确）
  symlink 目标父不存在         errno ENOENT
  readlink 父不存在            errno ENOENT
  open 读不存在的文件（父存在） errno ENOENT
  open 写（父存在）            ok
VERDICT collapsed-errno legs: []
```

**判据 2 —— 端到端**（`byteplan-python-template`，`memoryMB=4096`，三个环境旋钮照 §6.5）：

```
[npm]      rc=0   npm install -g @larksuite/cli --prefix /home/user/.local
[lark-cli] rc=0   lark-cli version 1.0.97
```

⚠️ 舰队上这条命令还需要一条网络规则：`network.allowOut` 放 `registry.npmjs.org:443` 与
`*.npmmirror.com:443`。原因**不是** N95，而是包的 postinstall 要从 `registry.npmmirror.com` 下二进制 ——
那个域名不在平台默认钉进 `/etc/hosts` 的七个源里，而没有通配域规则时沙箱没有可用 DNS
（`curl: (6) Could not resolve host`）。给一条通配规则会顺带启用每沙箱 DNS 网关，问题消失；
compose 车道上同一条命令不需要它，正是因为那条车道的沙箱本来就带可用 DNS。

**回退杆**：把 `deploy/stack/.version` 写回 `0.1.0-1183-gab98313-20261009-175937` 再跑
`deploy/k8s-k0s/apply.sh`（镜像都还在 ACR，回退是又一次滚动，不是重建）。`1183` → `1185` 之间
只有 N95 这一处 fork 改动。
