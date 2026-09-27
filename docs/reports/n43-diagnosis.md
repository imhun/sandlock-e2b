# N43 定因报告：真根形态下 `dirfd` 相对路径被当成"虚拟路径"

状态：**已定因，未修**。所有"实测"都标了出处；只有读代码得出的结论标了 `[读代码]`。
工作目录 `/Users/polus/project/ai/sandlock-e2b`，fork 子模块 `third_party/sandlock`（HEAD `6ff2505`）。

---

## 0. 一句话结论

`chroot/dispatch.rs` 里带 `dirfd` 的路径翻译，先 `readlink("/proc/<pid>/fd/<n>")` 拿基路径，
再交给 `ChrootCtx::reported_to_virtual()`；后者有一条"**子进程已 pivot 进真根 ⇒ 内核报的路径
已经是虚拟路径**"的捷径（`chroot/dispatch.rs:361-373`）。这条捷径对 `/proc/<pid>/cwd` 成立，对
**沙箱里的 fd 不成立**：沙箱的 fd 几乎全是中介在**宿主**上打开后 `SECCOMP_IOCTL_NOTIF_ADDFD`
投递进去的（`chroot/dispatch.rs:922/946-972`），它们的 `file->f_path.mnt` 挂在**中介的宿主
mount** 上，内核于是把 `/proc/<pid>/fd/N` 渲染成**宿主路径**。捷径原样放行这个宿主路径，后面
`resolve_in_root*` 把它当**虚拟路径**去 rootfs 里找 ⇒ 找不到 ⇒ `EACCES`（nofollow/open 一族）
或 `ENOENT`（must-exist 一族）。

一句话：**N35/真根把"内核替我解析"变成"内核真解析"，而中介的 dirfd 翻译还停在"内核报的就是
我的虚拟拼写"的假设上。**

复现：稳定（线上 r1/r2 逐字一致，除 sandbox id 与时间戳；本机同形复现）。
形状依赖：**只有 image-rootfs + `E2B_REAL_ROOT=1` 挂**；`E2B_REAL_ROOT=0` 与 pure 都 OK（实测）。

---

## 1. 复现

### 1.1 线上（首选判据，`0.1.0-535`，image-rootfs + 真根）

脚本 `tmp/n43/n43-repro.py`（命令组照抄缺陷登记，只加建 `seed.bin`(3,000,000 B) 与
`workspace/inner.bin` 的 setup）：

```bash
E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000 \
E2B_API_KEY=$(cat tmp/k0s/.apikey) \
tmp/z-venv/bin/python tmp/n43/n43-repro.py
```

原始输出（`tmp/n43/repro-cluster-r1.log`，第 1 次；第 2 次 `repro-cluster-r2.log` 与它逐字相同）：

```
sandbox sbx_7ef6aa66ff4251f7   (r2: sbx_d4e325b4ecd05340)

$ shape
    uid=0(root) gid=0(root) groups=0(root)

    total 2933
    drwxrwx--- 3  10000 nogroup    4096 Sep 26 06:41 .
    drwxr-xr-x 3 nobody nogroup      18 Sep 20 07:57 ..
    -rw-r--r-- 1  10000   10000 3000000 Sep 26 06:41 seed.bin
    drwxrwx--- 2  10000 nogroup    4096 Sep 26 06:41 workspace

    /home/user drwxrwx--- 10000 65534 4096
    /home/user/workspace drwxrwx--- 10000 65534 4096
  exit_code=0

$ direct: cat
    rc=0

$ direct: du -B1 --apparent-size (file)
    3000000	/home/user/seed.bin
    rc=0

$ A: find /home/user -type f
    /home/user/seed.bin
    rc=1
  stderr:
    find: '/home/user/workspace': Permission denied

$ B: tar -cf /dev/null /home/user
    rc=2
  stderr:
    tar: Removing leading `/' from member names
    tar: /home/user/seed.bin: Cannot stat: Permission denied
    tar: /home/user/workspace: Cannot stat: Permission denied
    tar: Exiting with failure status due to previous errors

$ C: du -s -B1 --apparent-size /home/user
    0	/home/user
    rc=1
  stderr:
    du: cannot access '/home/user/seed.bin': Permission denied
    du: cannot access '/home/user/workspace': Permission denied

$ D: os.stat(dir_fd=open('/home/user'))
    os.stat(seed.bin, dir_fd=/home/user) = FileNotFoundError: [Errno 2] No such file or directory: 'seed.bin'
    os.open(dirfd) readlink = /var/lib/e2b-sandboxes/sbx_007e7838cdf1a084
    os.listdir(dir_fd) = ['seed.bin', 'workspace']
```

注意最后两行是**同一个 fd**：`getdents64` 正常（不需要路径），而 `/proc/self/fd/N` 的
readlink 答案是**宿主路径**（`/var/lib/e2b-sandboxes/<id>`）。这一行就是定因的钥匙。

### 1.2 每个 dirfd 变体的 errno（线上，同一沙箱形态）

`tmp/n43/n43-errno-probe.py`（原始输出 `tmp/n43/errno-probe-cluster.log`）：

```
sandbox sbx_c4a7b80326dcca58
dirfd on /home/user      = 4
  readlink(/proc/self/fd/4) = /var/lib/e2b-sandboxes/sbx_c4a7b80326dcca58
  readlink(/proc/self/cwd)   = /home/user
  readlink(/proc/self/root)  = /
  stat(dir_fd, follow)      newfstatat             -> ENOENT (No such file or directory)
  stat(dir_fd, nofollow)    newfstatat|NOFOLLOW    -> EACCES (Permission denied)
  lstat(dir_fd)             newfstatat|NOFOLLOW    -> EACCES
  access(dir_fd, follow)    faccessat2             -> OK False
  access(dir_fd, nofollow)  faccessat2|NOFOLLOW    -> OK False
  readlink(dir_fd)          readlinkat             -> EACCES
  open(dir_fd)              openat                 -> EACCES
  open(dir_fd, subdir)      openat|O_DIRECTORY     -> EACCES
  listdir(dir_fd)           getdents64             -> OK ['seed.bin', 'workspace']
  stat(absolute)            newfstatat             -> OK st_mode=33188 …
  open(absolute)            openat                 -> OK 5
```

（`os.access` 在 CPython 里把 ENOENT/EACCES 都折算成 `False` 而不抛，所以那两行只能说明
"解析失败"，不区分 errno —— 不是权限位问题。）

### 1.3 内核语义：`/proc/<pid>/fd/N` 的拼写由 **fd 自己的 vfsmount** 决定（本机实测）

最小探针 `tmp/n43/readlink-semantics.sh` + `child-body.sh`（一个容器内：子进程 `unshare -m`
+ bind workspace 到 `/home/user` + `pivot_root`；父进程是"中介"：同一 pid ns、宿主 root、
未 pivot）。原始输出：

```
child: umount2(oldroot) = 0
child: id         = 0
child: root       = <[Errno 2] ...: '/proc/self/root'>       # 真根里没有 /proc（中介合成的）
child: ls /home/user = ['seed.bin']
--- outside reader (same pid ns, host root, NOT pivoted) ---
outside: root   = '/'
outside: cwd    = '/'
outside: fd3 (child-opened)         = '/home/user'     <-- 子进程自己在 pivot 后打开的 fd
outside: fd9 (supervisor-inherited) = '/arena/ws'      <-- 中介在宿主上打开、投递给子进程的 fd
```

同一个已 pivot 的子进程、同一个读取者，两个 fd 的拼写完全不同 ⇒ 决定拼写的是 **fd 挂在哪棵
mount 树**，不是"子进程是否 pivot"，也不是"谁在读"。沙箱里的 fd 全是后者（中介开的宿主 fd）。

### 1.4 本机同形复现（E2B 全栈，用仓库自己的 harness）

`tmp/n43/eb-shapes.py` 用 `tests/security/conftest.py::route_b_sandbox`（"worker 怎么建沙箱，
它就怎么建"）在 `e2b-sandlock-test:latest` 里跑同一命令组，rootfs 用
`resolve_test_rootfs("python:3.11-slim")` 真镜像、cwd 走 `_view_cwd`（与 worker 一致：

```bash
docker run --rm --privileged -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
  -v "$PWD":/workspace -w /workspace e2b-sandlock-test:latest \
  python3 -u tmp/n43/eb-shapes.py {image-real|image-emulated|pure}
```

`image-real`（`tmp/n43/eb-image-real.log`）与线上同签名：

```
$ A: find /home/user -type f
    /home/user/seed.bin
    rc=1
    find: '/home/user/workspace': Permission denied
$ C: du -s -B1 --apparent-size /home/user
    0	/home/user
    rc=1
    du: cannot access '/home/user/workspace': Permission denied
    du: cannot access '/home/user/seed.bin': Permission denied
$ D: os.stat('seed.bin', dir_fd=open('/home/user'))
       readlink(fd) = /tmp/tmpc3bgkbsc-ws
    FileNotFoundError: [Errno 2] No such file or directory: 'seed.bin'
```

（本地 `tar -cf /dev/null` 在**三种形状**里都先死在 `/dev/null: Cannot open: Permission denied`
—— 本 harness 的 policy 与线上不同（线上 tar 打开 `/dev/null` 正常，只抱怨 `seed.bin`）。与
N43 无关，见 §6 第 3 条。）

---

## 2. 定因

### 2.1 dirfd 是怎么被翻译的

`crates/sandlock-core/src/chroot/dispatch.rs`：

| 行 | 代码 | 作用 |
|---|---|---|
| 596-615 | `fn build_virtual_path(notif, dirfd, path, ctx)` | 把 `(dirfd, path)` 合成一个**虚拟**路径串 |
| 602-604 | `if Path::new(path).is_absolute() { path.to_string() }` | 直路径直接用调用者给的拼写（所以直路径全对） |
| 606-607 | `if dirfd32 == AT_FDCWD { virtual_cwd_of(...) }` | 相对 cwd：用中介自己跟踪的**虚拟** cwd |
| **609** | `let base_host = read_link("/proc/{pid}/fd/{dirfd}").ok()?` | 其余 dirfd：**问内核要基路径** |
| **610** | `ctx.reported_to_virtual(notif.pid, &base_host)?` | 把内核给的拼写转成虚拟拼写 |
| 612-613 | `base_virtual.join(path)` | 拼接后交给 `resolve_chroot_path*` |

`reported_to_virtual`（`chroot/dispatch.rs:361-373`）：

```rust
/// A pivoted child's reports are already virtual -- they are relative to
/// the rootfs -- so they must not be put through the host-to-virtual
/// mapping, which would look them up under the rootfs and find nothing.
fn reported_to_virtual(&self, pid: u32, path: &Path) -> Option<PathBuf> {
    if self.child_is_pivoted(pid) {        // :341 按 inode 比较 /proc/<pid>/root 与 ctx.root
        return Some(path.to_path_buf());   // 原样返回
    }
    self.host_to_virtual(path)
}
```

### 2.2 为什么直路径能过、dirfd 相对不能

* **直路径**：`build_virtual_path` 走 602-604，拼写就是子进程给的 `/home/user/seed.bin`，
  后面 `resolve_in_root`（`chroot/resolve.rs:184/245`）用 `openat2(RESOLVE_IN_ROOT)` 在 rootfs
  里解析 ⇒ 正常。
* **dirfd 相对**：走 609-610。沙箱的 fd 是**中介开的宿主 fd**（打开点
  `chroot/dispatch.rs:800` 的 `resolve_chroot_path` + `:922` 的 `inject_watched` →
  `NotifAction::InjectFdSend`，即 `SECCOMP_IOCTL_NOTIF_ADDFD`），它的 mount 属于中介的宿主
  mount 树 ⇒ 内核把 `/proc/<pid>/fd/N` 渲染成 `/var/lib/e2b-sandboxes/<id>`（§1.3 实测的
  `fd9` 那一行；线上 §1.1/§1.2 也直接读到这个宿主路径）。`child_is_pivoted` 为真 ⇒ 610 原样
  放行 ⇒ 合成出 `/var/lib/e2b-sandboxes/<id>/seed.bin`，**一个不是虚拟路径的串被当虚拟路径**：
  * `resolve_existing_in_root`：rootfs 里没有 `var/lib/e2b-sandboxes/<id>` ⇒ `None`（`resolve.rs:259`）；
  * `resolve_in_root_nofollow`：同样是父目录 `openat2_in_root` 失败 ⇒ `None`（`resolve.rs:232`）；
  * `resolve_in_root`（= existing 失败后 nofollow，`resolve.rs:184-193`）⇒ `None`。

  对比：`E2B_REAL_ROOT=0` 时 `child_is_pivoted` 为假 ⇒ 走 `host_to_virtual`：mount 表里
  `/home/user → /var/lib/e2b-sandboxes/<id>`（`envd_service/executors/sandlock.py:2116-2118`）
  把它映射回 `/home/user` ⇒ 一切正常。**这就是同一条 fd、同一段代码、只差一个 flag 的分叉。**

### 2.3 两个 errno 各从哪来（`chroot/dispatch.rs`）

| 调用变体 | 解析函数 | 失败时的 errno | 行 |
|---|---|---|---|
| `newfstatat` 无 `AT_SYMLINK_NOFOLLOW`（`os.stat(dir_fd=…)`） | `read_and_resolve_existing` → `resolve_chroot_path_existing` | **ENOENT** | `:741-754`（`:753`） |
| `newfstatat|AT_SYMLINK_NOFOLLOW`（`os.stat(follow_symlinks=False)`/`lstat`；`find`/`du`/`tar` 用这条） | `read_and_resolve_nofollow` → `resolve_chroot_path_nofollow` | **EACCES** | `:724-739`（`:735`） |
| `openat(dirfd, …)`（`tar` 取文件内容、`find` 下降子目录） | `handle_chroot_open` → `resolve_chroot_path` | **EACCES** | `:800-803` |
| `readlinkat(dirfd, …)` | `resolve_chroot_path_nofollow` | **EACCES** | `:2478-2481` |
| `faccessat2` | 同 stat 族，`real_path.exists()` 为假 ⇒ ENOENT | ENOENT（CPython 折算成 `False`） | `:2296-2301` |
| `getdents64(fd)` | 不做路径解析（fd 本身可用） | — | 所以 `find` 能列出 `seed.bin` |

`find` 只抱怨子目录、`du`/`tar` 抱怨文件，是这个表的自然结果：fts 用
`fstatat(AT_FDCWD, name)`（相对 cwd，走 606-607 的虚拟 cwd，**能过**）+ `openat(parent_fd, name,
O_DIRECTORY)` 下降（**撞 EACCES**），`du`/`tar` 则对每个条目用父 dirfd 的 `fstatat`/`openat`
（**撞 EACCES**）。errno 的归属是 §1.2 实测的；"哪个工具用哪条变体"是按标准库行为解释的，未
逐条 strace。

### 2.4 引入点与家族归属

* `git log -S reported_to_virtual` ⇒ **`86630ea`（feat(realroot): build a real root for the
  image-rootfs shape，2026-09-23）** 引入这条捷径（`4afd806` 又给 getcwd 扩了一次）。即 **N43
  是真根工作（N35/N14）带来的回归**，2026-09-25 `E2B_REAL_ROOT=1` 上生产后才可见；此前默认
  `E2B_REAL_ROOT=0`，走到的是 `host_to_virtual` 分支，所以历史无人撞到。
* **不是 `PURE_UNGATED` 家族**：那一族是"根本没被中介拦"的 syscall（`sys/path_surface.rs:364`）。
  这里的 `newfstatat`/`statx`/`readlinkat`/`faccessat`/`openat` 全都在
  `MEDIATED_PATH_SYSCALLS`（`path_surface.rs:67-116`）里，**拦是拦了，翻译错了**。
* **不是 `Open` 桶**：Open 桶是"未中介、无门禁、待决"的 12 条；这里是已中介族群内的回归。
* **同族还有一处**：`crates/sandlock-core/src/cow/dispatch.rs:107-137`
  （`resolve_at_path_with_virtual`）同样只 `readlink(/proc/<pid>/fd/N)` 后**直接 join**，连
  host→virtual 映射都没有。E2B 的 worker 不走 COW（`executors/sandlock.py` 的 `on_exit` 是
  子进程退出回调，不是 fork 的 COW 分支），所以它不影响 N43 的线上表现，但修的时候要么一起修，
  要么写清楚它为什么不修。
* **文档里需要订正的一句**：`chroot/dispatch.rs:361-367` 的注释（"A pivoted child's reports
  are already virtual"）是错的断言，按代码/实测应改为"**cwd 一族**是虚拟的，**fd 一族**可能是
  中介的宿主路径"。E2B 侧 `docs/chroot-workspace-exec.md` §7/§9 的实测表只覆盖了 exec 与
  shebang，没有覆盖 fd 相对解析，不构成矛盾，但会被读成"真根下路径解析整类都没问题"。

---

## 3. 形状依赖（各一句，都有实测）

| 形状 | 结论 | 证据 |
|---|---|---|
| image-rootfs + `E2B_REAL_ROOT=1` | **挂**（本缺陷） | 线上 §1.1/§1.2 两次 + 本机 §1.4 `eb-image-real.log` |
| image-rootfs + `E2B_REAL_ROOT=0` | **不挂**，四件套全 OK | 本机实测 `eb-image-emulated.log`：`find` 列出两个文件 rc=0；`du -s` = `3000100 /home/user` rc=0；`readlink(fd) = /home/user`；`os.stat = 3000000`；且 `test_real_root_mounts.py` 在同一容器（`E2B_REAL_ROOT=0`）里用真镜像装 3 个沙箱 `1 passed`，说明这条 harness 里模拟根形态本身是好的 |
| pure（无 base image） | **不挂**，四件套全 OK | 本机实测 `eb-pure.log`：`du -s` = `3000100 /home/user` rc=0、`os.stat = 3000000`；判据（`[读代码]`）是 pure 没有 root 可 pivot（`context.rs:893-895` "real_root requires a chroot root"），`child_is_pivoted` 恒假 ⇒ 走 `host_to_virtual`（N15 之后 pure 的 `chroot_root="/"`，identity 翻译） |

形态判据统一是 `child_is_pivoted(pid)`（`chroot/dispatch.rs:341-359`，按 inode 比较
`/proc/<pid>/root` 与 `ctx.root`）—— 这是**唯一**让那条捷径生效的开关，所以"真根挂、另外两种
形状不挂"不是巧合，是同一个分支。上面两种非真根形状是**跑出来的**，判据是**读代码**得到的。

---

## 4. 修好后应全 OK 的命令组（判据）

就用缺陷登记那四条（外加两条直路径对照，防止"把整条链改成恒错"也算通过）：

```bash
# 沙箱内，/home/user 下有 3000000 B 的 seed.bin（以及一个子目录）
id                                                        # uid=0(root)
cat /home/user/seed.bin > /dev/null; echo rc=$?             # rc=0
du -B1 --apparent-size /home/user/seed.bin                  # 3000000  /home/user/seed.bin
find /home/user -type f; echo rc=$?                        # 列出 seed.bin（+ 子目录里的文件），rc=0，无 Permission denied
tar -cf /dev/null /home/user; echo rc=$?                   # rc=0，无 Cannot stat
du -s -B1 --apparent-size /home/user; echo rc=$?           # 等于 seed.bin + 子目录内容之和，rc=0
python3 -c "import os
fd = os.open('/home/user', os.O_RDONLY|os.O_DIRECTORY)
print(os.stat('seed.bin', dir_fd=fd).st_size)"             # 3000000，不再 FileNotFoundError
```

线上跑法：`tmp/n43/n43-repro.py`（r1/r2 已附）。判据要点：`find`/`du`/`tar` 的 rc 必须是 0，
且 stderr 为空 —— 只把 errno 从 ENOENT 换成 EACCES（或反之）不算修好。

---

## 5. 修法建议（落点 / 代价 / 风险）

**主方案 A（最小，1 行）**：`crates/sandlock-core/src/chroot/dispatch.rs:606-611`，dirfd 分支里
先试反向映射，映射不到才当"已经是虚拟路径"：

```rust
let base_host = std::fs::read_link(format!("/proc/{}/fd/{}", notif.pid, dirfd)).ok()?;
// Two spellings come back for a pivoted child: the sandbox's own ("/home/user",
// for a descriptor on a mount inside its namespace) and the mediator's host path
// ("/var/lib/e2b-sandboxes/<id>", for the descriptors the mediator opened and
// injected -- which is nearly all of them). Map when it maps; only then treat it
// as already virtual.
let base_virtual = ctx
    .host_to_virtual(&base_host)
    .or_else(|| ctx.reported_to_virtual(notif.pid, &base_host))?;
```

* 代价：一次 mount-table 前缀比较（`resolve.rs::host_to_virtual`，已有函数，无新 syscall）；
  `child_is_pivoted` 的 `stat("/proc/<pid>/root")` 仍只在 fallback 里发生（真根下 fd 基路径几乎
  总是能映射，热路径反而少一次 stat）。
* 风险：**字符串启发式**。若镜像里恰好存在 `<宿主 workspace 路径>` 这个目录（例如镜像内含
  `/var/lib/e2b-sandboxes/<自己的 id>`），`host_to_virtual` 会把**虚拟**拼写误映射成另一条路径。
  这是"两条路都是猜测"的固有问题；A 案把它从"对 fd 一族永远错"改成"对 cwd 一族不受影响、
  对 fd 一族基本对"。要彻底消除就得走方案 C。

**主方案 B（同样小，改在语义函数里）**：把 `reported_to_virtual`（:368-373）改成
"能映射就映射，否则回落到原样"，理由与 A 相同，只是覆盖面更大（`/proc/<pid>/cwd`、`/exe`、
fd 三处共用）。风险比 A 大一点点：`/proc/<pid>/cwd` 的虚拟拼写若恰好落在 `chroot_root` 前缀下
会走错分支（同样需要方案 C 才能根治）。**A 和 B 选一个，不要两个都改**。

**方案 C（根治，代价最高）**：dirfd 相对解析改成"按 fd 解析"——用 `pidfd_getfd` 把子进程的
dirfd 复制到中介（仓库里已有同类原语：`procfs::dup_fd_from_pid` / `cow::dispatch` 的
`dup_fd_from_pid` 路径），然后 `openat(dirfd_copy, name, O_PATH)`，让**内核**沿 fd 自己的
mount 解析，再把结果 host→virtual。代价：每次 dirfd 相对调用多一次 `pidfd_getfd` + `openat2`
（还要处理 `O_PATH` 权限、被 ptrace 限制的进程、fd 竞态 `EBADF`）；风险：性能面（`du`/`tar`
是逐条 fstatat 的调用密度最高的路径）与新增失败模式。**建议：A（或 B）先修，C 作为 N14
"退役模拟"那一步的顺带收益**（真根下本来就可以让内核自己解析更多路径面）。顺带把
`cow/dispatch.rs::resolve_at_path_with_virtual` 的同族写法一起对齐，否则 CLI 的 COW 形状仍然是
坏的（`[读代码]`，未实测）。

**必须补的钉子**（当前 tip 覆盖不到这一点，值得点名）：

* fork 侧：`crates/sandlock-core/tests/integration/test_chroot.rs` 里加一条"真根 + 子目录 fd +
  `fstatat`/`openat`/`readlinkat`"的用例（现有 `test_chroot_proc_dirfd_relative_is_virtualized`
  只覆盖 `/proc` 合成；`test_determinism.rs:232`、`test_netlink_virt.rs:381` 的 dirfd 用例是
  **非 chroot** 的 hostname/hosts shim，抓不到这条）；并让它同时在
  `real_root(false)`/`real_root(true)` 下跑（两种形态一份断言）。
* E2B 侧：`tmp/n43/n43-repro.py` 这种"四条命令 rc 全 0"的生产形态探针，挂进
  `deploy/scripts/prod_shape_lane` 那条 lane（N35 的 exec 探针就是这么钉的）。

---

## 6. 不确定的部分

1. **没读线上 worker 的 env**：`deploy/scripts/open-cluster-tunnel.sh --check` 这次连不上
   （`127.0.0.1:16443 connection refused`，隧道没起），所以"线上确实是 `E2B_REAL_ROOT=1`"不是
   直接读 env 得来的，而是**行为判定**：只有 pivot 过的子进程才会命中那条捷径，而线上确实命中
   了；同一条探针在本机 `E2B_REAL_ROOT=0` 下不挂。要更硬的话，起隧道后
   `kubectl -n sandlock get pod e2b-worker-0 -o jsonpath='{.spec.containers[0].env}'` 补一条。
2. **没有实测打过补丁的 fork**（按纪律没改仓库）：修法方向的可信度来自"A 案的结果 == 模拟根
   的行为"，而模拟根形态是**实测 OK** 的（§1.4 `eb-image-emulated.log`）。真正修的时候第一件事
   应该是把 §4 的命令组在 `E2B_REAL_ROOT=1` 下跑一遍。
3. **本地 harness 的两处偏差，已确认与 N43 无关**：① `tar -cf /dev/null` 在本地三种形状里都死在
   `/dev/null: Cannot open`（线上不死），说明本地 harness 的 policy 与线上不同（`/dev` 的写权限
   声明），换线上跑才对得上；② pure 形状必须用**宿主 workspace 路径**当 cwd（`_view_cwd` 的
   设计，见 `executors/sandlock.py:2215-2243`），我第一版传 `/home/user` 得到的是 harness 的
   `chdir ... errno 2`，与 N43 无关。
4. **`find`/`du`/`tar` 各自用哪条 syscall 变体**是按标准库行为解释的（fts 的 `fstatat`+`openat`
   下降、du/tar 的父 dirfd `fstatat`/`openat`），没有逐条 strace；§1.2 已经把**每条变体的
   errno** 实测出来了，两者的组合足以解释现象，但"哪个工具走哪条"这一层是解释不是取证。
5. **`host_to_virtual` 的别名选择**：`/home/user` 与 `/workspace` 是同一个宿主目录
   （`resolve.rs::host_to_virtual` 的 tie-break 是声明顺序），所以映射回来的虚拟拼写是
   `/home/user`。若将来有人把声明顺序改了，拿到的会是 `/workspace` —— 不影响本缺陷的成立与修
   法，但会影响"修好后 `find` 打印的路径拼写"，写用例时别把 `/home/user` 写死。

---

## 7. 附：本次新增的文件（都在 `tmp/`，未动仓库其它文件）

| 文件 | 用途 |
|---|---|
| `tmp/n43/n43-repro.py` | 线上最小复现（判据命令组） |
| `tmp/n43/repro-cluster-r1.log` / `-r2.log` | 线上原始输出（两次逐字一致） |
| `tmp/n43/n43-errno-probe.py` + `errno-probe-cluster.log` | 线上每条 dirfd 变体的 errno |
| `tmp/n43/readlink-semantics.sh` + `child-body.sh` | 内核语义探针（fd 归属决定 readlink 拼写） |
| `tmp/n43/eb-shapes.py` + `eb-{image-real,image-emulated,pure}.log` | 本机 E2B 全栈三形态 A/B |
| `tmp/n43/fork-repro.sh` | fork 级 CLI 复现（未跑通：CLI 的 COW workdir 规范化在 chroot 形状下先失败，`[读代码]` `cow/seccomp.rs:663` —— 保留以便后续用原生 CLI 复现时省事） |
