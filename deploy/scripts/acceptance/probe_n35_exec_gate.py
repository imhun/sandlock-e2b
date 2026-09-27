"""N35 (directed follow-up): who refuses to exec a file the sandbox owns?

The N15 mediation experiment left two open questions behind, both recorded on
the N35 row of docs/task-backlog.md:

  1. In the chroot (production) shape, a file the sandbox just wrote into its
     own workspace cannot be exec'd -- EACCES, 50/50, with the file at
     -rwxr-xr-x, chmod rc=0, so it is a policy decision, not a mode bit.
  2. The same probe in the pure shape is clean (20/20), so the refusal is not
     "write then exec" in general.

Two candidate layers were named, and they need different fixes:

  * the mediator's execve handler only admitting paths in its readable set
    (ctx.can_read in crates/sandlock-core/src/chroot/dispatch.rs), with the
    workspace declaring fs_writable but not fs_read;
  * Landlock's EXECUTE right not being granted on the workspace, i.e. the
    question is policy coverage of the workspace path.

This probe separates them by observation, without patching the fork:

  * script (shebang) vs copied binary -- if both are refused, it is about the
    file's location, not about how the kernel runs it;
  * a file that existed in the workspace before the sandbox started -- if it is
    refused too, "write then exec" is not the trigger at all;
  * the same write in one command and the exec in the next command (a tick
    later) -- the ETXTBSY window question from the pure-shape finding;
  * an optional "grant" leg that adds the workspace's virtual spellings to
    fs_writable. If coverage is what is missing, that is the cheapest way to
    make the path rules exist, and it moves the refusal without touching any
    fork code.

Every leg gets its own executor (its own sandbox instance and mediator), so one
leg cannot poison the next, and every command is bounded by a timeout: a
command that never returns is printed as TIMEOUT instead of hanging the run.
That matters here -- the first version of this probe wedged on a mediated
access(2) with the supervisor idle, which is a result in its own right.

Usage (inside the lane container, i.e. the docker run of
deploy/scripts/test-prod-shaped.sh without the pytest tail):

    python3 -u tmp/k0s/probe_n35_exec_gate.py [chroot|pure] [grant] [leg|all|list]

"grant" only means anything for the chroot shape; it is ignored (and said so)
in the pure shape.
"""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

from tests.security.conftest import (
    SANDBOX_UID,
    require_mediation_capable,
    resolve_test_rootfs,
    run_sh,
    sandbox_tmpdir,
)

TIMEOUT_S = float(os.environ.get("N35_TIMEOUT_S", "30"))
LEGS = [
    "ctl",
    "root",
    "errno",
    "script",
    "interp",
    "binary",
    "preexist",
    "rootshebang",
    "hostinterp",
    "shebangguest",
    "pip",
    "twice",
    "afterfail",
    "wedge",
    "binloop",
    "policy",
    "staticbin",
    "rootstatic",
    "staticwrite",
    "hostuncovered",
    "binfmt",
    "denyview",
    "guestmount",
    "cwdprobe",
    "relerrno",
    "relbinary",
    "relscript",
    "split",
    "loop",
    "sloop",
]

# Where the probe parks its "script that lives in the image rootfs" fixture.
# The image rootfs is a shared cache directory, so this is one fixed name and
# it is overwritten harmlessly on every run.
ROOTFS_TOOL = "/usr/local/bin/n35_rootfs_tool"


def build_executor(shape: str, grant: bool):
    """A sandbox built the way the worker builds one, plus the optional grant."""
    from envd_service.executors.sandlock import SandlockExecutor
    from envd_service.route_b import RouteBConfig

    workspace = sandbox_tmpdir(suffix="-ws")
    chroot = shape == "chroot"
    image = "python:3.11-slim" if chroot else None
    rootfs = resolve_test_rootfs(image) if chroot else None
    host_uid = SANDBOX_UID if os.geteuid() == 0 else None
    extra = ["/workspace", "/home/user"] if (chroot and grant) else []
    # Mirrors `Settings.real_root` (E2B_REAL_ROOT): the probes build their own
    # executor, so the switch has to be read here rather than through the
    # factory. Default off, like the setting.
    real_root = os.environ.get("E2B_REAL_ROOT", "0").strip() == "1"
    pid_ns = os.environ.get("E2B_PID_NS", "0").strip() == "1"
    executor = SandlockExecutor(
        workspace_dir=str(workspace),
        base_image=image,
        image_rootfs=rootfs,
        host_uid=host_uid,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
        extra_fs_writable=extra,
        real_root=real_root,
        pid_ns=pid_ns,
        sandbox_id=f"sbx_n35_{uuid.uuid4().hex[:8]}",
        route_b=RouteBConfig(
            mode="auto",
            uid_start=host_uid if host_uid is not None else SANDBOX_UID,
            uid_size=2,
            tmp_root=sandbox_tmpdir(suffix="-route-b"),
        ),
    )
    return executor, workspace


def spell(shape: str, workspace: Path, name: str) -> str:
    """The guest spelling of a file in the sandbox workspace."""
    return f"/workspace/{name}" if shape == "chroot" else f"{workspace}/{name}"


def pt_interp_of(path: Path) -> str | None:
    """The ELF interpreter path, '' for a static ELF, None for a non-ELF.

    Static binaries matter here: the mediator injects an *in-memory* copy only
    when there is a PT_INTERP to patch, so a static binary is the one exec shape
    that reaches a real filesystem inode -- which is how we find out whether the
    workspace's own inode is exec-allowed at all.
    """
    import struct

    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if not blob.startswith(b"\x7fELF") or blob[4] != 2:
        return None
    (e_phoff,) = struct.unpack_from("<Q", blob, 0x20)
    (e_phentsize,) = struct.unpack_from("<H", blob, 0x36)
    (e_phnum,) = struct.unpack_from("<H", blob, 0x38)
    for index in range(e_phnum):
        off = e_phoff + index * e_phentsize
        (p_type,) = struct.unpack_from("<I", blob, off)
        if p_type == 1:  # PT_LOAD
            continue
        if p_type == 3:  # PT_INTERP
            (p_offset,) = struct.unpack_from("<Q", blob, off + 8)
            (p_filesz,) = struct.unpack_from("<Q", blob, off + 32)
            return blob[p_offset : p_offset + p_filesz].rstrip(b"\0").decode(
                errors="replace"
            )
    return ""


def find_static_elf() -> Path | None:
    """First static ELF worth exec'ing in the lane container, or None."""
    for candidate in (
        "/usr/sbin/docker-init",
        "/sbin/tini",
        "/bin/busybox",
        "/usr/bin/strace",
    ):
        path = Path(candidate)
        if path.exists() and pt_interp_of(path) == "":
            return path
    return None


def show(label: str, result) -> None:
    if result is None:
        print(f"LEG {label} TIMEOUT (> {TIMEOUT_S:.0f}s)")
        sys.stdout.flush()
        return
    code, out, err = result
    text_out = out.decode(errors="replace").strip().replace("\n", " | ")
    text_err = err.decode(errors="replace").strip().replace("\n", " | ")
    print(f"LEG {label} exit={code} out=[{text_out}] err=[{text_err}]")
    sys.stdout.flush()


async def run(executor, cwd, script: str):
    try:
        return await asyncio.wait_for(run_sh(executor, cwd, script), TIMEOUT_S)
    except asyncio.TimeoutError:
        return None


async def one_leg(shape: str, grant: bool, leg: str) -> None:
    executor, workspace = build_executor(shape, grant)
    ws = Path(workspace)
    cwd = Path("/home/user") if shape == "chroot" else ws
    try:
        require_mediation_capable(executor)
        tag = f"{shape}{'+grant' if grant else ''}/{leg}"
        if leg == "ctl":
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    'echo "uid=$(id -u) gid=$(id -g) cwd=$(pwd)"; /bin/echo ctl-ok',
                ),
            )
            return
        if leg == "root":
            # Is there a real chroot/mount namespace behind the virtual root?
            # If /proc/self/root is the host root, the kernel resolves a
            # shebang interpreter in the *host* path space, not the guest's.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "readlink /proc/self/root; readlink /proc/self/cwd; "
                    "ls -l /proc/self/root/bin/sh /bin/sh; "
                    "head -2 /proc/self/root/etc/os-release 2>/dev/null; "
                    "ls /proc/self/root | head -20",
                ),
            )
            return
        if leg == "errno":
            # Exact errno for a refused exec, straight from the guest's python.
            # No successful exec at the end of the list: an exec that *works*
            # would replace the process and read stdin, which is the pipe the
            # harness holds open -- that would look like a wedge and is not one.
            tool = spell(shape, ws, "tool_errno")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho errno-hi\\n' > {tool}; chmod +x {tool}; "
                    "python3 -c 'import errno, os, sys\n"
                    "for p in sys.argv[1:]:\n"
                    "    try:\n"
                    "        os.execv(p, [p])\n"
                    "    except OSError as e:\n"
                    f"        print(p, e.errno, errno.errorcode.get(e.errno), e.strerror)' "
                    f"{tool} {tool}-missing </dev/null",
                ),
            )
            return
        if leg == "script":
            tool = spell(shape, ws, "tool_script")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho script-hi\\n' > {tool}; chmod +x {tool}; "
                    f"ls -l {tool}; {tool}; echo exec_rc=$?",
                ),
            )
            return
        if leg == "interp":
            tool = spell(shape, ws, "tool_interp")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho interp-hi\\n' > {tool}; chmod +x {tool}; "
                    f"cat {tool}; /bin/sh {tool}; echo sh_rc=$?",
                ),
            )
            return
        if leg == "binary":
            tool = spell(shape, ws, "tool_bin")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"cp /bin/echo {tool}; chmod +x {tool}; ls -l {tool}; "
                    f"{tool} bin-hi; echo exec_rc=$?",
                ),
            )
            return
        if leg == "preexist":
            # Written by the probe process before the sandbox starts: neither
            # the write-then-exec ordering nor a mediator-held write descriptor
            # is in play for this one.
            tool = spell(shape, ws, "tool_preexist")
            (ws / "tool_preexist").write_text("#!/bin/sh\necho preexist-hi\n")
            (ws / "tool_preexist").chmod(0o755)
            show(tag, await run(executor, cwd, f"ls -l {tool}; {tool}; echo exec_rc=$?"))
            return
        if leg == "rootshebang":
            # The same shebang script, but living in the *image* rootfs instead
            # of the workspace: separates "the file is in a mount the rules do
            # not cover" from "any shebang script is refused in this shape".
            # The executor's own rootfs, not a second resolve_test_rootfs call
            # (that returns a fresh extraction the sandbox never looks at).
            rootfs = executor._image_rootfs
            fixture = Path(rootfs) / ROOTFS_TOOL.lstrip("/")
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("#!/bin/sh\necho rootshebang-hi\n")
            fixture.chmod(0o755)
            show(tag, await run(executor, cwd, f"ls -l {ROOTFS_TOOL}; {ROOTFS_TOOL}; echo exec_rc=$?"))
            return
        if leg == "pip":
            # A real product case: pip's console script is a shebang script in
            # the image (#!/usr/local/bin/python). `python3 -m pip` is the
            # binary-only control. Both are bounded by the guest's own timeout
            # so a wedge prints instead of stalling the run.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "ls -l /usr/local/bin/pip 2>/dev/null || ls -l /usr/local/bin/pip3; "
                    "timeout 10 /usr/local/bin/pip --version </dev/null; echo pip_rc=$?; "
                    "timeout 10 python3 -m pip --version </dev/null; echo m_rc=$?",
                ),
            )
            return
        if leg == "hostinterp":
            # Which path space does the *kernel* resolve a shebang interpreter
            # in? /usr/local/bin/python3 exists in both: the image (3.11) and
            # the lane container that hosts the sandbox (3.14). The version the
            # script prints names the space, and the three possible outcomes
            # each answer the question:
            #   py (3, 14)          -> resolved in the host space, allowed
            #   py (3, 11)          -> resolved in the guest space (real chroot)
            #   errno/EACCES, rc 126 -> resolved in the host space, refused
            # python3.14 alone exists only on the host, so it is the sharper
            # probe: "not found" means the guest space, a version means the host.
            plain = spell(shape, ws, "tool_hostinterp")
            only = spell(shape, ws, "tool_hostonly")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/usr/local/bin/python3\\nimport sys; print(\"py\", sys.version_info[:2])\\n' "
                    f"> {plain}; chmod +x {plain}; {plain}; echo rc_plain=$?; "
                    f"printf '#!/usr/local/bin/python3.14\\nimport sys; print(\"py\", sys.version_info[:2])\\n' "
                    f"> {only}; chmod +x {only}; {only}; echo rc_only=$?",
                ),
            )
            return
        if leg == "shebangguest":
            # The mirror image of hostinterp: an interpreter that exists ONLY in
            # the guest's own tree (a copy of /bin/echo inside the workspace).
            # If the kernel resolved shebangs in the guest space this would
            # print the script path; ENOENT (rc 127) says it looked in the host
            # space, where no such file exists.
            interp = spell(shape, ws, "interp_bin")
            script = spell(shape, ws, "tool_shebang_guest")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"cp /bin/echo {interp}; chmod +x {interp}; "
                    f"printf '#!{interp}\\n' > {script}; chmod +x {script}; "
                    f"ls -l {interp} {script}; {script}; echo rc=$?",
                ),
            )
            return
        if leg == "twice":
            # Two refused execs back to back in one command: the 20x loop hung,
            # so this asks whether a single repeat is enough to wedge.
            tool = spell(shape, ws, "tool_twice")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho twice-hi\\n' > {tool}; chmod +x {tool}; "
                    f"{tool}; echo first_rc=$?; {tool}; echo second_rc=$?",
                ),
            )
            return
        if leg == "afterfail":
            # Does a refused shebang exec wedge the *next* command in the same
            # instance? The 20x loop (write -> refused exec -> write -> ...)
            # timed out while two refused execs back to back did not.
            tool = spell(shape, ws, "tool_afterfail")
            after = spell(shape, ws, "after_fail")
            show(
                "a " + tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho af-hi\\n' > {tool}; chmod +x {tool}; "
                    f"{tool}; echo exec_rc=$?",
                ),
            )
            show("b " + tag, await run(executor, cwd, f"echo after > {after}; ls -l {after}"))
            return
        if leg == "policy":
            # What the mediator is actually handed. Spellings matter: a
            # Landlock rule is only installed when the path still exists after
            # the chroot translation, so the raw entries decide whether the
            # workspace (and which spellings of it) is covered at all.
            import json
            from glob import glob

            ceiling = executor._policy_ceiling()
            print(f"POLICY envd fs_writable={ceiling['fs_writable']}")
            print(f"POLICY envd fs_readable={ceiling['fs_readable']}")
            await run(executor, cwd, "true")
            found = sorted(glob("/tmp/*-route-b/**/policy.json", recursive=True))
            for path in found[-2:]:
                try:
                    doc = json.loads(Path(path).read_text())
                except Exception as exc:  # a missing/partial file is a data point
                    print(f"POLICY {path}: unreadable {exc}")
                    continue
                keep = {
                    key: doc[key]
                    for key in (
                        "chroot",
                        "chroot_root",
                        "chroot_readable",
                        "chroot_writable",
                        "fs_readable",
                        "fs_writable",
                        "fs_mount",
                        "mounts",
                    )
                    if key in doc
                }
                print(f"POLICY {path}: {json.dumps(keep, ensure_ascii=False)}")
            sys.stdout.flush()
            return
        if leg == "staticbin":
            # A static ELF is the only exec that reaches a *real filesystem
            # inode* (no PT_INTERP -> no memfd copy), so it answers "is the
            # workspace's own inode exec-allowed in this shape?".
            source = find_static_elf()
            if source is None:
                print(f"LEG {tag} SKIPPED (no static ELF found in the lane image)")
                return
            target = ws / "static_bin"
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
            # tini (docker-init) prints a version for --version and exits.
            show(
                tag + f" (via {source})",
                await run(
                    executor,
                    cwd,
                    f"ls -l {spell(shape, ws, 'static_bin')}; "
                    f"{spell(shape, ws, 'static_bin')} --version; echo exec_rc=$?",
                ),
            )
            return
        if leg == "rootstatic":
            # The same static binary, but in the image rootfs instead of the
            # workspace: separates "a static ELF cannot be exec'd at all" from
            # "a host-filesystem inode cannot be exec'd, a rootfs one can".
            source = find_static_elf()
            if source is None:
                print(f"LEG {tag} SKIPPED (no static ELF found in the lane image)")
                return
            fixture = Path(executor._image_rootfs) / "usr/local/bin/n35_static_bin"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(source.read_bytes())
            fixture.chmod(0o755)
            show(
                tag + f" (via {source})",
                await run(
                    executor,
                    cwd,
                    f"ls -l {ROOTFS_TOOL.rsplit('/', 1)[0]}/n35_static_bin; "
                    f"{ROOTFS_TOOL.rsplit('/', 1)[0]}/n35_static_bin --version; "
                    "echo exec_rc=$?",
                ),
            )
            return
        if leg == "staticwrite":
            # The *guest* writes a static binary and execs it in the same
            # command. That is the one combination the two previous findings
            # did not cover: the source inode now has an exec grant (so the
            # earlier EACCES is gone), and the mediator's write watch holds a
            # descriptor for a tick -- which is exactly the ETXTBSY window.
            source = find_static_elf()
            if source is None:
                print(f"LEG {tag} SKIPPED (no static ELF found in the lane image)")
                return
            # Park the source in the workspace from the probe process, then let
            # the *guest* copy it (a mediated write) and exec the copy.
            seed = ws / "static_seed"
            seed.write_bytes(source.read_bytes())
            seed.chmod(0o755)
            target = spell(shape, ws, "static_written")
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"cp {spell(shape, ws, 'static_seed')} {target}; chmod +x {target}; "
                    f"{target} --version; echo same_tick_rc=$?; sleep 1; "
                    f"{target} --version; echo next_tick_rc=$?",
                ),
            )
            return
        if leg == "denyview":
            # The policy denies /sys and /proc/kcore; under a real root those
            # paths resolve *inside the image* instead of being merely refused,
            # so this checks what is actually reachable there.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "ls /sys 2>&1 | wc -l; ls /sys/kernel 2>&1 | head -2; "
                    "cat /proc/kcore 2>&1 | head -c 60; echo kcore_rc=$?; "
                    "ls /proc 2>&1 | wc -l; "
                    "cat /proc/self/status 2>/dev/null | grep -c . ",
                ),
            )
            return
        if leg == "binfmt":
            # A *kernel-side format handler*: binfmt_misc matches by magic and
            # execs a registered interpreter, resolving that interpreter path
            # itself. Under the image rootfs the interpreter is therefore looked
            # up *inside the image* -- which is exactly the difference between
            # the emulated root and a real one.
            magic = "N35BINMAGIC"
            registered = Path("/proc/sys/fs/binfmt_misc/n35probe")
            register = Path("/proc/sys/fs/binfmt_misc/register")
            if not registered.exists() and register.exists():
                # Interpreter: an in-image path, so the "interpreter resolves in
                # the sandbox's tree" case is the one being measured.
                register.write_text(f":n35probe:M::{magic}::/bin/sh:\n")
            payload = ws / "n35_binfmt_payload"
            payload.write_text(f"{magic}\n/bin/echo binfmt-ran\n")
            payload.chmod(0o755)
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    f"ls -l {spell(shape, ws, 'n35_binfmt_payload')}; "
                    f"{spell(shape, ws, 'n35_binfmt_payload')}; echo binfmt_rc=$?",
                ),
            )
            return
        if leg == "guestmount":
            # Can the *guest* mount? This is the property N14's design depends
            # on: the fork's setup phase may mount, the workload must not. Run
            # in a lane whose container profile DOES admit the mount family
            # (the default lane has SYS_ADMIN, so the rule is included) to see
            # which layer actually refuses.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "echo uid=$(id -u); command -v mount unshare chroot; "
                    "grep -E '^(CapEff|Seccomp)' /proc/self/status 2>/dev/null; "
                    f"mkdir -p {spell(shape, ws, 'mnt')} && echo mkdir_ok; "
                    f"mount -t tmpfs none {spell(shape, ws, 'mnt')} 2>&1; echo mount_rc=$?; "
                    f"mount -t proc none {spell(shape, ws, 'mnt')} 2>&1; echo mount_proc_rc=$?; "
                    f"umount {spell(shape, ws, 'mnt')} 2>&1; echo umount_rc=$?; "
                    "unshare -m true 2>&1; echo unshare_rc=$?; "
                    "chroot / true 2>&1; echo chroot_rc=$?",
                ),
            )
            return
        if leg == "hostuncovered":
            # Pure-shape control: a binary on a host path that no rule covers.
            # An exec there must be denied if "files outside every rule are
            # denied" holds -- which is the claim the chroot-shape shebang
            # failure rests on.
            outside = Path("/var/tmp/n35-uncovered")
            outside.mkdir(parents=True, exist_ok=True)
            outside.chmod(0o755)
            source = find_static_elf()
            if source is None:
                print(f"LEG {tag} SKIPPED (no static ELF found in the lane image)")
                return
            target = outside / "static_bin"
            target.write_bytes(source.read_bytes())
            target.chmod(0o755)
            show(
                tag + f" (via {source})",
                await run(
                    executor,
                    cwd,
                    f"ls -l {outside}/static_bin; {outside}/static_bin --version; "
                    "echo exec_rc=$?",
                ),
            )
            return
        if leg == "cwdprobe":
            # Where is the sandbox's cwd, really? The sandbox's `pwd` prints the
            # path the *mediator* believes, so this compares inodes instead.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "pwd; stat -c '%i %n' . /home/user /workspace / 2>&1; "
                    "touch ./probe_cwd_marker && ls -la . | head -5; "
                    "echo marker_rc=$?",
                ),
            )
            return
        if leg == "relerrno":
            # Same question as relbinary, but with the raw errno instead of the
            # shell's rendering of it: the difference between "the file is not
            # there" and "the kernel refused it" is the whole diagnosis.
            import shutil as _shutil

            pre_bin = ws / "pre_rel_bin"
            _shutil.copy("/bin/echo", pre_bin)
            pre_bin.chmod(0o755)
            (ws / "pre_rel_script").write_text("#!/bin/sh\necho pre-rel-script\n")
            (ws / "pre_rel_script").chmod(0o755)
            script = (
                "printf '#!/bin/sh\\necho fresh-rel-script\\n' > ./fresh_rel_script; "
                "cp /bin/echo ./fresh_rel_bin; chmod +x ./fresh_rel_script ./fresh_rel_bin; "
                "ls -la .; "
                "python3 -c 'import errno, os, sys\n"
                "for p in sys.argv[1:]:\n"
                "    try:\n"
                "        os.execv(p, [p])\n"
                "    except OSError as e:\n"
                "        print(p, e.errno, errno.errorcode.get(e.errno), e.strerror)' "
                "./fresh_rel_bin ./fresh_rel_script ./pre_rel_bin ./pre_rel_script "
                "</dev/null"
            )
            show(tag, await run(executor, cwd, script))
            return
        if leg == "relbinary":
            # Exactly the shape the security test uses: a *relative* path, a
            # bare `cp` from PATH, and the exec in the same command.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "cp /bin/echo ./n35_rel_bin && chmod +x ./n35_rel_bin && "
                    "./n35_rel_bin rel-hi; echo rc=$?",
                ),
            )
            return
        if leg == "relscript":
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "printf '#!/bin/sh\\necho rel-script-hi\\n' > ./n35_rel_script && "
                    "chmod +x ./n35_rel_script && ./n35_rel_script; echo rc=$?",
                ),
            )
            return
        if leg == "wedge":
            # The 20x write->refused-exec loop timed out. Is five enough? The
            # loop shape is the one a user-level install has, so a wedge that
            # only needs a handful of iterations matters more than the exact
            # count.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "ok=0; i=0; while [ $i -lt 5 ]; do i=$((i+1)); "
                    f"f={spell(shape, ws, 'tool_wedge_$i')}; "
                    "printf '#!/bin/sh\\necho wedge-hi\\n' > $f; chmod +x $f; "
                    '$f; echo "iter $i rc=$?"; done; echo LOOP_DONE',
                ),
            )
            return
        if leg == "binloop":
            # The pure shape (and the N15 mediation experiment) reported
            # ETXTBSY for write-then-exec within one tick. A *binary* write
            # followed by an exec inside the same command is the same shape
            # with an exec the mediator can redirect, so this measures the
            # window where it is observable in the shape that ships.
            show(
                tag,
                await run(
                    executor,
                    cwd,
                    "ok=0; busy=0; other=0; i=0; while [ $i -lt 20 ]; do i=$((i+1)); "
                    f"f={spell(shape, ws, 'tool_binloop_$i')}; "
                    "cp /bin/echo $f; chmod +x $f; "
                    'if out=$($f 2>&1); then ok=$((ok+1)); else '
                    'case "$out" in *"Text file busy"*|*"errno 26"*) busy=$((busy+1));; '
                    '*) other=$((other+1)); echo "OTHER $i: $out";; esac; fi; done; '
                    'echo "RESULT ok=$ok busy=$busy other=$other"',
                ),
            )
            return
        if leg == "split":
            tool = spell(shape, ws, "tool_split")
            show(
                "a " + tag,
                await run(
                    executor,
                    cwd,
                    f"printf '#!/bin/sh\\necho split-hi\\n' > {tool}; chmod +x {tool}; "
                    f"ls -l {tool}",
                ),
            )
            show("b " + tag, await run(executor, cwd, f"{tool}; echo exec_rc=$?"))
            return
        if leg in ("loop", "sloop"):
            prefix = "tool_loop_" if leg == "loop" else "tool_sloop_"
            files = [spell(shape, ws, f"{prefix}{i}") for i in range(1, 21)]
            counted = (
                "ok=0; busy=0; denied=0; other=0; "
                + "".join(
                    f"i={i}; f={path}; printf '#!/bin/sh\\necho loop-hi\\n' > $f; "
                    "chmod +x $f; "
                    'if out=$($f 2>&1); then ok=$((ok+1)); else '
                    'case "$out" in *"Text file busy"*|*"errno 26"*) busy=$((busy+1));; '
                    '*"Permission denied"*) denied=$((denied+1));; '
                    '*) other=$((other+1)); echo "OTHER $i: $out";; esac; fi; '
                    for i, path in enumerate(files, start=1)
                )
                + 'echo "RESULT ok=$ok busy=$busy denied=$denied other=$other"'
            )
            if leg == "loop":
                show(tag, await run(executor, cwd, counted))
                return
            writes = (
                "".join(
                    f"printf '#!/bin/sh\\necho loop-hi\\n' > {path}; chmod +x {path}; "
                    for path in files
                )
                + "echo wrote=20"
            )
            show("a " + tag, await run(executor, cwd, writes))
            # A pause makes "next command" also mean "clearly past one tick".
            time.sleep(0.5)
            execs = (
                "ok=0; busy=0; denied=0; other=0; "
                + "".join(
                    f"i={i}; f={path}; "
                    'if out=$($f 2>&1); then ok=$((ok+1)); else '
                    'case "$out" in *"Text file busy"*|*"errno 26"*) busy=$((busy+1));; '
                    '*"Permission denied"*) denied=$((denied+1));; '
                    '*) other=$((other+1)); echo "OTHER $i: $out";; esac; fi; '
                    for i, path in enumerate(files, start=1)
                )
                + 'echo "RESULT ok=$ok busy=$busy denied=$denied other=$other"'
            )
            show("b " + tag, await run(executor, cwd, execs))
            return
        raise SystemExit(f"unknown leg {leg!r}")
    finally:
        try:
            executor.close()
        except Exception:  # a wedged instance must not mask the leg's result
            pass


async def main_async() -> int:
    # The executor logs the *context* of a failed exec (exit code, argv, rootfs
    # facts) at WARNING; a setup failure inside the sandbox is otherwise
    # invisible, because the child's stderr is not wired up yet.
    import logging

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s %(name)s: %(message)s",
    )
    shape = sys.argv[1] if len(sys.argv) > 1 else "chroot"
    grant = len(sys.argv) > 2 and sys.argv[2] == "grant"
    what = sys.argv[3] if len(sys.argv) > 3 else "all"
    if shape not in ("chroot", "pure"):
        raise SystemExit(f"unknown shape {shape!r}")
    if shape == "pure" and grant:
        print("note: grant is a chroot-only leg; ignoring it for the pure shape")
        grant = False
    if what == "all":
        todo = LEGS
    else:
        todo = [item for item in what.split(",") if item]
        for leg in todo:
            if leg not in LEGS:
                raise SystemExit(f"unknown leg {leg!r}; known: {', '.join(LEGS)}")
    print(f"== shape={shape} grant={grant} legs={todo} timeout={TIMEOUT_S:.0f}s")
    sys.stdout.flush()
    for leg in todo:
        try:
            await one_leg(shape, grant, leg)
        except Exception as exc:  # keep going: one bad leg is one data point
            print(f"LEG {shape}/{leg} RAISED {type(exc).__name__}: {exc}")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
