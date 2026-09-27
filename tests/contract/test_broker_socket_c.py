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

import dataclasses
import errno
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "deploy" / "priv"

#: Where ``serve`` insists on running from: the path is compiled into the
#: broker (``PRIV_DEFAULT_MAINT_BIN``), precisely so that no setting can move
#: the trust. The test lane therefore installs the freshly built binary there.
INSTALLED_BROKER = Path("/var/lib/e2b-priv/e2b-maint")

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
    process: subprocess.Popen
    _output: tuple[bytes, bytes] | None = None

    def stop(self) -> str:
        """Kill the daemon's whole process group and return what it logged."""
        if self._output is None:
            if self.process.poll() is None:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            self._output = self.process.communicate(timeout=30)
        return self._output[1].decode()


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
        process = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        handle = _Serve(socket=socket_path, process=process)
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


def _connect_as(uid: int, socket_path: Path) -> str:
    """``connect()`` once as `uid`, in a forked child, and report the outcome.

    The socket's mode is the layer a pool uid has to be stopped by (before any
    peer check can run), so the test has to drop to that uid to see it.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
            outcome = "connected"
        except OSError as exc:
            outcome = f"error:{exc.errno}"
        os.write(write_fd, outcome.encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    chunks = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        chunks += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    return chunks.decode()


def _chown(path: Path, uid: int = POOL_START) -> dict:
    return {"v": 1, "args": ["chown", "--uid", str(uid), "--path", str(path)]}


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
