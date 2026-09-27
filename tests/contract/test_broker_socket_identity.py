"""C1 wave-1 final fix: the broker learns the *peer's* identity over the socket.

The exec shape's hidden premise was "the process that runs e2b-maint *is* the
worker": ``chown --worker`` chowns to ``getuid()/getgid()`` and
``priv_gid_allowed`` accepts "the worker's own gid". Behind ``serve`` that
premise is false -- the daemon is root and forks a **root** grandchild -- so
both rules silently changed meaning:

* ``chown --worker`` handed the tree to **root** (``0:0``), which the worker's
  own contract forbids ("never name root", ``envd_service/uid_pool.py``);
* ``--gid <worker gid>`` was refused, because ``priv_gid_allowed`` compared
  against the *daemon's* gid (0) instead of the worker's -- and wave 2's
  ``Sandbox.create()`` always names it (``0770 owner=<pool uid>
  group=<worker gid>``, ``envd_service/volumes.py``).

Both are one root cause and both are invisible to a lane whose daemon and peer
share a gid: the existing socket contract file runs as root on *both* sides, so
"the worker's own gid" == 0 == the daemon's gid and the bug hides. This file
closes that hole by making the two identities *differ*:

* the daemon runs as **root** with ``E2B_BROKER_PEER_UID/GID=65534``;
* the client drops to **uid/gid 65534** (``setpriv``) and drives the real
  ``PrivHelpers`` argv builders through the real Python transport, startup
  handshake included;
* so ``--gid 65534`` must be accepted and land ``21000:65534``, and
  ``chown --worker`` must land ``65534:65534`` -- never ``0:0``.

``setpriv`` is a util-linux binary that is present in the test image (and in
every distro the worker image derives from); it is the same privilege drop the
``tmp/c1_e2e_probe.py`` probe does with ``preexec_fn``, spelled as a program so
that this test needs no forking hooks. ``--clear-groups`` matters: without it
the child would inherit root's supplementary groups.

``E2B_ROUTE_B_TMP_ROOT`` is pointed inside the state base for every process
here: the shared shape self-check refuses a scratch root outside the broker's
whitelist, which is the real deployment's premise and not something this lane
may paper over.

Root, ``SO_PEERCRED``, ``chown``, AF_UNIX and the uid drop are all why this
lives in the container lane:

    docker run --rm -v "$PWD:/w" -w /w e2b-sandlock-test:latest \
        sh -c 'python3 -m pytest tests/contract/test_broker_socket_identity.py -q'
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRIV_DIR = PROJECT_ROOT / "deploy" / "priv"

#: ``serve`` only trusts the compiled-in install path, so the rebuilt broker
#: goes where the image installs it (as in ``test_broker_socket_c.py``).
INSTALLED_BROKER = Path("/var/lib/e2b-priv/e2b-maint")

#: The two identities that must stay *different* for this file to mean
#: anything: the daemon is root, the worker it brokers for is 65534.
WORKER_UID = 65534
WORKER_GID = 65534

#: This module's own pool segment (the chown target has to land inside it).
POOL_START = 21000
POOL_SIZE = 16
POOL_END = POOL_START + POOL_SIZE - 1

#: The whole client half: resolve the shape (which runs the real hello
#: handshake), then perform one action. It prints a single JSON line; every
#: refusal that escapes as an exception exits non-zero, so a hidden failure can
#: never be mistaken for success.
CLIENT_SCRIPT = r"""
import json
import socket
import sys
from pathlib import Path

repo, action, target = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, repo)

from envd_service import priv_helpers
from envd_service.config import Settings

result = {"euid": __import__("os").geteuid(), "egid": __import__("os").getegid(),
          "action": action}
helpers = priv_helpers.configure_priv_helpers(Settings())
if helpers is None:
    result["error"] = "configure_priv_helpers resolved no shape"
    print(json.dumps(result))
    raise SystemExit(3)

result["transport"] = helpers.transport
result["socket"] = str(helpers.broker_socket)
result["expected_roots"] = [str(path) for path in helpers._root_paths()]
hello = helpers.hello()
result["peer_uid"] = hello.get("peer_uid")
result["peer_gid"] = hello.get("peer_gid")
result["roots"] = hello.get("roots")

if action == "gid":
    helpers.chown(uid=21000, path=target, gid=65534)
elif action == "worker":
    helpers.chown_worker(path=target)
elif action == "image":
    helpers.chown(uid=21000, path=target)
elif action == "out-of-pool":
    # The worker's own validator would refuse uid 999 before the request left
    # this process, so this one goes down the wire raw: what is on trial is the
    # *daemon's* pool gate, not the worker's pre-filter.
    line = json.dumps(
        {"v": 1, "args": ["chown", "--uid", "999", "--path", str(target)],
         "timeout_s": 300}
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(helpers.broker_socket))
        client.sendall(line)
        client.shutdown(socket.SHUT_WR)
        raw = b""
        while b"\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                break
            raw += chunk
    result["refusal"] = json.loads(raw.decode("utf-8"))
else:
    result["error"] = f"unknown action {action!r}"
    print(json.dumps(result))
    raise SystemExit(3)

print(json.dumps(result))
"""


def _snapshot(path: Path):
    """The installed broker as the image shipped it (capability xattr too)."""
    if not path.exists():
        return None
    info = path.stat()
    return (
        path.read_bytes(),
        stat.S_IMODE(info.st_mode),
        info.st_uid,
        info.st_gid,
        os.getxattr(path, "security.capability"),
    )


def _restore(path: Path, snapshot) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    data, mode, uid, gid, xattr = snapshot
    path.write_bytes(data)
    os.chmod(path, mode)
    os.chown(path, uid, gid)
    os.setxattr(path, "security.capability", xattr)


@pytest.fixture(scope="module")
def installed_broker(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``deploy/priv`` built clean, installed where ``serve`` insists on it.

    The empty ``stderr`` is the same ``-Wall -Wextra`` bar the other broker
    lanes hold themselves to, and the fresh image is what makes this file test
    *this* revision: it has to carry its file capability, because the uid-65534
    client half walks the real ``_build_helpers`` self-check.
    """
    out = tmp_path_factory.mktemp("priv-c1-identity")
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
    os.chown(INSTALLED_BROKER, 0, WORKER_GID)
    subprocess.run(
        ["setcap", "cap_chown,cap_dac_override+ep", str(INSTALLED_BROKER)],
        check=True,
        capture_output=True,
    )
    try:
        yield INSTALLED_BROKER
    finally:
        _restore(INSTALLED_BROKER, previous)


def _base_env(scratch: Path) -> dict[str, str]:
    """The one shape both halves read: daemon and worker, byte for byte.

    The workspace base, state base, shared root and image cache are four
    distinct absolute directories under ``scratch``, so the daemon's root list
    has all four entries and the client's ``_root_paths()`` has to spell them
    identically (the handshake compares them).
    """
    env = dict(os.environ)
    for name in (
        "E2B_PRIV_HELPER_SOCKET",
        "E2B_MAINT_BIN",
        "E2B_BROKER_WORKER_UID",
        "E2B_BROKER_WORKER_GID",
    ):
        env.pop(name, None)
    env.update(
        {
            "E2B_UID_POOL_START": str(POOL_START),
            "E2B_UID_POOL_SIZE": str(POOL_SIZE),
            "E2B_WORKSPACE_BASE": str(scratch / "sandboxes"),
            "E2B_STATE_BASE": str(scratch / "state"),
            "E2B_SHARED_VOLUME_ROOT": str(scratch / "shared"),
            "E2B_IMAGE_CACHE_DIR": str(scratch / "images"),
            # Inside the state base on purpose: the shared shape self-check
            # refuses a scratch root outside the broker's whitelist.
            "E2B_ROUTE_B_TMP_ROOT": str(scratch / "state" / ".route-b"),
            # The daemon's gate and the client's identity are the same number:
            # that is what makes the daemon accept the uid-65534 socket.
            "E2B_BROKER_PEER_UID": str(WORKER_UID),
            "E2B_BROKER_PEER_GID": str(WORKER_GID),
        }
    )
    return env


def _client_env(scratch: Path, socket_path: Path) -> dict[str, str]:
    env = _base_env(scratch)
    env["E2B_PRIV_HELPER_TRANSPORT"] = "socket"
    env["E2B_PRIV_HELPER_SOCKET"] = str(socket_path)
    return env


@pytest.fixture(scope="module")
def socket_scratch() -> Path:
    """A world-traversable scratch tree on the container's own filesystem.

    Two properties are load-bearing and neither is free:

    * **every parent has to be traversable by uid 65534**, or the client
      cannot ``connect()`` to the socket or ``stat`` the trees it chowns --
      pytest's ``tmp_path`` sits under a ``0700`` root directory;
    * **``chown`` to a non-root uid has to actually change the owner.** The
      worktree is mounted over virtiofs, where ``chown 65534:65534`` is
      silently a no-op (it reads back as ``0:0``), so a scratch tree under the
      repository would make every ownership assertion below meaningless.
      ``/tmp`` is the container's own overlay and keeps the semantics --
      exactly the filesystem ``tmp/c1_e2e_probe.py`` uses for the same reason.
    """
    root = Path(tempfile.mkdtemp(prefix="c1-identity-", dir="/tmp"))
    os.chmod(root, 0o755)
    for name in ("sandboxes", "state", "shared", "images"):
        (root / name).mkdir(mode=0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def broker(installed_broker: Path, socket_scratch: Path):
    """A real ``e2b-maint serve`` as root, gating on uid/gid 65534."""
    socket_path = socket_scratch / "broker.sock"
    process = subprocess.Popen(
        [str(installed_broker), "serve", "--socket", str(socket_path)],
        env=_base_env(socket_scratch),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while not socket_path.exists():
        if process.poll() is not None:
            out, err = process.communicate()
            pytest.fail(
                f"the broker exited before binding its socket "
                f"(rc={process.returncode}): {out.decode()} {err.decode()}"
            )
        if time.monotonic() >= deadline:
            pytest.fail(f"the broker never created {socket_path}")
        time.sleep(0.02)
    try:
        yield socket_path
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.communicate(timeout=30)


def _run_client(
    broker: Path, scratch: Path, action: str, target: Path
) -> dict:
    """One privilege-dropped ``PrivHelpers`` round trip, as uid 65534.

    Non-zero exit is a hard failure on purpose: ``CLIENT_SCRIPT`` lets only the
    refusal it asked for come back as data, so any other exit means the shape
    did not hold together and the test must not read a stale tree as success.
    """
    run = subprocess.run(
        [
            "setpriv",
            "--reuid",
            str(WORKER_UID),
            "--regid",
            str(WORKER_GID),
            "--clear-groups",
            sys.executable,
            "-c",
            CLIENT_SCRIPT,
            str(PROJECT_ROOT),
            action,
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(PROJECT_ROOT),
        env=_client_env(scratch, broker),
    )
    if run.returncode != 0:
        pytest.fail(
            f"the uid-{WORKER_UID} client failed for {action!r} "
            f"(rc={run.returncode}):\nstdout={run.stdout}\nstderr={run.stderr}"
        )
    return json.loads(run.stdout.strip().splitlines()[-1])


def _root_owned_tree(scratch: Path, name: str) -> Path:
    """A tree the worker cannot own yet, exactly as teardown finds them."""
    tree = scratch / "sandboxes" / name
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "workspace").mkdir(exist_ok=True)
    (tree / "workspace" / "file").write_text("x", encoding="utf-8")
    for path in (tree, tree / "workspace", tree / "workspace" / "file"):
        os.chown(path, 0, 0)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return tree


def test_the_client_half_really_is_the_worker(
    broker: Path, socket_scratch: Path
) -> None:
    """A guard on the guard: the assertions below are only meaningful if the
    client half dropped to 65534 while the daemon kept the root identity."""
    result = _run_client(
        broker, socket_scratch, "worker", _root_owned_tree(socket_scratch, "sbx_guard")
    )
    assert (result["euid"], result["egid"]) == (WORKER_UID, WORKER_GID)
    assert result["transport"] == "socket"


def test_the_handshake_names_the_peer_it_treats_as_the_worker(
    broker: Path, socket_scratch: Path
) -> None:
    """``hello`` must report the *peer's* uid **and** gid, and the Python side
    must accept them: this is the pair the whole fix hangs on."""
    result = _run_client(
        broker, socket_scratch, "worker", _root_owned_tree(socket_scratch, "sbx_hello")
    )
    assert result["peer_uid"] == WORKER_UID
    assert result["peer_gid"] == WORKER_GID
    # Byte for byte: the daemon's roots and the worker's are the same list, in
    # the same order (workspace base, state base, shared root, image cache).
    assert result["roots"] == result["expected_roots"]
    assert result["expected_roots"] == [
        str(socket_scratch / "sandboxes"),
        str(socket_scratch / "state"),
        str(socket_scratch / "shared"),
        str(socket_scratch / "images"),
    ]


def test_chown_accepts_the_worker_gid_over_the_socket(
    broker: Path, socket_scratch: Path
) -> None:
    """``--gid <worker gid>`` is the c1 tree model's group, so it must pass.

    Reverting the fix makes the daemon refuse it (``exit 77: gid 65534 is
    neither the worker's own gid (0) nor ...``) and the client raises.
    """
    tree = _root_owned_tree(socket_scratch, "sbx_gid")
    _run_client(broker, socket_scratch, "gid", tree)
    assert (os.stat(tree).st_uid, os.stat(tree).st_gid) == (POOL_START, WORKER_GID)


def test_chown_worker_hands_the_tree_back_to_the_worker(
    broker: Path, socket_scratch: Path
) -> None:
    """``chown --worker`` must land the *worker*, never the root daemon.

    Reverting the fix makes the daemon chown to ``getuid()/getgid()`` -- its
    own, ``0:0`` -- and silently hands the tree to root.
    """
    tree = _root_owned_tree(socket_scratch, "sbx_worker")
    _run_client(broker, socket_scratch, "worker", tree)
    assert (os.stat(tree).st_uid, os.stat(tree).st_gid) == (WORKER_UID, WORKER_GID)


def test_the_image_cache_root_can_be_handed_over(
    broker: Path, socket_scratch: Path
) -> None:
    """The fourth root: a sandbox secret the worker owns but the sandbox must."""
    secret_dir = socket_scratch / "images" / "secrets" / "sbx_secret"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret = secret_dir / "probe.secret"
    secret.write_text("value", encoding="utf-8")
    for path in (secret_dir, secret):
        os.chown(path, WORKER_UID, WORKER_GID)
        os.chmod(path, 0o600 if path.is_file() else 0o700)
    _run_client(broker, socket_scratch, "image", secret)
    assert (secret.stat().st_uid, secret.stat().st_gid) == (POOL_START, POOL_START)


def test_an_out_of_pool_uid_is_refused_by_the_daemon(
    broker: Path, socket_scratch: Path
) -> None:
    """The daemon's own pool gate, reached by a peer that passed the uid gate."""
    tree = _root_owned_tree(socket_scratch, "sbx_out")
    result = _run_client(broker, socket_scratch, "out-of-pool", tree)
    refusal = result["refusal"]
    assert refusal["ok"] is True
    assert refusal["exit"] == 77
    assert refusal["stderr"] == (
        f"e2b-maint: refused: uid 999 is outside the privileged helper uid "
        f"pool {POOL_START}..{POOL_END}\n"
    )
    assert (os.stat(tree).st_uid, os.stat(tree).st_gid) == (0, 0)
