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
* the gate is ``SO_PEERCRED`` against ``E2B_BROKER_PEER_UID`` /
  ``E2B_BROKER_PEER_GID`` (default 65534), never the socket file mode: the
  unprivileged worker has to be able to ``connect()``;
* the daemon execs **its own image** (``/proc/self/exe``) and never a program
  from the request -- ``args[0]`` is one of ``chown`` / ``rm`` / ``walk``, so
  it is not a general launcher;
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
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "deploy" / "priv"

#: This module's own pool segment. The gate is a range membership test, so a
#: daemon started here must not overlap another lane's pool -- and the chown
#: target has to land inside *this* range.
POOL_START = 21000
POOL_SIZE = 16
POOL_END = POOL_START + POOL_SIZE - 1

#: The request-line cap frozen by the protocol (64 KiB).
MAX_REQUEST = 64 * 1024

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
            "E2B_MAINT_BIN": str(binary),
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


@pytest.fixture(scope="module")
def broker_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``deploy/priv`` built the way the image builds it, warnings and all.

    The empty ``stderr`` is part of the assertion: ``-Wall -Wextra`` clean is
    the bar the other brokers are held to.
    """
    out = tmp_path_factory.mktemp("priv-c1")
    binary = out / "e2b-maint"
    build = subprocess.run(
        [
            "cc",
            "-O2",
            "-Wall",
            "-Wextra",
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
    return binary


@pytest.fixture()
def serve(broker_bin: Path, tmp_path: Path):
    """Start daemons on throwaway sockets; every one of them is reaped."""
    started: list[_Serve] = []

    def _start(
        name: str = "broker",
        *,
        pass_flag: bool = True,
        **overrides: str | None,
    ) -> _Serve:
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

    response = _request(handle.socket, {"v": 1, "hello": True}, process=handle.process)

    assert response == {"v": 1, "ok": False, "error": refusal}
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
    assert handle.stop() == (
        f"e2b-maint: serving on {handle.socket}\n"
        f"e2b-maint: refused: {refusal}\n"
    )


def test_serve_refuses_to_run_from_an_unpinned_path(
    broker_bin: Path, tmp_path: Path
) -> None:
    unpinned = tmp_path / "elsewhere" / "e2b-maint"

    run = subprocess.run(
        [str(broker_bin), "serve", "--socket", str(tmp_path / "unpinned.sock")],
        env=_broker_env(broker_bin, tmp_path, E2B_MAINT_BIN=str(unpinned)),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 77
    assert run.stdout == ""
    assert run.stderr == (
        f"e2b-maint: refused: cannot resolve the installed broker path "
        f"{unpinned}: No such file or directory\n"
    )

    # An existing path that is *not* the running image is refused too: serve
    # only ever runs from the installed path, never a copy of it.
    run = subprocess.run(
        [str(broker_bin), "serve", "--socket", str(tmp_path / "unpinned.sock")],
        env=_broker_env(
            broker_bin, tmp_path, E2B_MAINT_BIN=str(_image_cache(tmp_path))
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert run.returncode == 77
    assert run.stdout == ""
    assert run.stderr == (
        f"e2b-maint: refused: this broker must run from the installed path "
        f"{os.path.realpath(_image_cache(tmp_path))} (E2B_MAINT_BIN), but the "
        f"running image is {os.path.realpath(broker_bin)}: not serving from a "
        "copy\n"
    )
