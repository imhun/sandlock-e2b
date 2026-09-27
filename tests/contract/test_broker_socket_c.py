"""Task C1: ``e2b-maint serve`` -- the C half of the per-node broker socket.

Why a socket at all: the C1 cut-over takes ``chown`` / ``rm`` / ``walk`` off the
worker pod's own capabilities. A **root** broker per node owns them and speaks
the frozen line-JSON protocol ``{"v":1,"args":["chown", ...],"timeout_s":300}``
on ``/run/e2b-broker/broker.sock`` (``E2B_PRIV_HELPER_SOCKET`` overrides the
path). Task 2 writes the Python transport for the same contract; this file pins
the C side of it:

* one request line in, one response line out -- ``ok``/``exit``/``stdout``/
  ``stderr`` for a request that ran, ``ok:false`` + ``error`` for one that
  never did;
* the socket is ``0660 root:<peer gid>`` and the gate is ``SO_PEERCRED``
  against ``E2B_BROKER_PEER_UID`` / ``E2B_BROKER_PEER_GID`` (default 65534) --
  a pooled sandbox uid cannot even ``connect()``, and a peer that fails the
  gate is refused **by the daemon, before it forks** (an unauthorized local
  peer must not be able to make the root broker spawn anything);
* a refused connection, a ``fork()`` failure or too many in-flight handlers
  cost that one connection and nothing else: the daemon stays up and serving;
* the daemon execs **its own image** (``/proc/self/exe``, which must be the
  compiled-in ``/var/lib/e2b-priv/e2b-maint``) and never a program from the
  request -- ``args[0]`` is one of ``chown`` / ``rm`` / ``walk``, so it is not
  a general launcher;
* the response is JSON, so a byte that is not UTF-8 (a Linux filename may be
  any byte but NUL and ``/``) comes back as ``\\udcXX`` -- Python's
  ``surrogateescape`` -- and the decoded path still equals ``os.fsdecode()`` of
  the name ``os.walk`` reported;
* the whitelist is the Python side's roots (*this* order): workspace base, the
  state base when it is a root of its own, the shared volume root, and -- new
  in C1 -- ``E2B_IMAGE_CACHE_DIR``, because a sandbox's secret file lives at
  ``<image_cache_dir>/secrets/<sandbox_id>/`` and a non-root worker has to be
  able to hand it to a pool uid. That last root appears **only when the
  deployment names it**: the Python side's unset default is cwd-relative, and
  a root the daemon resolves somewhere else is a root nobody means.

Root, ``SO_PEERCRED``, ``chown`` and AF_UNIX are all why this lives in the
container lane and not in ``tests/unit``:

    docker run --rm -v "$PWD:/w" -w /w e2b-sandlock-test:latest \
        sh -c 'python3 -m pytest tests/contract/test_broker_socket_c.py -q'
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "deploy" / "priv"
MAINT_C = (PRIV_DIR / "maint.c").read_text(encoding="utf-8")


def _c_integer_define(name: str) -> int:
    """The value of a plain-integer ``#define`` in ``maint.c``.

    The C side's numbers are the contract this lane pins (the refusal throttle
    and the walk ceiling); reading them out of the source keeps the assertions
    on the messages and the constants one edit apart instead of two.
    """
    match = re.search(rf"^#define {name} ([0-9]+)$", MAINT_C, re.MULTILINE)
    assert match is not None, f"maint.c has no plain-integer #define {name}"
    return int(match.group(1))


def _c_bytes_define(name: str) -> int:
    """The value of a byte ceiling spelt as a product, e.g.
    ``((unsigned long long)64 * 1024 * 1024)`` -- the shape every output cap in
    ``maint.c`` has, so the relations between them can be asserted rather than
    restated.
    """
    match = re.search(rf"^#define {name} \((.*)\)$", MAINT_C, re.MULTILINE)
    assert match is not None, f"maint.c has no #define {name} (...)"
    product = 1
    for factor in match.group(1).split("*"):
        product *= int(factor.strip().removeprefix("(unsigned long long)").strip())
    return product


#: The bounded wait ``refuse_connection`` may spend on a refused peer that has
#: still to say anything (``PRIV_REFUSAL_WAIT_MS``).
REFUSAL_WAIT_MS = _c_integer_define("PRIV_REFUSAL_WAIT_MS")

#: Where ``serve`` insists on running from: the path is compiled into the
#: broker (``PRIV_DEFAULT_MAINT_BIN``), precisely so that no setting can move
#: the trust. The test lane therefore installs the freshly built binary there.
INSTALLED_BROKER = Path("/var/lib/e2b-priv/e2b-maint")


def _require_disposable_container() -> None:
    """Refuse loudly unless this really is the lane's one-shot container.

    This module (and ``test_broker_socket_identity.py`` with it) overwrites the
    *image's* ``/var/lib/e2b-priv/e2b-maint`` -- in the identity lane with its
    capability xattr -- and puts the original back at teardown. That is only
    ever legitimate where the file belongs to a container the image built:
    on a Linux **root** development machine the same path is the host's real
    installation, so the overwrite would land in (and, after a hard kill
    between the two, stay in) the host's privileged broker. ``serve`` also
    refuses to run from any other path, so there is no "harmless" variant of
    this mistake to point the lane at instead.

    An explicit ``RuntimeError`` and not a skip: a silently skipped lane is
    indistinguishable from a passing one in a summary line, and the shape
    below (root, ``SO_PEERCRED``, ``chown``, AF_UNIX) is the only place these
    guarantees are exercised at all.
    """
    in_container = any(
        marker.exists()
        for marker in (Path("/.dockerenv"), Path("/run/.containerenv"))
    )
    if os.geteuid() != 0 or not in_container:
        raise RuntimeError(
            "this lane only runs inside the one-shot test container "
            "(e2b-sandlock-test:latest, as root, with /.dockerenv or "
            "/run/.containerenv present): it replaces the installed broker "
            f"{INSTALLED_BROKER} in place and restores it at teardown, which "
            "outside that container means writing the host's own "
            "installation"
        )


_require_disposable_container()

#: This module's own pool segment. The gate is a range membership test, so a
#: daemon started here must not overlap another lane's pool -- and the chown
#: target has to land inside *this* range.
POOL_START = 21000
POOL_SIZE = 16
POOL_END = POOL_START + POOL_SIZE - 1

#: The request-line cap frozen by the protocol (64 KiB).
MAX_REQUEST = 64 * 1024

#: Mirrors ``PRIV_MAX_HANDLERS`` in maint.c: over this many in-flight handlers
#: the daemon refuses the connection instead of forking for it.
MAX_HANDLERS = 32

#: A path that exists in the image and is outside every whitelist root.
OUTSIDE = "/etc/hosts"

#: The daemon's response writer is "escape in 4 KiB chunks, never splitting a
#: UTF-8 sequence". This harness drives those same two functions with that same
#: loop, so the shapes that cannot be produced through a verb (a payload ending
#: in an incomplete sequence -- `walk` output always ends in a newline) are
#: still covered.
ESCAPE_HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>

#include "priv_common.h"

struct boundary_case {
    const char *bytes;
    size_t len;
};

int main(int argc, char **argv) {
    static const struct boundary_case cases[] = {
        {"\xf0\x80", 2},      /* a lead byte whose sequence never completes */
        {"abc\xe0", 4},       /* ... at the end of a longer chunk */
        {"\xe0", 1},          /* the chunk *is* the lead byte */
        {"\xf0\x9f\x98", 3},  /* three bytes of a four-byte sequence */
        {"\x80\x80", 2},      /* nothing but continuation bytes */
        {"a", 1},
    };
    char *data = NULL;
    char *escaped = NULL;
    size_t cap = 0, len = 0, used = 0, index;
    FILE *file;

    if (argc != 2) {
        fprintf(stderr, "usage: %s FILE\n", argv[0]);
        return 2;
    }
    for (index = 0; index < sizeof(cases) / sizeof(cases[0]); index++) {
        if (priv_json_escape_boundary(cases[index].bytes, cases[index].len) == 0) {
            fprintf(stderr, "boundary case %zu made no progress\n", index);
            return 1;
        }
    }
    file = fopen(argv[1], "rb");
    if (file == NULL) {
        perror("fopen");
        return 2;
    }
    for (;;) {
        size_t got;
        while (len + 4097 > cap) {
            char *grown;
            cap = cap != 0 ? cap * 2 : 8192;
            grown = realloc(data, cap);
            if (grown == NULL) {
                return 2;
            }
            data = grown;
        }
        got = fread(data + len, 1, 4096, file);
        len += got;
        if (got < 4096) {
            break;
        }
    }
    fclose(file);
    escaped = malloc(6 * 4096 + 1);
    if (escaped == NULL) {
        return 2;
    }
    while (used < len) {
        size_t chunk = len - used > 4096 ? 4096 : len - used;
        chunk = priv_json_escape_boundary(data + used, chunk);
        if (chunk == 0) {
            fprintf(stderr, "the chunker stalled at byte %zu\n", used);
            return 1;
        }
        fwrite(escaped, 1, priv_json_escape(escaped, data + used, chunk), stdout);
        used += chunk;
    }
    return 0;
}
"""


def _peer_uid() -> int:
    """The peer the daemon must accept: this process.

    Read at call time rather than hard-coded: the container lane runs as root
    (``chown`` and ``SO_PEERCRED`` both need a real identity), and the daemon's
    gate is "the peer uid is exactly ``E2B_BROKER_PEER_UID``".
    """
    return os.geteuid()


def _peer_gid() -> int:
    return os.getegid()


def _workspace(scratch: Path) -> Path:
    return scratch / "sandboxes"


def _image_cache(scratch: Path) -> Path:
    return scratch / "images"


def _roots_text(scratch: Path) -> str:
    """The roots diagnostic the way the C broker spells it: ", "-joined.

    ``E2B_STATE_BASE`` is cleared for every daemon this module starts, so the
    workspace base *is* the state base and is named once. This is the shape
    with ``E2B_IMAGE_CACHE_DIR`` named, which is what every deployment does.
    """
    return f"{_workspace(scratch)}, {_image_cache(scratch)}"


def _broker_env(
    binary: Path, scratch: Path, **overrides: str | None
) -> dict[str, str]:
    """The daemon's environment: this module's pool, this process as the peer.

    ``E2B_STATE_BASE`` / ``E2B_SHARED_VOLUME_ROOT`` are cleared unless the
    caller names them (the ordering contract,
    ``test_hello_roots_follow_the_frozen_order``), so the shape this module
    starts by default is the workspace base plus the image cache. An override
    of ``None`` *removes* the variable, which is how "the deployment did not
    name one" is spelled.
    """
    _workspace(scratch).mkdir(parents=True, exist_ok=True)
    _image_cache(scratch).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in (
        "E2B_STATE_BASE",
        "E2B_SHARED_VOLUME_ROOT",
        "E2B_PRIV_HELPER_SOCKET",
        # Cleared on purpose: the compiled-in install path is the only thing
        # `serve` trusts, so this variable must have no effect at all.
        "E2B_MAINT_BIN",
    ):
        env.pop(name, None)
    env.update(
        {
            "E2B_UID_POOL_START": str(POOL_START),
            "E2B_UID_POOL_SIZE": str(POOL_SIZE),
            "E2B_BROKER_PEER_UID": str(_peer_uid()),
            "E2B_BROKER_PEER_GID": str(_peer_gid()),
            "E2B_WORKSPACE_BASE": str(_workspace(scratch)),
            "E2B_IMAGE_CACHE_DIR": str(_image_cache(scratch)),
        }
    )
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


class _NotReady(Exception):
    """The daemon is not accepting connections yet (retryable)."""


@dataclasses.dataclass
class _Serve:
    """A running ``e2b-maint serve`` and the socket it bound."""

    socket: Path
    health: Path | None
    process: subprocess.Popen
    _output: tuple[bytes, bytes] | None = None
    _log: str = ""

    def log(self) -> str:
        """What the daemon has logged so far -- without waiting for it to exit.

        The daemon's own account of a connection ("refused: ...", one line per
        connection, written *before* the answer goes to the socket) is the only
        evidence that survives the peer, so a test that hangs up on it has to
        read it while the process is still up. Non-blocking, and accumulated:
        ``stop`` returns the whole log rather than only whatever was left in
        the pipe.
        """
        self._log += _read_available(self.process.stderr)
        return self._log

    def stop(self) -> str:
        """Kill the daemon's whole process group and return what it logged."""
        if self._output is None:
            if self.process.poll() is None:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self._output = self.process.communicate(timeout=30)
        self._log += self._output[1].decode()
        return self._log


def _read_available(stream) -> str:
    """Everything readable on ``stream`` right now (never blocks)."""
    fd = stream.fileno()
    out = b""
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 65536)
        if not chunk:
            break  # EOF: the writer (or its pipe) is gone
        out += chunk
    return out.decode()


def _snapshot(path: Path) -> tuple[bytes, int, int, int, bytes | None] | None:
    """The file as the image shipped it -- xattr (the capability) included."""
    if not path.exists():
        return None
    info = path.stat()
    try:
        xattr = os.getxattr(path, "security.capability")
    except OSError:
        xattr = None
    return (
        path.read_bytes(),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        xattr,
    )


def _restore(
    path: Path, snapshot: tuple[bytes, int, int, int, bytes | None] | None
) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    data, mode, uid, gid, xattr = snapshot
    path.write_bytes(data)
    os.chmod(path, mode)
    os.chown(path, uid, gid)
    if xattr is not None:
        os.setxattr(path, "security.capability", xattr)


def _build_broker(binary: Path, *defines: str) -> None:
    """Build ``deploy/priv`` the way the image does, with optional -D overrides.

    ``-Wall -Wextra`` clean is the same bar the module fixture holds the
    shipped build to, so a test that compiles a second variant cannot smuggle a
    warning in with it.
    """
    build = subprocess.run(
        [
            "cc",
            "-O2",
            "-Wall",
            "-Wextra",
            *defines,
            "-o",
            str(binary),
            str(PRIV_DIR / "maint.c"),
            str(PRIV_DIR / "priv_common.c"),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    assert build.stderr == ""


@contextlib.contextmanager
def _installed_with_walk_cap(bytes_cap: int, tmp_path: Path):
    """Install the same source built with a tiny ``PRIV_MAX_WALK_OUTPUT`` (A7).

    Every ceiling in the daemon is a compile-time constant, and crossing the
    shipped 64 MiB one would take a tree of ~1M entries -- so the crossing is
    driven by the same source built with a smaller cap. The module fixture's
    binary is put back afterwards; the rest of the lane keeps running the
    shipped constant, whose relations are asserted separately.
    """
    built = tmp_path / "e2b-maint-small-walk-cap"
    _build_broker(built, f"-DPRIV_MAX_WALK_OUTPUT={bytes_cap}")
    previous = _snapshot(INSTALLED_BROKER)
    shutil.copyfile(built, INSTALLED_BROKER)
    os.chmod(INSTALLED_BROKER, 0o750)
    os.chown(INSTALLED_BROKER, 0, os.getegid())
    try:
        yield INSTALLED_BROKER
    finally:
        _restore(INSTALLED_BROKER, previous)


@pytest.fixture(scope="module")
def broker_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``deploy/priv`` built the way the image builds it, and installed.

    The empty ``stderr`` is part of the assertion: ``-Wall -Wextra`` clean is
    the bar the other brokers are held to. The binary then goes to the
    canonical install path -- ``serve`` refuses to run from anywhere else, and
    that refusal is itself tested -- and the image's own copy (capability xattr
    included) is put back when this module is done.
    """
    out = tmp_path_factory.mktemp("priv-c1")
    built = out / "e2b-maint"
    build = subprocess.run(
        [
            "cc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-o",
            str(built),
            str(PRIV_DIR / "maint.c"),
            str(PRIV_DIR / "priv_common.c"),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    assert build.stderr == ""
    previous = _snapshot(INSTALLED_BROKER)
    INSTALLED_BROKER.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built, INSTALLED_BROKER)
    os.chmod(INSTALLED_BROKER, 0o750)
    os.chown(INSTALLED_BROKER, 0, os.getegid())
    try:
        yield INSTALLED_BROKER
    finally:
        _restore(INSTALLED_BROKER, previous)


@pytest.fixture()
def serve(broker_bin: Path, tmp_path: Path):
    """Start daemons on throwaway sockets; every one of them is reaped."""
    started: list[_Serve] = []

    def _start(
        name: str = "broker",
        *,
        pass_flag: bool = True,
        socket_path: Path | None = None,
        health_socket: Path | None = None,
        **overrides: str | None,
    ) -> _Serve:
        if socket_path is None:
            socket_path = tmp_path / f"{name}.sock"
        env = _broker_env(broker_bin, tmp_path, **overrides)
        argv = [str(broker_bin), "serve"]
        if pass_flag:
            argv += ["--socket", str(socket_path)]
        else:
            # No flag: the path has to come from the environment.
            env["E2B_PRIV_HELPER_SOCKET"] = str(socket_path)
        if health_socket is not None:
            argv += ["--health-socket", str(health_socket)]
        process = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        handle = _Serve(socket=socket_path, health=health_socket, process=process)
        started.append(handle)
        return handle

    yield _start
    for handle in started:
        handle.stop()


def _round_trip(socket_path: Path, payload: bytes) -> bytes:
    """One connection: send ``payload``, read the answer to EOF."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            client.connect(str(socket_path))
        except OSError as exc:
            # Not listening yet (the daemon may still be forking): retryable.
            raise _NotReady(str(exc)) from exc
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    return b"".join(chunks)


def _send(
    socket_path: Path,
    payload: bytes,
    *,
    process: subprocess.Popen | None = None,
    timeout: float = 15.0,
) -> bytes:
    """``_round_trip`` with a connect retry; other failures are real failures."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return _round_trip(socket_path, payload)
        except _NotReady:
            if process is not None and process.poll() is not None:
                pytest.fail(
                    f"the broker exited before answering (rc={process.returncode})"
                )
            if time.monotonic() >= deadline:
                pytest.fail(f"the broker socket {socket_path} never accepted")
            time.sleep(0.02)


def _response(raw: bytes) -> dict:
    """The response is exactly one line: the protocol has no other framing."""
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    return json.loads(raw.decode())


def _request(
    socket_path: Path,
    request: Any,
    *,
    process: subprocess.Popen | None = None,
) -> dict:
    payload = json.dumps(request).encode() + b"\n"
    return _response(_send(socket_path, payload, process=process))


def _await_listening(handle: _Serve) -> None:
    """Block until the daemon answers a hello.

    The C ``ping`` client has no retry of its own (a probe that retried would
    hide exactly the "the daemon is not up" failure it exists to report), so
    the harness waits with a real, side-effect-free request first.
    """
    _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)


def _handlers(handle: _Serve) -> list[int]:
    """The daemon's live handler processes.

    Linux reports a process's own children here, and one handler per accepted
    connection is the design -- so this is the observable that says whether the
    daemon forked for a connection at all.
    """
    children = Path(
        f"/proc/{handle.process.pid}/task/{handle.process.pid}/children"
    )
    return [int(pid) for pid in children.read_text().split()]


def _await_handlers(handle: _Serve, count: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_handlers(handle)) >= count:
            return
        time.sleep(0.01)
    pytest.fail(
        f"the broker never reached {count} handlers (saw {len(_handlers(handle))})"
    )


def _await_log(handle: _Serve, expected: str, timeout: float = 15.0) -> None:
    """Poll the daemon's own log until it is exactly ``expected``.

    The alternative -- a fixed sleep, then "the daemon is still up" -- leaves
    a reviewer to take two things on trust (that the writes landed inside the
    window, and that nothing was killed after it). Polling for the daemon's
    account of every connection makes it explicit: one "refused: ..." line per
    connection is written *before* its answer goes to the socket, so it exists
    regardless of what happened to that answer, and a daemon that died on
    SIGPIPE stops logging -- so a short log and a dead process both fail here,
    as soon as they happen.
    """
    deadline = time.monotonic() + timeout
    log = handle.log()
    while log != expected:
        if handle.process.poll() is not None:
            log = handle.log()
            if log != expected:
                pytest.fail(
                    f"the broker exited (rc={handle.process.returncode}) with "
                    f"{log!r} logged, expected {expected!r}"
                )
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"the broker logged {log!r}, expected {expected!r}")
        time.sleep(0.01)
        log = handle.log()


def _connect_as(uid: int, socket_path: Path) -> str:
    """``connect()`` once as `uid`, in a child process, and report the outcome.

    The socket's mode is the layer a pool uid has to be stopped by (before any
    peer check can run), so the test has to drop to that uid to see it.
    ``subprocess`` rather than ``os.fork()``: this suite runs in a process that
    other lanes may have made multi-threaded, where forking warns.
    """
    script = (
        "import os, socket, sys\n"
        "uid = int(sys.argv[1])\n"
        "os.setgroups([])\n"
        "os.setgid(uid)\n"
        "os.setuid(uid)\n"
        "client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "try:\n"
        "    client.connect(sys.argv[2])\n"
        "    print('connected')\n"
        "except OSError as exc:\n"
        "    print(f'error:{exc.errno}')\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script, str(uid), str(socket_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _hello_as(uid: int, socket_path: Path) -> bytes:
    """One ``hello`` as `uid`, returning the answer (or the connect failure).

    The business socket's gate is only ever tested from a mismatching *env*
    (this lane's clients are root, and the mode stops a pool uid first), but
    the health socket's gate is "uid 0" itself -- so the peer has to really be
    somebody else. Same child-process trick as ``_connect_as``: this suite may
    run in a multi-threaded process, where ``os.fork()`` warns.
    """
    script = (
        "import os, socket, sys\n"
        "uid = int(sys.argv[1])\n"
        "os.setgroups([])\n"
        "os.setgid(uid)\n"
        "os.setuid(uid)\n"
        "client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "client.settimeout(15)\n"
        "try:\n"
        "    client.connect(sys.argv[2])\n"
        "except OSError as exc:\n"
        "    print(f'connect-error:{exc.errno}')\n"
        "    raise SystemExit(0)\n"
        "client.sendall(b'{\"v\":1,\"hello\":true}\\n')\n"
        "client.shutdown(socket.SHUT_WR)\n"
        "answer = b''\n"
        "while not answer.endswith(b'\\n'):\n"
        "    chunk = client.recv(65536)\n"
        "    if not chunk:\n"
        "        break\n"
        "    answer += chunk\n"
        "sys.stdout.write(answer.decode())\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script, str(uid), str(socket_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.encode()


def _chown(path: Path, uid: int = POOL_START) -> dict:
    return {"v": 1, "args": ["chown", "--uid", str(uid), "--path", str(path)]}


def _freeze(pid: int) -> None:
    """SIGSTOP ``pid`` and wait until the kernel has it stopped.

    ``os.kill`` only queues the signal, so a test that wants "the bytes are in
    the socket *before* the daemon looks" has to wait for the stop to land --
    otherwise the daemon can accept and read while the client's ``send`` is
    still on its way, which is the race the caller is trying to remove.
    ``/proc/<pid>/stat`` is the observable: unlike a second ``waitpid`` it does
    not consume the stop notification the ``Popen`` object may want later.
    """
    os.kill(pid, signal.SIGSTOP)
    deadline = time.monotonic() + 15
    while True:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
        if state == "T":
            return
        assert time.monotonic() < deadline, f"pid {pid} never stopped (state {state})"
        time.sleep(0.005)


def _read_answer(client: socket.socket) -> bytes:
    """Read one response line (the protocol has no other framing)."""
    raw = b""
    while b"\n" not in raw:
        chunk = client.recv(65536)
        assert chunk, "the broker closed the connection without an answer"
        raw += chunk
    return raw


def test_serve_round_trips_a_chown(serve, tmp_path: Path) -> None:
    handle = serve()
    tree = _workspace(tmp_path) / "sb-1"
    (tree / "logs").mkdir(parents=True)
    (tree / "logs" / "run.log").write_text("x")

    response = _request(handle.socket, _chown(tree), process=handle.process)

    assert response == {"v": 1, "ok": True, "exit": 0, "stdout": "", "stderr": ""}
    # No --gid in the request: the maintenance broker defaults it to --uid.
    assert (os.stat(tree).st_uid, os.stat(tree).st_gid) == (POOL_START, POOL_START)


def test_serve_chowns_a_secret_tree_under_the_image_cache(
    serve, tmp_path: Path
) -> None:
    """The 4th root: ``<image_cache_dir>/secrets/<sandbox_id>/`` (C1's reason).

    Only when the deployment names ``E2B_IMAGE_CACHE_DIR`` -- see
    ``test_image_cache_root_is_absent_when_the_deployment_names_none`` for the
    other half of the rule.
    """
    handle = serve()
    secret = _image_cache(tmp_path) / "secrets" / "sb-1"
    secret.mkdir(parents=True)
    (secret / "token").write_text("s3cret")

    response = _request(handle.socket, _chown(secret), process=handle.process)

    assert response == {"v": 1, "ok": True, "exit": 0, "stdout": "", "stderr": ""}
    assert (os.stat(secret).st_uid, os.stat(secret).st_gid) == (POOL_START, POOL_START)


def test_image_cache_root_is_absent_when_the_deployment_names_none(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    """No default for the 4th root: unset means it is not a root at all.

    The Python side's unset value is cwd-relative, so inventing one here would
    whitelist a directory nobody means -- and would make the two sides' root
    lists disagree, which is what the hello handshake exists to refuse.
    """
    handle = serve("no-cache", E2B_IMAGE_CACHE_DIR=None)
    _await_listening(handle)

    ping = subprocess.run(
        [str(broker_bin), "ping", "--socket", str(handle.socket)],
        env=_broker_env(broker_bin, tmp_path, E2B_IMAGE_CACHE_DIR=None),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert ping.returncode == 0
    # E2B_STATE_BASE and E2B_SHARED_VOLUME_ROOT are unset too: the only root
    # left is the workspace base.
    assert ping.stdout == (
        json.dumps(
            {
                "v": 1,
                "ok": True,
                "peer_uid": _peer_uid(),
                "peer_gid": _peer_gid(),
                "uid_pool": [POOL_START, POOL_SIZE],
                "roots": [str(_workspace(tmp_path))],
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    # And the absence is real: the secret tree is refused, by name.
    secret = _image_cache(tmp_path) / "secrets" / "sb-1"
    secret.mkdir(parents=True)
    assert _request(handle.socket, _chown(secret), process=handle.process) == {
        "v": 1,
        "ok": True,
        "exit": 77,
        "stdout": "",
        "stderr": (
            f"e2b-maint: refused: path {secret} is outside the privileged "
            f"helper roots ({_workspace(tmp_path)})\n"
        ),
    }


def test_hello_roots_follow_the_frozen_order(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    """The four roots, in the order the Python side builds them.

    Task 2's transport compares these against its own list, so the order and
    the conditional entries are the interface -- not a diagnostic.
    """
    state = tmp_path / "state"
    shared = tmp_path / "shared"
    state.mkdir()
    shared.mkdir()

    handle = serve(
        "ordered",
        E2B_STATE_BASE=str(state),
        E2B_SHARED_VOLUME_ROOT=str(shared),
    )
    _await_listening(handle)
    ping = subprocess.run(
        [str(broker_bin), "ping", "--socket", str(handle.socket)],
        env=_broker_env(broker_bin, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ping.returncode == 0
    assert json.loads(ping.stdout)["roots"] == [
        str(_workspace(tmp_path)),
        str(state),
        str(shared),
        str(_image_cache(tmp_path)),
    ]

    # A state base that *is* the workspace base is not named twice: one
    # directory, one root (the Python side's own rule).
    handle = serve(
        "state-is-workspace",
        E2B_STATE_BASE=str(_workspace(tmp_path)),
        E2B_SHARED_VOLUME_ROOT=str(shared),
    )
    _await_listening(handle)
    ping = subprocess.run(
        [str(broker_bin), "ping", "--socket", str(handle.socket)],
        env=_broker_env(broker_bin, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ping.returncode == 0
    assert json.loads(ping.stdout)["roots"] == [
        str(_workspace(tmp_path)),
        str(shared),
        str(_image_cache(tmp_path)),
    ]


def test_serve_returns_the_child_stdout(serve, tmp_path: Path) -> None:
    """``walk`` is the shape the byte ledger eats: argv through, stdout back."""
    handle = serve()
    tree = _workspace(tmp_path) / "sb-1"
    (tree / "logs").mkdir(parents=True)
    (tree / "logs" / "run.log").write_text("x")

    response = _request(
        handle.socket, {"v": 1, "args": ["walk", "--path", str(tree)]},
        process=handle.process,
    )

    # "<kind> <uid> <gid> <mode-octal> <size> <path>", a directory's own
    # allocation (st_blocks x 512) -- the number `du` reports.
    blocks = f"d {_peer_uid()} {_peer_gid()} 755 {{}} {{}}"
    assert response == {
        "v": 1,
        "ok": True,
        "exit": 0,
        "stdout": (
            blocks.format(os.stat(tree).st_blocks * 512, tree)
            + "\n"
            + blocks.format(os.stat(tree / "logs").st_blocks * 512, tree / "logs")
            + "\n"
            + f"f {_peer_uid()} {_peer_gid()} 644 1 {tree / 'logs' / 'run.log'}\n"
        ),
        "stderr": "",
    }


def test_serve_refuses_a_path_outside_the_roots(serve, tmp_path: Path) -> None:
    handle = serve()

    response = _request(handle.socket, _chown(Path(OUTSIDE)), process=handle.process)

    # The request *ran* (the child refused it), so it is a result, not ok:false.
    assert response == {
        "v": 1,
        "ok": True,
        "exit": 77,
        "stdout": "",
        "stderr": (
            f"e2b-maint: refused: path {OUTSIDE} is outside the privileged "
            f"helper roots ({_roots_text(tmp_path)})\n"
        ),
    }


def test_serve_rejects_a_non_pool_uid(serve, tmp_path: Path) -> None:
    handle = serve()
    tree = _workspace(tmp_path) / "sb-1"
    tree.mkdir()

    # uid 0 never reaches the pool gate: it is refused as a *shape* first.
    zero = _request(handle.socket, _chown(tree, uid=0), process=handle.process)
    assert zero == {
        "v": 1,
        "ok": True,
        "exit": 2,
        "stdout": "",
        "stderr": "e2b-maint: usage: --uid: uid/gid must be positive (got 0)\n",
    }

    # Any other uid outside this module's segment is named as out of range.
    stray = POOL_END + 1
    response = _request(handle.socket, _chown(tree, uid=stray), process=handle.process)
    assert response == {
        "v": 1,
        "ok": True,
        "exit": 77,
        "stdout": "",
        "stderr": (
            f"e2b-maint: refused: uid {stray} is outside the privileged helper "
            f"uid pool {POOL_START}..{POOL_END}\n"
        ),
    }
    # Refused means nothing was handed over.
    assert os.stat(tree).st_uid == _peer_uid()


def test_serve_rejects_unknown_verb(serve, tmp_path: Path) -> None:
    handle = serve()
    pwned = tmp_path / "pwned"

    response = _request(
        handle.socket,
        {"v": 1, "args": ["sh", "-c", f"touch {pwned}"]},
        process=handle.process,
    )

    assert response == {
        "v": 1,
        "ok": False,
        "error": (
            "args[0] 'sh' is not one of the privileged helper verbs "
            "(chown, rm, walk)"
        ),
    }
    # Not a launcher: nothing ran, and the daemon named the refusal.
    assert not pwned.exists()
    assert handle.stop() == (
        f"e2b-maint: serving on {handle.socket}\n"
        "e2b-maint: refused: args[0] 'sh' is not one of the privileged helper "
        "verbs (chown, rm, walk)\n"
    )


def test_serve_refuses_a_request_line_over_the_limit(serve, tmp_path: Path) -> None:
    handle = serve()

    raw = _send(
        handle.socket,
        b"x" * (MAX_REQUEST + 1) + b"\n",
        process=handle.process,
    )

    assert raw == (
        b'{"v":1,"ok":false,"error":'
        b'"the request line is longer than the 65536-byte limit"}\n'
    )


def test_ping_answers_hello_with_pool_and_roots(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    handle = serve()
    _await_listening(handle)

    ping = subprocess.run(
        [str(broker_bin), "ping", "--socket", str(handle.socket)],
        env=_broker_env(broker_bin, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert ping.returncode == 0
    assert ping.stderr == ""
    # `ping` prints the hello response verbatim: the same one-line JSON the
    # Python transport (Task 2) parses, in the frozen key order. The roots are
    # the whole list in the frozen order too -- the image cache named by the
    # deployment comes last.
    assert ping.stdout == (
        json.dumps(
            {
                "v": 1,
                "ok": True,
                "peer_uid": _peer_uid(),
                "peer_gid": _peer_gid(),
                "uid_pool": [POOL_START, POOL_SIZE],
                "roots": [str(_workspace(tmp_path)), str(_image_cache(tmp_path))],
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def test_socket_path_comes_from_the_environment(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    handle = serve("env", pass_flag=False)
    _await_listening(handle)

    ping = subprocess.run(
        [str(broker_bin), "ping"],
        env=_broker_env(
            broker_bin, tmp_path, E2B_PRIV_HELPER_SOCKET=str(handle.socket)
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert ping.returncode == 0
    assert _response(ping.stdout.encode())["ok"] is True


def test_hello_rejects_a_peer_uid_mismatch(serve) -> None:
    stranger = _peer_uid() + 1
    handle = serve("peer-uid", E2B_BROKER_PEER_UID=str(stranger))
    refusal = f"peer uid {_peer_uid()} does not match E2B_BROKER_PEER_UID={stranger}"
    assert _handlers(handle) == []

    response = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)

    assert response == {"v": 1, "ok": False, "error": refusal}
    # The refusal was decided *before* the fork: an unauthorized local uid must
    # not be able to make the root daemon spawn anything, and must not be able
    # to stall it either.
    assert _handlers(handle) == []
    assert handle.process.poll() is None
    assert handle.stop() == (
        f"e2b-maint: serving on {handle.socket}\n"
        f"e2b-maint: refused: {refusal}\n"
    )


def test_hello_rejects_a_peer_gid_mismatch(serve) -> None:
    stranger = _peer_gid() + 1
    handle = serve("peer-gid", E2B_BROKER_PEER_GID=str(stranger))
    refusal = f"peer gid {_peer_gid()} does not match E2B_BROKER_PEER_GID={stranger}"

    response = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)

    assert response == {"v": 1, "ok": False, "error": refusal}
    assert _handlers(handle) == []
    assert handle.process.poll() is None
    assert handle.stop() == (
        f"e2b-maint: serving on {handle.socket}\n"
        f"e2b-maint: refused: {refusal}\n"
    )


#: How many hung-up peers the regression sends at one daemon. One would do
#: (measured: the daemon's very first write to a dead socket is the one that
#: matters); 25 keeps the burst the wave-1 report described while staying far
#: below PRIV_MAX_HANDLERS (32), so the handlers' own exit is what frees them.
HUNG_UP_CONNECTIONS = 25


def test_a_peer_that_hangs_up_cannot_take_the_broker_down(serve) -> None:
    """``connect()`` then ``close()`` at once, against both serving paths.

    The refusal for a peer the daemon does not accept is written by the daemon
    itself (no handler is forked for it), so that write lands in a socket whose
    other end is already gone. With SIGPIPE at its default disposition the
    *daemon* died there -- killing the node's broker, and the trigger is the
    worker's own transport cancelling a request. ``Popen`` restores default
    signal dispositions, so this daemon really does start with SIGPIPE at its
    default: this is the regression, not a mocked one.

    The evidence is the daemon's own log instead of a sleep: every hung-up
    connection leaves exactly one refusal line *before* its answer is written
    (the accepted path refuses an empty request, the gated one refuses the
    peer), so the burst is waited out by polling for those lines. A daemon
    that took SIGPIPE on a dead write stops at one line and exits -- which is
    what makes "red" here a certainty rather than a race inside a fixed
    timing window.
    """
    stranger = _peer_uid() + 1
    refusal = f"peer uid {_peer_uid()} does not match E2B_BROKER_PEER_UID={stranger}"
    refused = serve("hangup-refused", E2B_BROKER_PEER_UID=str(stranger))
    served = serve("hangup-served")
    # Readiness by each daemon's own "serving on" line: a request would answer
    # here (a refusal *is* an answer) and add a line to the count below.
    for handle in (refused, served):
        _await_log(handle, f"e2b-maint: serving on {handle.socket}\n")

    for handle, refusal_line in (
        (refused, f"e2b-maint: refused: {refusal}\n"),
        (served, "e2b-maint: refused: empty request\n"),
    ):
        for _ in range(HUNG_UP_CONNECTIONS):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(handle.socket))
            client.close()  # no read, no shutdown: just gone
            # Give the daemon room to accept this one before the next: what
            # this pins is the write to a peer that is *already* gone, and a
            # connection the daemon never accepted would prove nothing.
            time.sleep(0.01)
        _await_log(
            handle,
            f"e2b-maint: serving on {handle.socket}\n"
            + refusal_line * HUNG_UP_CONNECTIONS,
        )
        assert handle.process.poll() is None

    # And both are still serving.
    refused_reply = _request(
        refused.socket, {"v": 1, "hello": True}, process=refused.process
    )
    assert refused_reply == {
        "v": 1,
        "ok": False,
        "error": (
            f"peer uid {_peer_uid()} does not match E2B_BROKER_PEER_UID={stranger}"
        ),
    }
    assert _request(served.socket, {"v": 1, "hello": True}, process=served.process)[
        "ok"
    ] is True


def test_the_socket_is_reachable_only_by_its_peer(serve) -> None:
    """``0660 root:<peer gid>``: a pool uid cannot even connect().

    SO_PEERCRED is still the real gate, but this is the layer that keeps an
    unauthorized uid out of the daemon's accept loop (and out of fork()) in the
    first place.

    The socket goes in a world-traversable directory on purpose: pytest's
    ``tmp_path`` is ``0700``, where a pool uid would be refused by the
    directory and the mode of the socket itself would never be exercised.
    """
    directory = Path(tempfile.mkdtemp(prefix="e2b-broker-socket-"))
    os.chmod(directory, 0o755)
    try:
        handle = serve("mode", socket_path=directory / "broker.sock")
        _await_listening(handle)

        info = os.stat(handle.socket)
        assert (info.st_uid, info.st_gid) == (0, _peer_gid())
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert _connect_as(POOL_START, handle.socket) == f"error:{errno.EACCES}"
        assert _connect_as(_peer_uid(), handle.socket) == "connected"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_health_socket_is_root_only_and_answers_the_probe_hello(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    """A4: the probe's socket -- root-only, hello-only, no peer gate at all.

    The DaemonSet's liveness/readiness probes run as the container's own root,
    and the *business* socket's gate is about the worker
    (``E2B_BROKER_PEER_UID``): a root probe is refused there by name (measured
    on the k0s cluster, 2026-09-27, and pinned below). That refusal is what
    used to drag ``setpriv`` into the probe and SETUID/SETGID into the broker's
    capability set. The health socket removes the need: it is ``0660
    root:root``, it answers ``{"v":1,"hello":true}`` to root without consulting
    the peer identity, and it must never become a second business socket --
    every other request is refused by name. The business socket itself is
    unchanged: same mode, same peer group, same gate in the parent.
    """
    stranger = _peer_uid() + 1
    health = tmp_path / "probe-health.sock"
    handle = serve(
        "health-probe",
        health_socket=health,
        E2B_BROKER_PEER_UID=str(stranger),
    )
    _await_log(
        handle,
        f"e2b-maint: serving on {handle.socket}\n"
        f"e2b-maint: health on {health}\n",
    )

    # Two sockets, two contracts: the health one is root's, the business one
    # stays the worker's.
    health_info = os.stat(health)
    assert (health_info.st_uid, health_info.st_gid) == (0, 0)
    assert stat.S_IMODE(health_info.st_mode) == 0o660
    business_info = os.stat(handle.socket)
    assert (business_info.st_uid, business_info.st_gid) == (0, _peer_gid())
    assert stat.S_IMODE(business_info.st_mode) == 0o660

    # The premise of the whole item: root is refused on the business socket...
    refusal = f"peer uid {_peer_uid()} does not match E2B_BROKER_PEER_UID={stranger}"
    assert _request(handle.socket, {"v": 1, "hello": True}, process=handle.process) == {
        "v": 1,
        "ok": False,
        "error": refusal,
    }
    # ...by the daemon, before it forks for it: that gate is untouched (the
    # health socket forks for its own clients, so this has to be checked before
    # any of them connect).
    assert _handlers(handle) == []

    # ...and answered on the health socket, verbatim, by the same client the
    # manifest runs (`ping`, no setpriv, this process's own root identity).
    assert _request(health, {"v": 1, "hello": True}, process=handle.process) == {
        "v": 1,
        "ok": True,
        "peer_uid": _peer_uid(),
        "peer_gid": _peer_gid(),
        "uid_pool": [POOL_START, POOL_SIZE],
        "roots": [str(_workspace(tmp_path)), str(_image_cache(tmp_path))],
    }
    ping = subprocess.run(
        [str(broker_bin), "ping", "--socket", str(health)],
        env=_broker_env(broker_bin, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ping.returncode == 0
    assert ping.stderr == ""
    assert json.loads(ping.stdout)["ok"] is True

    # Not a second business socket: a verb is refused by name, and so is a
    # "hello" that carries arguments.
    health_only = 'the health socket answers only {"v":1,"hello":true}'
    assert _request(
        health, {"v": 1, "args": ["walk", "--path", str(_workspace(tmp_path))]}
    ) == {"v": 1, "ok": False, "error": health_only}
    assert _request(
        health, {"v": 1, "hello": True, "args": ["walk", "--path", "/"]}
    ) == {"v": 1, "ok": False, "error": health_only}

    # Nothing above took the daemon down, and the health socket still answers.
    assert _request(health, {"v": 1, "hello": True})["ok"] is True
    assert handle.process.poll() is None
    assert handle.stop().endswith(
        f"e2b-maint: refused: {refusal}\n"
        f"e2b-maint: refused: {health_only}\n"
        f"e2b-maint: refused: {health_only}\n"
    )


def test_the_health_socket_gate_refuses_a_non_root_peer_by_name(serve) -> None:
    """The other half of A4: fail closed, and say who was let down.

    In the DaemonSet the health socket is unreachable for anyone but root (the
    file is ``0660 root:root`` and the worker's uid/gid is 65534), which is why
    this is defence in depth rather than the first gate -- so the test *relaxes
    the mode* to prove the gate itself holds if the mode ever drifts: a pool
    uid that can connect is refused by a message that names its uid, and the
    daemon keeps serving.
    """
    # pytest's tmp_path is 0700: without this the pool uid would be stopped by
    # the directory and the gate would never be reached.
    directory = Path(tempfile.mkdtemp(prefix="e2b-broker-health-"))
    os.chmod(directory, 0o755)
    try:
        health = directory / "health.sock"
        handle = serve("health-gate", health_socket=health)
        _await_log(
            handle,
            f"e2b-maint: serving on {handle.socket}\n"
            f"e2b-maint: health on {health}\n",
        )

        # 0660 root:root is the first gate: a pool uid cannot even connect.
        refused = f"connect-error:{errno.EACCES}\n".encode()
        assert _hello_as(POOL_START, health) == refused

        # Now the gate itself, with the mode out of the way.
        os.chmod(health, 0o666)
        assert _response(_hello_as(POOL_START, health)) == {
            "v": 1,
            "ok": False,
            "error": (
                f"peer uid {POOL_START} is not root: the health socket is for "
                "this container's own probe"
            ),
        }
        assert handle.process.poll() is None
        assert _request(health, {"v": 1, "hello": True})["ok"] is True
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_serve_refuses_a_health_socket_that_is_the_broker_socket(
    serve, tmp_path: Path
) -> None:
    """Two listeners on one path would silently pick one contract over the other."""
    both = tmp_path / "both.sock"
    handle = serve("health-clash", socket_path=both, health_socket=both)
    assert handle.process.wait(timeout=30) == 77
    assert handle.stop() == (
        "e2b-maint: refused: the health socket and the broker socket are the "
        f"same path: {both}\n"
    )


def test_serve_refuses_connections_over_the_handler_cap(serve, tmp_path: Path) -> None:
    """A trusted peer's leak must not turn into unlimited forked root processes."""
    handle = serve()
    _await_listening(handle)
    idle = [
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(MAX_HANDLERS)
    ]
    try:
        # Nothing is sent on these: a handler blocks reading its request, which
        # is exactly how the cap gets reached.
        for client in idle:
            client.connect(str(handle.socket))
        _await_handlers(handle, MAX_HANDLERS)

        extra = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)

        assert extra == {
            "v": 1,
            "ok": False,
            "error": f"the broker is already serving {MAX_HANDLERS} requests",
        }
        # Refused means refused *without* a handler, and the daemon lives.
        assert len(_handlers(handle)) == MAX_HANDLERS
        assert handle.process.poll() is None
    finally:
        for client in idle:
            client.close()

    # Once the connections go away the daemon serves again (its handlers exit
    # on EOF and the daemon reaps them before the next accept).
    deadline = time.monotonic() + 15
    while True:
        final = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)
        if final["ok"] is True:
            break
        assert time.monotonic() < deadline, final
        time.sleep(0.02)


def test_a_refusal_only_waits_for_a_peer_that_has_not_spoken_yet(serve) -> None:
    """A2: the refusal throttle is spent on "nothing yet", never on "here already".

    ``refuse_connection`` has to drain what the refused peer already sent --
    closing a socket that still holds unread input is what turns the written
    answer into an RST -- and that drain is time the *accept loop* pays, one
    refused connection at a time. So the first look at the connection is a
    **non-blocking read**: a request that is already in the socket (the
    ordinary refusal: the peer spoke before the answer was written) or the EOF
    of a peer that hung up is drained and closed *now*, with no timer
    involved; only a peer that has said nothing at all falls back to the
    bounded wait.

    Wall time alone cannot pin that: a refusal whose input is here costs
    microseconds either way, so the distinction is the *branch*, which the
    daemon reports when ``E2B_BROKER_REFUSAL_TRACE`` is set -- and the accept
    loop's cost of the whole exchange is what the second half measures. Three
    assertions, one property each:

    * two refusals whose requests were already written (both connections are
      filled while the daemon is SIGSTOPped, so "already in the socket" is a
      fact and not a race against the client) are answered with exactly one
      ``had already spoken`` trace each -- restoring the old ``poll`` first
      reports the wait branch instead, which is the red half of "no fixed
      50 ms";
    * the one from the *second* connection arrives promptly after the daemon is
      allowed to run again: an "wait anyway" implementation would answer it
      a whole ``PRIV_REFUSAL_WAIT_MS`` late, which is the cost this removes
      from the accept loop;
    * a peer that stays connected and says nothing *does* take the bounded wait
      (the positive control: removing the fallback would stop the answer from
      surviving its own close, and the trace line disappears with it).
    """
    stranger = _peer_uid() + 1
    refusal = f"peer uid {_peer_uid()} does not match E2B_BROKER_PEER_UID={stranger}"
    payload = json.dumps({"v": 1, "hello": True}).encode() + b"\n"
    spoken_line = (
        "e2b-maint: refusal trace: the peer had already spoken: "
        f"{len(payload)} bytes drained without waiting\n"
    )

    spoken = serve(
        "refuse-spoken",
        E2B_BROKER_PEER_UID=str(stranger),
        E2B_BROKER_REFUSAL_TRACE="1",
    )
    _await_log(spoken, f"e2b-maint: serving on {spoken.socket}\n")
    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for client in (first, second):
        client.settimeout(15.0)
    try:
        _freeze(spoken.process.pid)
        for client in (first, second):
            client.connect(str(spoken.socket))
            client.sendall(payload)
        started = time.monotonic()
        os.kill(spoken.process.pid, signal.SIGCONT)
        assert _response(_read_answer(first)) == {
            "v": 1,
            "ok": False,
            "error": refusal,
        }
        assert _response(_read_answer(second)) == {
            "v": 1,
            "ok": False,
            "error": refusal,
        }
        # The first refusal's drain was paid for by the second: both were in
        # the socket before the daemon ran, so answering the second one is
        # where a fixed wait would show up as accept-loop latency.
        elapsed = time.monotonic() - started
    finally:
        first.close()
        second.close()

    # Half the throttle: far above the microseconds the drain costs, far below
    # the extra 50 ms a refusal that waits anyway would add to this pair.
    assert elapsed < (REFUSAL_WAIT_MS / 2) / 1000, (
        f"two refusals whose requests were already in the socket took "
        f"{elapsed * 1000:.1f} ms; the accept loop is paying the "
        f"{REFUSAL_WAIT_MS} ms throttle on a connection that did not need it"
    )
    assert spoken.stop() == (
        f"e2b-maint: serving on {spoken.socket}\n"
        f"e2b-maint: refused: {refusal}\n" + spoken_line
        + f"e2b-maint: refused: {refusal}\n" + spoken_line
    )

    silent = serve(
        "refuse-silent",
        E2B_BROKER_PEER_UID=str(stranger),
        E2B_BROKER_REFUSAL_TRACE="1",
    )
    _await_log(silent, f"e2b-maint: serving on {silent.socket}\n")
    quiet = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    quiet.settimeout(15.0)
    try:
        quiet.connect(str(silent.socket))
        assert _response(_read_answer(quiet)) == {
            "v": 1,
            "ok": False,
            "error": refusal,
        }
        # The positive control: this peer really does take the bounded wait,
        # and the daemon says so (the line is written before the wait, so it
        # exists even though the answer itself did not need it).
        _await_log(
            silent,
            f"e2b-maint: serving on {silent.socket}\n"
            f"e2b-maint: refused: {refusal}\n"
            "e2b-maint: refusal trace: the peer has not spoken yet: waiting up "
            f"to {REFUSAL_WAIT_MS} ms for its request\n",
        )
    finally:
        quiet.close()


def test_a_silent_peer_is_refused_and_the_broker_keeps_serving(serve) -> None:
    """A connection that says nothing is answered ``ok:false`` and closed.

    The peer gate runs before the fork, so a peer that reaches a handler is an
    authenticated one -- and an authenticated peer whose transport wedges is
    exactly the shape that used to park a handler for as long as it liked: the
    cap (PRIV_MAX_HANDLERS) then turns 32 silent connections into a broker that
    accepts every worker on the node and serves none of them, without a single
    failure to look at. Reading the request therefore has a deadline
    (``E2B_BROKER_REQUEST_READ_MS``, an operator's knob -- see
    PRIV_DEFAULT_REQUEST_READ_MS for why the node's broker owns that value).

    The test names a small deadline on purpose: the default is 30 s of
    protection, and spending it here would buy nothing. Both halves matter --
    the connection is refused inside the deadline *and* with a named error,
    and the daemon is still there afterwards, serving the next caller.
    """
    handle = serve("read-deadline", E2B_BROKER_REQUEST_READ_MS="400")
    _await_listening(handle)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # The refusal has to *arrive*, not merely be decided; without a timeout a
    # broker that never answers would hang this lane instead of failing it.
    client.settimeout(10.0)
    try:
        client.connect(str(handle.socket))
        started = time.monotonic()
        raw = b""
        while b"\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
        elapsed = time.monotonic() - started
    finally:
        client.close()

    assert _response(raw) == {
        "v": 1,
        "ok": False,
        "error": "the request was not sent within 400 ms",
    }
    # Not before the deadline (a refusal that never waited would be a
    # different path), and not long after it.
    assert 0.3 <= elapsed <= 3.0
    # Fail closed, never self-harm: the refusal cost this one handler.
    assert handle.process.poll() is None
    assert (
        _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)[
            "ok"
        ]
        is True
    )


def test_serve_refuses_an_unusable_request_read_deadline(serve) -> None:
    """The knob is a startup gate, like the peer identity and the uid pool.

    A value the daemon cannot read must stop it from serving: the alternative
    is a broker that answers every request with a message its caller cannot
    act on (and, with a deadline of 0, one that never reads a request at all).
    """
    handle = serve("bad-read-ms", E2B_BROKER_REQUEST_READ_MS="0")

    assert handle.process.wait(timeout=30) == 77
    assert handle.stop() == (
        "e2b-maint: refused: invalid request read deadline: "
        "E2B_BROKER_REQUEST_READ_MS must be a positive number of milliseconds "
        "(got '0')\n"
    )


def test_walk_escapes_a_non_utf8_name_like_python_does(
    serve, tmp_path: Path
) -> None:
    """One undecodable byte must not cost the caller the whole answer.

    A Linux filename may be any byte but NUL and '/'. The response is JSON, so
    that byte comes back as Python's surrogateescape spelling (``\\udc80`` for
    ``\\x80``) -- the decoded path then equals ``os.fsdecode()`` of the same
    name, i.e. it still matches what ``os.walk`` reported.
    """
    handle = serve()
    tree = _workspace(tmp_path) / "sb-1"
    tree.mkdir()
    raw = os.fsencode(tree) + b"/bad\x80name"
    weird = os.fsdecode(raw)
    with open(weird, "wb") as sink:
        sink.write(b"x")

    response = _request(
        handle.socket,
        {"v": 1, "args": ["walk", "--path", str(tree)]},
        process=handle.process,
    )

    assert response == {
        "v": 1,
        "ok": True,
        "exit": 0,
        "stdout": (
            f"d {_peer_uid()} {_peer_gid()} 755 "
            f"{os.stat(tree).st_blocks * 512} {tree}\n"
            f"f {_peer_uid()} {_peer_gid()} 644 1 {os.fsdecode(raw)}\n"
        ),
        "stderr": "",
    }
    assert response["stdout"].splitlines()[1].split(" ", 5)[5] == os.fsdecode(raw)

    # And the request direction: that same name has to be *usable*, or a tree
    # with such a file could never be torn down or handed over.
    assert _request(handle.socket, _chown(Path(weird)), process=handle.process) == {
        "v": 1,
        "ok": True,
        "exit": 0,
        "stdout": "",
        "stderr": "",
    }
    assert os.stat(weird).st_uid == POOL_START

    # A name that ends in an *incomplete* sequence is the same story, and it is
    # the shape the chunker has to keep its hands off: each of its bytes is
    # escaped on its own, exactly as `os.fsdecode()` decoded them.
    other = _workspace(tmp_path) / "sb-2"
    other.mkdir()
    raw2 = os.fsencode(other) + b"/a\xf0\x9f\x98b"
    weird2 = os.fsdecode(raw2)
    with open(weird2, "wb") as sink:
        sink.write(b"x")

    assert _request(
        handle.socket,
        {"v": 1, "args": ["walk", "--path", str(other)]},
        process=handle.process,
    ) == {
        "v": 1,
        "ok": True,
        "exit": 0,
        "stdout": (
            f"d {_peer_uid()} {_peer_gid()} 755 "
            f"{os.stat(other).st_blocks * 512} {other}\n"
            f"f {_peer_uid()} {_peer_gid()} 644 1 {os.fsdecode(raw2)}\n"
        ),
        "stderr": "",
    }


def test_the_streaming_escaper_never_stalls(tmp_path: Path) -> None:
    """The 4 KiB chunker must always make progress.

    A payload whose end is an incomplete UTF-8 sequence used to hand the writer
    a 0-byte step: the handler then spun forever and the caller never got an
    answer. The C layer is where this can be driven directly -- a `walk`
    response always ends in a newline, so the daemon path cannot reach the
    shape -- and the harness calls the same two functions the writer calls, in
    the same 4 KiB loop. What the harness has to produce is checked as the
    consumer's round trip (`json.loads` of the escaped stream is
    `os.fsdecode` of the payload), for an illegal tail *and* for a legal
    character straddling the chunk boundary: the two shapes the writer cannot
    be allowed to treat alike.
    """
    source = tmp_path / "escape_harness.c"
    source.write_text(ESCAPE_HARNESS)
    harness = tmp_path / "escape-harness"
    build = subprocess.run(
        [
            "cc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-I",
            str(PRIV_DIR),
            "-o",
            str(harness),
            str(source),
            str(PRIV_DIR / "priv_common.c"),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    assert build.stderr == ""

    cases = {
        # 4096 bytes whose last byte is a lead byte: the final chunk is exactly
        # the incomplete sequence.
        "incomplete-tail": b"a" * 4095 + b"\xf0",
        # ...and one whose 4 KiB boundary falls *inside* a legal two-byte
        # sequence, so the chunker has to back off rather than split it.
        "boundary-inside-a-character": b"a" * 4095 + "\u00e9".encode("utf-8"),
    }
    data = tmp_path / "payload.bin"
    for name, payload in cases.items():
        data.write_bytes(payload)
        run = subprocess.run(
            [str(harness), str(data)], capture_output=True, text=True, timeout=30
        )

        assert run.returncode == 0, (name, run.stderr)
        # The invariant is the round trip, not one spelling of one payload:
        # whatever the harness emits, the caller's `json.loads` has to hand
        # back exactly what `os.fsdecode` made of those bytes. Comparing the
        # two spellings byte for byte only ever held for payloads that were
        # pure ASCII plus isolated illegal bytes -- `json.dumps` writes a legal
        # non-ASCII character as `\uXXXX` while the writer emits its UTF-8
        # bytes -- which made the assertion narrower than the contract it was
        # standing in for.
        assert json.loads('"' + run.stdout + '"') == os.fsdecode(payload), name


def test_serve_refuses_to_run_from_a_copy(broker_bin: Path, tmp_path: Path) -> None:
    """The installed path is compiled in: a copy must not serve."""
    copy = tmp_path / "copy" / "e2b-maint"
    copy.parent.mkdir()
    shutil.copyfile(broker_bin, copy)
    os.chmod(copy, 0o750)

    run = subprocess.run(
        [str(copy), "serve", "--socket", str(tmp_path / "copy.sock")],
        env=_broker_env(broker_bin, tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 77
    assert run.stdout == ""
    assert run.stderr == (
        f"e2b-maint: refused: this broker must run from the installed path "
        f"{INSTALLED_BROKER}, but the running image is {os.path.realpath(copy)}: "
        "not serving from a copy\n"
    )


def test_serve_refuses_a_non_positive_timeout(serve, tmp_path: Path) -> None:
    """``timeout_s`` is a positive budget and nothing runs for ``<= 0``.

    The daemon checks the budget *before* it forks the grandchild, so this is
    the cheap half of the timeout contract and the refusal the worker would
    otherwise read as a completed step.

    The other half -- a request that outlives ``timeout_s`` and is SIGKILLed,
    with ``ok:false`` -- is
    ``test_a_walk_that_outlives_the_budget_is_killed_and_the_daemon_survives``
    below: it needs a tree large enough that the walk *reliably* outlives a
    1 s budget, so it pays ~20 s of setup and is kept separate from this
    cheap refusal.
    """
    handle = serve("timeout")
    _await_listening(handle)
    tree = tmp_path / "sandboxes" / "sbx_timeout"
    tree.mkdir(parents=True)
    for budget in (0, -5):
        response = _request(
            handle.socket,
            {
                "v": 1,
                "args": ["walk", "--path", str(tree)],
                "timeout_s": budget,
            },
            process=handle.process,
        )
        assert response == {
            "v": 1,
            "ok": False,
            "error": f"timeout_s must be positive (got {budget})",
        }


def test_walk_has_its_own_smaller_output_ceiling(
    broker_bin: Path, serve, tmp_path: Path
) -> None:
    """A7: `walk` answers under 64 MiB, and this daemon is what enforces it.

    One ``walk`` answers about one tree, and a tree is bounded by
    ``E2B_DISK_MAX_ENTRIES`` (500000 entries) at ~80 bytes a line -- ~40 MB
    unescaped -- so the walk ceiling is 64 MiB (1.6x) while every other verb
    keeps ``PRIV_MAX_OUTPUT`` (256 MiB). 64 MiB unescaped reaches at most 6x
    that on the wire (the widest JSON escape), 384 MiB, still under the worker
    side's own 512 MiB *line* ceiling: a legitimate walk answer is therefore
    always refused by this daemon -- ``ok:false``, naming the walk cap -- and
    the worker's larger ceiling only ever catches an answer that did not come
    from here.

    The crossing is driven with a small compile-time cap (the shipped one would
    need ~1M entries), and the tree is walked directly first, so the refusal is
    known to be about a walk that really does exceed the cap rather than about
    the tree being empty. The shipped constants' relations are asserted at the
    end, against the source, because they are what makes the small number safe.
    """
    small_cap = 4096
    tree = _workspace(tmp_path) / "sb-walk-cap"
    tree.mkdir(parents=True)
    for index in range(200):
        (tree / f"entry-{index:04d}").write_text("x")

    with _installed_with_walk_cap(small_cap, tmp_path):
        direct = subprocess.run(
            [str(broker_bin), "walk", "--path", str(tree)],
            env=_broker_env(broker_bin, tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert direct.returncode == 0, direct.stderr
        assert len(direct.stdout) > small_cap

        handle = serve("walk-cap")
        _await_listening(handle)
        response = _request(
            handle.socket,
            {"v": 1, "args": ["walk", "--path", str(tree)]},
            process=handle.process,
        )

        assert response == {
            "v": 1,
            "ok": False,
            "error": (
                f"stdout exceeded the {small_cap}-byte walk output cap and was "
                "killed"
            ),
        }
        # The cap kills the walk, never the broker.
        assert handle.process.poll() is None
        assert _request(handle.socket, {"v": 1, "hello": True})["ok"] is True
        # The variant binary has to be off this path before the shipped one goes
        # back: a running daemon holds the inode (ETXTBSY), and the module
        # fixture's teardown would otherwise race with this restore.
        handle.stop()

    shipped = _c_bytes_define("PRIV_MAX_WALK_OUTPUT")
    every_verb = _c_bytes_define("PRIV_MAX_OUTPUT")
    assert shipped == 64 * 1024**2
    assert every_verb == 256 * 1024**2
    assert shipped < every_verb
    # 6x is the widest escape: even then the daemon refuses before the worker's
    # 512 MiB line ceiling is anywhere near.
    assert shipped * 6 < 512 * 1024**2


#: The runaway tree: entry count and name length are the two levers on how long
#: a ``walk`` takes, and hard links are by far the cheapest way to buy walk
#: time -- measured on this lane, 150k *files* build in 6.6 s and walk in
#: 0.29 s, the same 150k *hard links* build in 3.2 s and walk in 0.80 s (one
#: shared inode per directory, one name per entry, no data).
#:
#: Sizes are measured, not guessed. On the arm64 development box's
#: ``e2b-sandlock-test`` container (overlayfs, VirtioFS-backed VM disk):
#:
#:   * the walk's own rate is the stable half, 4.9-6.5 us/entry (300k -> 1.5 s,
#:     600k -> 3.3-3.9 s), so **350k entries with 200-byte names** walk in
#:     1.7-2.3 s and outlive the 1 s budget by ~2x -- the SIGKILL is reached
#:     deterministically, not by a coin flip between "the walk finished" and
#:     "the walk was killed";
#:   * building the tree is the noisy half (inode allocation through the VM's
#:     disk), 21-66 us/entry: 350k entries cost 7-23 s. The request itself is
#:     killed at 1 s, so the case costs one build plus ~1 s: **9-25 s total**,
#:     inside the 30 s lane budget even when the host is busy.
RUNAWAY_ENTRIES = 350_000
RUNAWAY_PER_DIR = 16_384
#: 200 bytes: long enough to make the walk's per-entry printf/pipe work matter,
#: short enough to stay away from NAME_MAX (255).
RUNAWAY_NAME_WIDTH = 200


def _build_runaway_tree(root: Path) -> None:
    """A tree whose ``walk`` outlives a 1 s budget; see ``RUNAWAY_ENTRIES``."""
    root.mkdir(parents=True)
    made = 0
    index = 0
    while made < RUNAWAY_ENTRIES:
        sub = root / f"d{index:04d}"
        sub.mkdir()
        seed = sub / "seed"
        with open(seed, "wb") as sink:
            sink.write(b"x")
        made += 1
        limit = min(RUNAWAY_PER_DIR, RUNAWAY_ENTRIES - made)
        for i in range(limit):
            os.link(seed, sub / ("f%05d" % i + "x" * (RUNAWAY_NAME_WIDTH - 6)))
        made += limit
        index += 1


def test_a_walk_that_outlives_the_budget_is_killed_and_the_daemon_survives(
    serve, tmp_path: Path
) -> None:
    """The budget is enforced by killing the grandchild, and only that request.

    ``walk`` is the one verb whose runtime the caller does not bound (it is the
    quota ledger's path over an arbitrarily large tree), so ``timeout_s`` is
    the only thing standing between a runaway walk and a handler that never
    answers. This is the execution half of that contract, next to the cheap
    refusal in ``test_serve_refuses_a_non_positive_timeout``:

    * ``ok:false`` with the daemon's own account ("timed out after 1s and was
      killed with SIGKILL") -- not a partial ``stdout`` the caller could
      mistake for a finished tree, and not a silent truncation;
    * the daemon is still up afterwards, and the very next ``hello`` succeeds:
      the kill costs that one connection, not the node's broker.

    The tree is built with hard links (see ``_build_runaway_tree``); its size
    is whatever makes the walk reliably outlive the budget on this lane -- the
    numbers are in the ``RUNAWAY_ENTRIES`` comment.
    """
    handle = serve("runaway")
    _await_listening(handle)
    tree = _workspace(tmp_path) / "sbx_runaway"
    _build_runaway_tree(tree)

    started = time.monotonic()
    response = _request(
        handle.socket,
        {"v": 1, "args": ["walk", "--path", str(tree)], "timeout_s": 1},
        process=handle.process,
    )
    elapsed = time.monotonic() - started

    # Exactly the refusal: no stdout, no exit code -- the walk never completed.
    assert response == {
        "v": 1,
        "ok": False,
        "error": "the request timed out after 1s and was killed with SIGKILL",
    }
    # The kill happened on the budget, not after the walk happened to finish.
    assert elapsed < 5.0
    # ...and it cost one request, not the broker.
    assert handle.process.poll() is None
    hello = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)
    assert hello["ok"] is True
    assert hello["peer_uid"] == _peer_uid()
    assert hello["peer_gid"] == _peer_gid()
