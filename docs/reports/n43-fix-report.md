# N43 修复报告：真根下 dirfd 相对路径被当成"虚拟路径"

状态：**已修 + 已钉**。fork `d750fa1`，父仓指针另行提交（见 §7）。基线已同步（`core_integ 562 -> 563`，实测）。
本文所有"实测"都标了出处；未实测的只有 §4 的 COW 结论与 §9 的若干判断，均标 `[读代码]`。
工作目录 `/Users/polus/project/ai/sandlock-e2b`，fork 子模块 `third_party/sandlock`。

## 0. 一句话结论

按定因给的**最小修（方案 A）**改了一处：`build_virtual_path` 的 dirfd 分支现在**先做 host→virtual
映射，映射不到才当内核报的拼写已经是虚拟路径**。三种形态（模拟根 / 真根 / pure 的宿主根）原来只有
真根是坏的，现在一条用例同时钉住三种，且这条用例在 tip 上是**实测红**、修完**实测绿**。
`cow/dispatch.rs` 的同族写法**不是同一个 bug，没动**（理由见 §4）。

## 1. 改了什么

| 文件:行 | 改动 |
|---|---|
| `crates/sandlock-core/src/chroot/dispatch.rs:617-627` | **唯一的逻辑改动**。dirfd 分支：`ctx.host_to_virtual(&base_host).or_else(\|\| ctx.reported_to_virtual(...))?` —— 能映射就先映射，映射不到才回落到"已经是虚拟路径"。代价是一次**已有的** mount 前缀比较（`chroot/resolve.rs:58-86`，无新 syscall）；副作用是 `child_is_pivoted()` 里那次 `stat("/proc/<pid>/root")` 从热路径挪到了 fallback（fd 基路径通常能映射，所以真根下反而少一次 stat） |
| `crates/sandlock-core/src/chroot/dispatch.rs:360-376` | 订正 `reported_to_virtual` 的注释。原文断言 "a pivoted child's reports are already virtual" 是**错的通称**（对 cwd 成立、对 fd 不成立），它正是让那条捷径看起来对 fd 也安全的原因。现在写明：cwd 一族是真虚拟，`/proc/<pid>/fd/N` 不是，所以 dirfd 站点先映射 |
| `crates/sandlock-core/tests/integration/test_chroot.rs:973-1107` | 新用例 `test_chroot::test_dirfd_relative_reads_resolve_in_both_chroot_shapes`（见 §3） |
| `tests/rootfs-helper.c:815-890` + `:921` | 新 helper 命令 `dirfd-probe <dir> <name>`：对同一个名字做 `fstatat`（follow）、`fstatat(AT_SYMLINK_NOFOLLOW)`、`openat`、`readlinkat`，每种一行 stdout，失败时打 `ENOENT(2)` 这种 token，**始终 exit 0**（一个探测失败不吞掉后面三行，红的时候四条全看得见） |
| `docs/test-baseline.md:336` | `core_integ = 563`（562 -> 563，+1） |

没有其它改动：没碰 wheel、没碰集群 / k0s、没碰这 4 个文件之外的任何东西。

## 2. RED / GREEN 原始输出

跑法（父仓 `deploy/scripts/fork-gate.sh`，uid 65534，与门禁同形）：

```bash
cd /Users/polus/project/ai/sandlock-e2b
IMAGE=sandlock-dev-f17:latest deploy/scripts/fork-gate.sh --one 'dirfd_relative_reads'
```

### RED（tip，`dispatch.rs` 尚未改）

日志 `tmp/n43/RED.log`。关键：**一条断言里三种形态全部跑完**，所以红的时候能同时看到"只有真根坏"：

```
thread 'test_chroot::test_dirfd_relative_reads_resolve_in_both_chroot_shapes' (327) panicked at crates/sandlock-core/tests/integration/test_chroot.rs:1103:5:
assertion `left == right` failed: a dirfd-relative name must resolve exactly like its absolute spelling, in every root shape
  left: [("emulated chroot", "stat size=3000\nlstat size=8\nopen bytes=3000\nreadlink seed.bin", ""), ("real root", "stat ENOENT(2)\nlstat EACCES(13)\nopen EACCES(13)\nreadlink EACCES(13)", ""), ("identity root (the pure shape's mediated root)", "stat size=3000\nlstat size=8\nopen bytes=3000\nreadlink seed.bin", "")]
 right: [("emulated chroot", "stat size=3000\nlstat size=8\nopen bytes=3000\nreadlink seed.bin", ""), ("real root", "stat size=3000\nlstat size=8\nopen bytes=3000\nreadlink seed.bin", ""), ("identity root (the pure shape's mediated root)", "stat size=3000\nlstat size=8\nopen bytes=3000\nreadlink seed.bin", "")]

failures:
    test_chroot::test_dirfd_relative_reads_resolve_in_both_chroot_shapes

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 562 filtered out; finished in 0.05s
```

即 `stat ENOENT(2)` / `lstat EACCES(13)` / `open EACCES(13)` / `readlink EACCES(13)` —— 与定因 §1.2
线上 errno 探针**逐条同形**（must-exist 一族 ENOENT，nofollow / open / readlink 一族 EACCES），
正是 `find`/`du`/`tar` 在生产沙箱里对 workspace 全线 EACCES 的那条路径。

### GREEN（`dispatch.rs` 改完，同一用例、同一断言）

日志 `tmp/n43/GREEN.log`：

```
running 1 test
test test_chroot::test_dirfd_relative_reads_resolve_in_both_chroot_shapes ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 562 filtered out; finished in 0.05s
```

### 整档门禁（`deploy/scripts/fork-gate.sh`，非 root 全相位）

日志 `tmp/n43/full-gate-final-r2.log`：

```
    ==> core_lib           913 passed -- matches baseline
    ==> core_integ         563 passed -- matches baseline
    ==> ffi                104 passed -- matches baseline
    ==> cli                 98 passed -- matches baseline
    ==> supervise           55 passed -- matches baseline
    ==> supervise_cost       3 passed -- matches baseline
    ==> cli_build            0 passed -- matches baseline
    ==> python             465 passed -- matches baseline
==> 门禁通过
```

**未做**：`--oci-root` / `--supervise-root` / `--mediation-2uid` 三个 root 相位本机没跑。理由与证据：
oci 相位走的是**模拟根** chroot（`crates/sandlock-oci/src/policy.rs:375` 只 `chroot(rootfs)`，不设
`real_root`），而本次改动在模拟根下是**逐字等价的**（见 §3 的 emulated 实测与 §5 的等价论证），
且测试条数没有变化。要从"没跑"变成"跑过"是另一轮的事，不在这条回归的判据里。

## 3. 形状：三种根都钉在同一条用例里

用例对每个形态**都跑同一组四条探测**（在 mount point 上开 dirfd + 名字是指向文件的符号链接），
只断言一次 `[(label, stdout, stderr)] == [(label, 期望四行, "")]`：

| 形态 | 策略 | 结果（修前 / 修后） |
|---|---|---|
| 模拟根（`E2B_REAL_ROOT=0` 的形态） | `chroot(rootfs)` + `real_root(false)` + `fs_mount("/work", ws)` | **修前就正确** / 修后不变 |
| 真根（`E2B_REAL_ROOT=1`，线上形态） | 同上 + `real_root(true)` | **修前 ENOENT + EACCES×3** / 修后正确 |
| pure（无 image rootfs） | `chroot("/")`（N15：宿主根，恒等翻译）+ 纯形态那条策略（`/usr`、`/lib`、`/bin`、`/opt` 可读，workspace 可写） | **修前就正确** / 修后不变 |

为什么 pure 一定不受影响（代码级）：`chroot_root == "/"` 时 `host_to_virtual` 的第一条候选就是
`("/", "/")`，任何绝对路径都 `starts_with("/")` ⇒ 映射结果恒等于输入，与改动前的"pivoted ⇒ 原样放行"
**逐字相同**。这正是"改前 OK、改后必须还 OK"的那条等式，而它现在也被这条用例实测钉住了。

三条 fixture 细节值得记下来（都是本机实测踩到的，不是推断）：

* 真根那两条的 `/work` **必须先在 rootfs 里建出来**：`realroot.rs` 的挂载点缺失是**报错**而不是
  "静默不存在"，envd 也是在建 policy 时一并建的（`_ensure_chroot_mount_points`）。
* pure 那条的 workspace **不能放在符号链接下面**：本 lane 的 `third_party/sandlock/target` 是
  `target -> /src/target-linux`，而授权路径是按**内核解析后**的拼写比较的（见 §9 第 4 条的实测），
  所以 pure 的 fixture 改用仓库自己的、无链接的 `tmp/`，并在建好后 `canonicalize`。
* 这四条探测对挂载源 / 宿主路径的**解析**与**授权**是两件事：前者早就对，坏的一直是后者。

## 4. `cow/dispatch.rs` 同族：**不受影响，且不该照抄这个修法**

定因点名了 `crates/sandlock-core/src/cow/dispatch.rs:110-137`（`resolve_at_path_with_virtual`）：
它同样只 `readlink("/proc/<pid>/fd/N")` 后**直接 join**，连 host→virtual 都没有。结论是**它不是同一个
bug**，按最小改动原则**没动**。`[读代码]` 的依据：

* COW 这一层的比较域是**宿主路径**：结果先过 `map_cow_upper_path`（`cow/dispatch.rs:142-148`，
  按 `cow.upper_dir()` 的宿主前缀剥离），再 `cow.matches(&path)`（`cow/seccomp.rs:712`，
  `p.starts_with(&self.workdir_str) && !p.starts_with(&self.storage_dir)`），而 `workdir` 在
  `SeccompCowBranch::create` 里被 `canonicalize()`（`cow/seccomp.rs:663-677`）。
* 也就是说：**内核报宿主拼写正是 COW 想要的**。N43 的错在 chroot 那一层"把宿主拼写当虚拟拼写"，
  COW 没有这条"pivoted ⇒ 已经是虚拟"的捷径；把它的结果改成虚拟拼写反而会把匹配打断。
* 真根下 COW 只剩一个**漏拦截**方向的缺口（孩子自己 mount 树上的 fd 会报虚拟拼写、匹配不上宿主
  workdir 前缀 ⇒ `Continue` ⇒ 内核自己解析，答案仍然对，只是没被 COW 拦）：这是"少拦"，不是"解析错"，
  与 N43 的方向相反；而且 `envd_service/` 里**一处 `cow` 都没有**（实测 `rg -n "cow|Cow" envd_service/`
  无输出），部署形态根本不走 COW。
* 副作用证据：改动后 `test_cow::` 26/0 绿、整档 core_integ 563/0 绿。

## 5. 同名目录歧义：风险写清（以及为什么当前部署碰不到）

方案 A 是**字符串启发式**：`host_to_virtual` 只做 `host_path.starts_with(source)` 的前缀匹配
（候选 = `("/", chroot_root)` + 每条挂载的宿主源；最长源胜，等长按声明顺序，`resolve.rs:58-86`）。
所以"先映射"会把**恰好以外源拼写开头的虚拟路径**也映射掉。什么形状才会歧义、以及为什么现在的部署不在其中：

1. **要歧义，必须拿到一个"内核报虚拟拼写"的 fd。** 但沙箱里几乎每个 path-based `open` 都由中介在
   宿主打开后用 `ADDFD` 投递（`chroot/dispatch.rs:800` 的 `resolve_chroot_path` → `:922`
   `inject_watched`，`:929-1000` 的成功分支全走注入），部署也不走 COW，所以孩子手里**根本没有**
   挂在它自己 mount 树上的 path fd ⇒ 内核报的**总是宿主拼写** ⇒ 映射永远是对的。真正会报虚拟拼写的
   只剩 `cwd` 一族，而它走 `AT_FDCWD` 分支（`:610-612`），本次没动。
2. **即使出现那个 fd，多数情况下映射回的还是同一个对象**：一个虚拟拼写若以某条挂载的**宿主源**开头，
   那条挂载的源就是同一个宿主目录（bind mount 是同一个 inode），映射出来的虚拟路径再解析回去仍是
   那个 inode。真正会指向**另一个对象**的，只有"镜像自己就存在一条与宿主源同名的目录"这一种形状：
   例如镜像里内建了 `/var/lib/e2b-sandboxes/<本沙箱 id>`（或 `/var/lib/e2b-sandboxes/<id>/rootfs` 这种
   前缀）—— 即镜像必须是**用这个沙箱自己那棵树**做出来的。`<id>` 是随机的 `sbx_<hex>`，
   `python:3.11-slim` 这类镜像里不存在这种目录，当前部署不会碰到；要紧的是它需要"镜像内同名目录 +
   孩子自持该类 fd"两者同时成立。
3. 剩下那个**真实存在**的启发式副作用不是错误而是**拼写**：`/workspace` 与 `/home/user` 是同一个
   宿主目录时，`host_to_virtual` 按**声明顺序**挑一个（`resolve.rs:411-430` 的 tie-break 用例），
   所以修好后 `find` 打印的可能是 `/workspace/...` 也可能是 `/home/user/...`。用例因此**只断言
   `seed.bin` 这个相对名与四项数字**，不写死任何一侧的绝对拼写（定因 §6.5 的提醒）。
4. 要**根治**得走定因的方案 C（`pidfd_getfd` 复制 dirfd + `openat(O_PATH)` 让内核沿 fd 自己的
   mount 解析再映射回来）：那要加 `pidfd_getfd` + 一次 openat，落在 `du`/`tar` 这种逐条 `fstatat`
   的高密度路径上，还会新增 `EBADF` / ptrace 失败模式。建议留给 N14"退役模拟"那一步。

## 6. 基线：改前 / 改后

`docs/test-baseline.md`，`scripts/test-all.sh` 按**相等**判定（多一条也判红）：

| 套件 | 改前 | 改后 | 实测 |
|---|---|---|---|
| `core_integ` | 562 | **563** | 全相位门禁 `563 passed -- matches baseline`（`tmp/n43/full-gate-final-r2.log`）；单跑族 `test_chroot::` 50/0（改前 49/0） |
| 其它 7 套 | 913 / 104 / 98 / 55 / 3 / 0 / 465 | 不变 | 同一份门禁日志逐条 `matches baseline` |

改前那次**失败的**单跑也很有用：`562 filtered out` ⇒ 这个 binary 里原本正好 562 条，加 1 条即 563。

## 7. 提交

* **fork**：`third_party/sandlock` @ `d750fa1`（`fix(chroot): map a dirfd's host spelling back, do not
  take it as virtual`），基于 tip `6ff2505`。提交用 pathspec 暂存，`git diff --cached --name-only`
  确认只有 §8 那 4 个文件。
* **父仓**：只含 submodule 指针（`6ff2505 -> d750fa1`），`git diff --cached --name-only` 只有
  `third_party/sandlock`；父仓里其它既有的未提交改动（`control_plane/`、`docs/`、`tests/` 等）
  **没有被碰**。
* 这份报告在 `.superpowers/`（父仓 `.gitignore:19` 忽略），按既有惯例不进任何提交。

## 8. 文件清单（fork，4 个）

| 文件 | 行 |
|---|---|
| `crates/sandlock-core/src/chroot/dispatch.rs` | `:360-376`（注释订正）、`:617-627`（逻辑，1 个表达式） |
| `crates/sandlock-core/tests/integration/test_chroot.rs` | `:948-1107`（注释 + 新用例，1 条 `#[tokio::test]`） |
| `tests/rootfs-helper.c` | `:815-890`（`dirfd-probe`）、`:921`（dispatch 注册） |
| `docs/test-baseline.md` | `:336`（`core_integ = 563`） |

证据文件（父仓 `tmp/`，gitignore）：`tmp/n43/RED.log`、`tmp/n43/GREEN.log`、
`tmp/n43/full-gate-final-r2.log`、`tmp/n43/core_integ-wedge.log`、`tmp/n43/prefix-isolated-restore-test.log`。

## 9. 担忧 / 没做的事

1. **本机整档非 root 门禁不稳定，这一轮撞到过一次挂起**（不是红，是挂）。
   第一次全相位跑到 `test_instance_exec::test_a_child_restored_into_a_session_keeps_the_session_executable`
   时挂住 >13 分钟（日志 `tmp/n43/core_integ-wedge.log`；我 kill 掉容器，rc=137），重跑后 91s 内
   563/0 全绿。**它不是 N43 相关**，三条依据：(a) 那个用例的 policy `base_policy()` 没有 chroot，
   而 chroot 处理器只在 `policy.chroot_root.is_some()` 时注册（`seccomp/dispatch.rs:599`），
   本次改动的那段代码在它里面根本不会被调用；(b) 它自己就是 `#[tokio::test]`（单线程 runtime）
   + 阻塞读，正是同文件里 `6367c26` 记为 FUP-29 的那类挂起（兄弟用例已被改成多线程 runtime，
   这一条没有）；(c) 带修复的那次全相位跑里它 88s 内就过了（`ok`）。按 FUP-09 我只保留了第一次
   挂起的那份日志、没有覆盖它。**需要人拍板的是：这条本机 flake 要不要单独开一条 issue。**
2. **没在集群上跑修好的 wheel**（按纪律：不重建 wheel、不推集群）。所以"线上
   `find`/`du`/`tar` 四条 rc 全 0"这一步**由你来做**。本报告能给的最强本机证据是：
   同一条 dirfd 相对路径在真根形态下修前 errno 与线上探针逐条同形（§2），修后在**三种形态**
   下都给与绝对路径完全一致的答案（§3）；命令组判据仍是定因 §4 那六条。
3. **`cow/dispatch.rs` 的结论是读代码 + 族内测试绿，不是造一个 COW×真根 的实测**
   （部署不走 COW，造这个形状的收益低于代价）。若 N14 真要把 COW 也搬到真根上，那一处要重新过一遍。
4. **顺手实测到一个与本缺陷无关、但值得记一笔的性质**：授权路径是按**内核解析后**的拼写比较的，
   所以"声明在符号链接下面的授权"匹配不上（本 lane 的 `target -> /src/target-linux` 就是这么暴露的：
   `chroot("/")` 下 `fs_read(<带链接的路径>)` 的目录列举被判 EACCES，而同一路径的 `fs_write`
   —— 在 readable 非空、必须走真实查找时 —— 才能命中）。当前部署的 workspace 与系统目录都不含
   符号链接，pure 形态也已被实测 OK，所以**这不是线上问题**；只是写这类 fixture 时的一个坑，
   已写进用例注释。
5. **歧义风险本身没有测试钉**（§5）：要钉就得造"镜像内存在与宿主源同名的目录"的 fixture，
   而且现在没有可靠手段让沙箱拿到"自持 mount 上的 path fd"来触发它。方案 C 落地时这条会自然消失。
