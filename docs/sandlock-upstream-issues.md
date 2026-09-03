# sandlock 上游问题记录（fork: imhun/sandlock，运行时基线 `upstream-pr/netns-free-clean`）

记录在本仓库实测发现、需在 sandlock fork/上游修复的问题。GitHub 侧未直接提 issue：
本机无 `gh` CLI，且现有 `GITHUB_TOKEN` 只读（推送/写 API 均 403，见
`docs/HANDOFF.md`「上游 PR」段），所以每条都在这里留可复现脚本与源码定位，
换到有写权限的环境时可直接转成 issue。

---

## SL-1 路径中介（USER_NOTIF）以 supervisor 身份执行系统调用，沙箱自己的文件不再属于自己

- **类型**：隔离语义缺陷（DAC 层面），非远程可利用漏洞；在共享/可写目录上会破坏沙箱间隔离
- **严重度**：High（多租户 worker 场景）
- **受影响**：`0.9.0-beta` @ `be387c7`（`wheels/fork` 内 `.so` 同样表现）；Linux，root supervisor
- **在本仓库的表现**：HANDOFF 的 T1/T5（`chmod` EPERM、共享卷 1777+sticky 失效）

### 结论

当策略启用**路径中介**时（实测触发条件：`fs_denied` 非空、或 chroot/镜像 rootfs；源码里还有第三组 `cow_path_syscalls()`，对应 COW 分支，本文未单独验证其触发条件），
fork 会用 seccomp `SECCOMP_RET_USER_NOTIF` 把一批文件系统调用交给 supervisor 代执行：

```text
crates/sandlock-core/src/seccomp_plan.rs
  cow_path_syscalls():    openat openat2 execve unlinkat mkdirat mknodat renameat2
                          symlinkat linkat fchmodat fchownat truncate utimensat
                          newfstatat statx faccessat readlinkat getdents64 chdir getcwd
                          (+ 各 arch 的旧式 open/unlink/chmod/chown/…)
  chroot_path_syscalls(): 同一组路径相关调用
```

而 `crates/sandlock-core/src/seccomp/notif.rs` 里**没有任何调用方身份切换**
（`grep setfsuid|seteuid|geteuid notif.rs` 无命中），所以这些调用是以 **supervisor（root）**
的身份、按 **supervisor 的 DAC 视角** 完成的。子进程自己的 Landlock ruleset 仍然生效
（越出可写集合的写入照旧被拒，见下），但凡是被中介的那一次操作，内核看到的"操作者"是 root。

后果（都实测到了）：

1. 沙箱新建的文件属主是 **uid 0**，且子进程请求的 mode 不会被应用（`chmod` 返回 EPERM）；
2. `unlinkat`/`renameat2` 也被中介 ⇒ **删除/改名按 root 的权限判定**：共享目录（1777 +
   sticky）里"非 owner 不得删他人文件"这条保护不再成立，一个沙箱可以删掉另一个沙箱的文件；
3. `fchmodat`/`fchownat` 同样按 root 判定（本次未测出越权改他人文件属主的用例，但判定主体
   错位，值得单独审计）；
4. worker 上会积累"由不可信代码产生、但属主是 root"的文件：按 uid 做的审计、配额归属
   （project id）、后续清理都会失真。

没有发现 Landlock 白名单被绕过：`fs_denied` 开启时，向 `/var/lib`、`/etc` 这类不在
`fs_readable/fs_writable` 内的路径写入仍然失败。

### 复现

一次性 privileged 容器内运行（纯 API，不依赖本仓库代码也可精简）：

```python
# python3 repro.py            需要: sandlock wheel, /var/lib 可写, root
import os, subprocess, sys, tempfile
from pathlib import Path
from sandlock import Sandbox

UID_A, UID_B = 4242, 4343
shared = Path(tempfile.mkdtemp(dir="/var/lib")); os.chmod(shared, 0o777)

def sbx(uid):
    ws = Path(tempfile.mkdtemp(dir="/var/lib"))
    os.chown(ws, uid, uid); os.chmod(ws, 0o700)
    return Sandbox(fs_writable=[str(ws), str(shared)],
                   fs_readable=["/usr", "/lib", "/bin"],
                   fs_denied=["/dev/shm"],          # ← 打开中介；换成 [] 即对照组
                   uid=uid, gid=uid, max_memory="256M", max_processes=32,
                   max_open_files=512, max_cpu=100, clean_env=True, cwd=str(ws))

a, b = sbx(UID_A), sbx(UID_B)
print(a.run(["/bin/sh", "-c", f"printf secret-A > {shared}/a.txt; chmod 600 {shared}/a.txt; echo chmod=$?"]).stdout)
print(b.run(["/bin/sh", "-c", f"rm -f {shared}/a.txt; echo rm=$?"]).stdout)          # 期望 EPERM（sticky）
print(subprocess.run(["ls", "-n", str(shared)], capture_output=True, text=True).stdout)
```

实测输出（`fs_denied=["/dev/shm"]`，即中介开启）：

```text
chmod=1  chmod: changing permissions of '.../a.txt': Operation not permitted
rm=0                                     # ← B 删掉了 A 的文件
-rw-r--r-- 1 0 0 ... a.txt               # ← 属主是 root，不是 4242
```

对照组（`fs_denied=[]`，无 chroot）：

```text
chmod=0
rm=1  rm: cannot remove '.../a.txt': Operation not permitted   # ← sticky 保护正常生效
-rw------- 1 4242 4242 ... a.txt                               # ← 属主正确
```

同一条策略在 E2B 侧的两种形态都复现：镜像 rootfs（chroot）形态下 `netns` 无关、
纯 sandlock 形态下只要下发 `fs_denied` 也一样。

### 期望行为

中介只应改变**路径解析**（chroot 视图、COW 覆写位置、deny 判定），不应改变**操作者身份**：

- 执行被中介的调用前 `setfsuid/setfsgid(caller_uid, caller_gid)`，执行后还原
  （或用 `openat2`+`O_CREAT` 后 `fchown` 回调用方，再把 fd 传回）；
- 需要 root 权限的部分（例如访问宿主真实路径）应以"代理"名义完成**并显式复现 DAC 结论**：
  对 `unlinkat/renameat2/fchmodat/fchownat` 这类调用，先按**调用方 uid** 做与内核等价的
  DAC 判定（owner / sticky / CAP_FOWNER 是否相对该 inode 的 mount userns 成立），
  不成立就返回 EPERM，而不是让 supervisor 直接成功；
- 建议同时提供策略开关（例如 `mediation_run_as=caller|supervisor`），便于既有依赖 COW/chroot
  语义的调用方渐进迁移。

### E2B 侧已做的缓解（不改上游）

- 纯 sandlock（无 chroot）形态不再下发 `fs_denied`：那些路径本来就不在 Landlock 可读白名单里，
  denial 冗余，却要为"属主错位"付代价（`envd_service/executors/sandlock.py`）。
  该形态实测恢复：属主 = 沙箱 host uid、`chmod` 正常、跨 uid sticky 保护真的生效。
- 镜像 rootfs 形态必须挂进容器 `/dev`（PTY 需要 `/dev/ptmx`、`devpts`），而 `fs_mount`
  只能挂目录根，无法只暴露单个设备结节点 ⇒ 仍需 `fs_denied` 挡住 `/dev/shm`、`/dev/mqueue`，
  该形态的属主问题保留，用 `xfail(strict=True)` 显式跟踪（HANDOFF T5、
  `tests/contract/test_uid_permissions.py`）。
- 相关上游问题面（同一机制，另行验证）：T4 `net_isolation` + chroot 下 MCP 入站映射起不来。
