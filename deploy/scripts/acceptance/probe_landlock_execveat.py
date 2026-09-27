"""Can a Landlock-confined process exec a binary it only holds *by fd*?

This is the crux of the real-root checkpoint/restore plan. The restore stub is a
host build artifact exec'd by path today, which no chroot root can resolve
(measured: `execvp '<target>/restore-stub': No such file or directory`, then a
10 s READY timeout). Executing it by descriptor -- `execveat(fd, "",
AT_EMPTY_PATH)` -- needs no path resolution at all, so it would work inside a
pivoted real root *if* Landlock permits it: its rules are evaluated against the
file's path hierarchy, and an fd-carried file may have none the ruleset can see.

The probe builds its own Landlock domain (granting a decoy directory only),
confirms that a *path* exec outside that grant is refused, and then measures the
fd exec. Both are reported with their errno.

    docker run --privileged --rm -v "$PWD":/src -w /src sandlock-dev:latest \
        python3 /src/tmp/k0s/probe_landlock_execveat.py
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import struct
import sys
import tempfile

libc = ctypes.CDLL("libc.so.6", use_errno=True)

SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446

LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

# Rights this ABI knows; handling all of them keeps the domain realistic.
ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13
ACCESS_FS_TRUNCATE = 1 << 14
ACCESS_FS_IOCTL_DEV = 1 << 15
ALL_FS = (1 << 16) - 1

AT_EMPTY_PATH = 0x1000


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


def create_ruleset(handled: int) -> int:
    attr = RulesetAttr(handled_access_fs=handled)
    fd = libc.syscall(
        SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    return fd


def add_path_rule(ruleset_fd: int, parent_fd: int, allowed: int) -> None:
    buf = struct.pack("<Qi", allowed, parent_fd)  # struct landlock_path_beneath_attr
    rc = libc.syscall(
        SYS_landlock_add_rule,
        ruleset_fd,
        LANDLOCK_RULE_PATH_BENEATH,
        buf,
        0,
    )
    if rc < 0:
        raise OSError(ctypes.get_errno(), "landlock_add_rule")


def restrict_self(ruleset_fd: int) -> None:
    # Landlock requires no_new_privs (or CAP_SYS_ADMIN) before self-restriction;
    # this is what every sandbox does before installing the domain.
    PR_SET_NO_NEW_PRIVS = 38
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
    if libc.syscall(SYS_landlock_restrict_self, ruleset_fd, 0) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")


def exec_result(argv_kind: str, stub: str, stub_fd: int) -> object:
    """Fork, exec once, and report what the kernel said."""
    pid = os.fork()
    if pid == 0:
        try:
            null = os.open("/dev/null", os.O_RDONLY)
            os.dup2(null, 0)
        except OSError:
            pass
        path = ctypes.create_string_buffer(stub.encode())
        argv = (ctypes.c_char_p * 2)(path.value, None)
        envp = (ctypes.c_char_p * 1)(None)
        if argv_kind == "path":
            libc.execve(path, argv, envp)
        else:
            libc.syscall(
                322,  # SYS_execveat
                stub_fd,
                b"",
                argv,
                envp,
                AT_EMPTY_PATH,
            )
        os._exit(ctypes.get_errno() & 0xFF)
    _pid, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return 0 if code == 0 else {"exit": code, "name": errno.errorcode.get(code, "?")}


def main() -> int:
    grant_stub = "--grant-stub" in sys.argv[1:]
    abi = libc.syscall(SYS_landlock_create_ruleset, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
    print(f"landlock ABI: {abi}, grant_stub={grant_stub}")

    workdir = tempfile.mkdtemp(prefix="ll-execveat-")
    decoy = os.path.join(workdir, "decoy")
    os.mkdir(decoy)
    # A *static* binary (the fork's rootfs-helper) as the stub stand-in: a
    # dynamic one would need its interpreter readable (`/lib/...`, outside the
    # grant) and the EACCES would be about the interpreter, not about the file
    # the syscall named -- the first version of this probe measured exactly that
    # confusion.
    static_binary = "/src/third_party/sandlock/tests/rootfs-helper"
    stub = os.path.join(workdir, "restore-stub-copy")
    shutil.copy(static_binary, stub)
    os.chmod(stub, 0o755)
    stub_fd = os.open(stub, os.O_PATH | os.O_CLOEXEC)
    # Everything the probe needs has to exist *before* the domain is armed: once
    # restricted, /bin/true is no longer readable (that is the point of the
    # control below), so even `shutil.copy` is refused.
    inside = os.path.join(decoy, "stub-inside")
    shutil.copy(static_binary, inside)
    os.chmod(inside, 0o755)

    ruleset = create_ruleset(ALL_FS)
    decoy_fd = os.open(decoy, os.O_PATH | os.O_CLOEXEC)
    # Search (EXECUTE on a directory) for the ancestors, so reaching the grant
    # is possible at all: a rule on `decoy` alone does not let the walk through
    # `/` and `/tmp`, and the control would be measuring that instead.
    for ancestor in ("/", "/tmp"):
        fd = os.open(ancestor, os.O_PATH | os.O_CLOEXEC)
        add_path_rule(ruleset, fd, ACCESS_FS_EXECUTE)
        os.close(fd)
    add_path_rule(ruleset, decoy_fd, ALL_FS)
    exec_only = "--exec-only" in sys.argv[1:]
    if grant_stub or exec_only:
        # The question the plan rests on: when the ruleset grants the stub's own
        # *host* path (the shape `restore_interactive` would produce), does the
        # descriptor form go through? It should -- and it still sidesteps the
        # path *resolution* a chroot/real root breaks.
        rights = ACCESS_FS_EXECUTE if exec_only else ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE
        add_path_rule(ruleset, stub_fd, rights)
    restrict_self(ruleset)
    os.close(ruleset)
    print(
        f"domain armed: {decoy} in full, / and /tmp search-only"
        + (", stub file granted EXECUTE only" if exec_only else "")
        + (", stub file granted EXECUTE|READ" if grant_stub and not exec_only else "")
    )

    # Control 1: the ruleset must refuse the path form.
    by_path = exec_result("path", stub, stub_fd)
    print("execve(path)      ->", json.dumps(by_path))
    # Control 2: a path *inside* the grant still runs (the domain is not a
    # blanket denial).
    allowed = exec_result("path", inside, -1)
    print("execve(allowed)   ->", json.dumps(allowed))
    # The measurement: the same binary, carried by descriptor.
    by_fd = exec_result("fd", stub, stub_fd)
    print("execveat(fd)      ->", json.dumps(by_fd))
    print(
        "execve(path,stub) ->",
        json.dumps(by_path),
        "(the path form has no resolution problem here; in a chroot/real root it is the one that is unresolvable)",
    )

    def ran(result: object) -> bool:
        # The helper exits 1 on an unknown command, and 13 (EACCES) is the
        # domain's refusal -- what matters is which of the two happened.
        return not (isinstance(result, dict) and result.get("exit") == 13)

    print(
        "verdict: fd exec",
        "RAN" if ran(by_fd) else "was REFUSED (EACCES) -- Landlock applies to the fd form too",
    )
    try:
        shutil.rmtree(workdir)
    except PermissionError:
        # The domain forbids removing anything outside the grant -- which is
        # itself confirmation that the restriction is live.
        print(f"(left behind: removing {workdir} is denied by the domain)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
