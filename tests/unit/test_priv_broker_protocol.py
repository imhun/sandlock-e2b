"""C1 Task 2: the worker's side of the maintenance broker's socket transport.

The broker is a per-node root daemon (``e2b-maint serve``, Task 1) that listens
on a unix socket, checks the peer's credentials and then ``fork``/``exec``s
*itself* with the argv it was handed -- the worker no longer execs a
file-capability binary for ``chown``. This file pins the Python half of the
frozen protocol (``envd_service/priv_helpers.py``).

The daemon here is a **real** ``AF_UNIX`` server in a thread, never a mock of
the socket layer: the framing (one JSON line per request, one line back), the
argv shape (``args`` without ``argv[0]``) and every refusal path are exercised
over the wire the production daemon speaks, so the C side in
``tests/contract/test_broker_socket_c.py`` is testing the other end of the
same bytes.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from envd_service import priv_helpers as ph
from envd_service.config import Settings


class FakeDaemon:
    """A one-socket ``e2b-maint serve`` stand-in, running in a thread.

    ``handler`` receives the parsed request and returns the response object,
    or ``None`` to answer nothing at all (that is how the worker's timeout
    path is reached). Every request it accepted is kept in ``requests``.
    """

    def __init__(self, socket_path: Path, handler) -> None:
        self.socket_path = Path(socket_path)
        self.requests: list[dict] = []
        self._handler = handler
        self._held: list[socket.socket] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            raw = b""
            while b"\n" not in raw:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
            request = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
            self.requests.append(request)
            response = self._handler(request)
            if response is None:
                # A daemon that accepted the request and never answers: the
                # connection has to stay *open*, or the worker sees a clean
                # EOF instead of its own timeout.
                self._held.append(conn)
                continue
            with conn:
                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def close(self) -> None:
        for conn in self._held:
            conn.close()
        self._held.clear()
        self._server.close()


@pytest.fixture
def fake_daemon(tmp_path: Path):
    """Start fake daemons; every one of them is closed at teardown."""

    started: list[FakeDaemon] = []

    def _start(handler, name: str = "broker.sock") -> FakeDaemon:
        daemon = FakeDaemon(tmp_path / name, handler)
        started.append(daemon)
        return daemon

    yield _start
    for daemon in started:
        daemon.close()


def _ok_request(request: dict) -> dict:
    return {"v": 1, "ok": True, "exit": 0, "stdout": "", "stderr": ""}


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "sandboxes"
    workspace.mkdir(exist_ok=True)
    return workspace


def _shared(tmp_path: Path) -> Path:
    shared = tmp_path / "shared"
    shared.mkdir(exist_ok=True)
    return shared


def _helpers(tmp_path: Path, *, socket_path: Path | None = None) -> ph.PrivHelpers:
    return ph.PrivHelpers(
        slot_spawn=tmp_path / "e2b-priv" / "e2b-slot-spawn",
        maint=tmp_path / "e2b-priv" / "e2b-maint",
        supervise_bin=(
            tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
        ),
        uid_pool_start=10000,
        uid_pool_size=1000,
        workspace_base=_workspace(tmp_path),
        shared_volume_root=_shared(tmp_path),
        transport="socket",
        broker_socket=socket_path,
    )


def _settings(tmp_path: Path, **overrides) -> Settings:
    fields = dict(
        priv_helpers="auto",
        workspace_base=_workspace(tmp_path),
        shared_volume_root=str(_shared(tmp_path)),
        uid_pool_start=10000,
        uid_pool_size=1000,
        route_b_tmp_root=_workspace(tmp_path) / ".route-b",
    )
    fields.update(overrides)
    return Settings(**fields)


def _install(tmp_path: Path, monkeypatch) -> Path:
    """Fake broker binaries + a stubbed capability reader (as in the F1 tests).

    Decision 3 of the brief: the two binaries stay in the image and keep their
    reachability/capability self-check -- the transport only decides which way
    the *runtime* request travels.
    """
    helper_dir = tmp_path / "e2b-priv"
    helper_dir.mkdir(mode=ph.HELPER_DIR_MODE, exist_ok=True)
    helper_dir.chmod(ph.HELPER_DIR_MODE)
    os.chown(helper_dir, 0, os.getegid())
    for name in ("e2b-slot-spawn", "e2b-maint"):
        target = helper_dir / name
        target.write_bytes(b"\x7fELF")
        target.chmod(ph.HELPER_FILE_MODE)
        os.chown(target, 0, os.getegid())
    caps = {
        "e2b-slot-spawn": ph.CAP_SETUID_MASK | ph.CAP_SETGID_MASK,
        "e2b-maint": ph.CAP_CHOWN_MASK | ph.CAP_DAC_OVERRIDE_MASK,
    }

    def _fake_read(path: Path) -> ph.FileCapabilities:
        mask = caps[path.name]
        return ph.FileCapabilities(effective=mask, permitted=mask, inheritable=0)

    monkeypatch.setattr(ph, "read_file_capabilities", _fake_read)
    monkeypatch.setattr(ph, "DEFAULT_HELPER_DIR", helper_dir)
    return helper_dir


def _stub_worker_identity(monkeypatch, uid: int = 65534, gid: int = 65534) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    monkeypatch.setattr(os, "getegid", lambda: gid)


def _hello(roots: list[str], *, uid_pool: list[int] | None = None) -> dict:
    return {
        "v": 1,
        "ok": True,
        "peer_uid": 65534,
        "peer_gid": 65534,
        "uid_pool": [10000, 1000] if uid_pool is None else uid_pool,
        "roots": roots,
    }


# ------------------------------------------------------------- argv over wire


def test_socket_transport_sends_args_without_argv0(
    tmp_path: Path, fake_daemon
) -> None:
    """The daemon execs *itself*: the request carries the verb and its flags.

    ``args`` must not name the binary -- that would be a path the daemon has
    to trust, and on the daemon's side the executable is pinned to
    ``/proc/self/exe``. The whole request is compared exactly, which also pins
    the per-verb budget decision 4 asks for (``chown``/``rm`` 300 s).
    """
    daemon = fake_daemon(_ok_request)
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    helpers.chown(uid=10003, path=target, recursive=True)

    assert daemon.requests == [
        {
            "v": 1,
            "args": [
                "chown",
                "--uid",
                "10003",
                "--gid",
                "10003",
                "--recursive",
                "--path",
                str(target),
            ],
            "timeout_s": 300,
        }
    ]
    assert str(helpers.maint) not in daemon.requests[0]["args"]


def test_socket_transport_asks_for_the_walk_budget_of_120s(
    tmp_path: Path, fake_daemon
) -> None:
    """Decision 4: a ``walk`` of a big tree is not a ``chown``."""
    daemon = fake_daemon(_ok_request)
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    helpers.walk(target)
    helpers.remove(target)

    assert [request["timeout_s"] for request in daemon.requests] == [120, 300]


def test_socket_transport_maps_refusal_to_privhelpererror(
    tmp_path: Path, fake_daemon
) -> None:
    """A non-zero exit keeps today's wording, verbatim (F1 pinned it)."""
    daemon = fake_daemon(
        lambda request: {
            "v": 1,
            "ok": True,
            "exit": 77,
            "stdout": "",
            "stderr": "e2b-maint: refused",
        }
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint (exit 77): e2b-maint: refused"
    )


def test_socket_transport_raises_on_ok_false(tmp_path: Path, fake_daemon) -> None:
    """``ok:false`` is "the request never ran": name the daemon's error."""
    daemon = fake_daemon(
        lambda request: {
            "v": 1,
            "ok": False,
            "error": "path /etc/hosts is outside the privileged helper roots",
        }
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint: path /etc/hosts is outside "
        "the privileged helper roots"
    )


def test_socket_transport_rejects_an_ok_without_an_exit(
    tmp_path: Path, fake_daemon
) -> None:
    """``{"ok": true}`` alone is not a completed privileged step.

    The daemon only says ``ok`` for a request it *ran*; without an integer
    ``exit`` there is nothing that says it succeeded. Defaulting the missing
    field to 0 turned a truncated (or hand-rolled) answer into a silent
    success -- the one thing a privileged step must never be.
    """
    daemon = fake_daemon(lambda request: {"v": 1, "ok": True, "stdout": ""})
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint: the broker answered ok "
        "without an integer exit (None)"
    )


def test_socket_transport_rejects_a_non_integer_exit(
    tmp_path: Path, fake_daemon
) -> None:
    """An exit the worker cannot interpret is a refusal, not a pass."""
    daemon = fake_daemon(
        lambda request: {"v": 1, "ok": True, "exit": "0", "stdout": ""}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint: the broker answered ok "
        "without an integer exit ('0')"
    )


def test_socket_transport_rejects_a_non_string_refusal(
    tmp_path: Path, fake_daemon
) -> None:
    """``ok:false`` has to carry the daemon's own words, as a string."""
    daemon = fake_daemon(lambda request: {"v": 1, "ok": False, "error": 42})
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint: the broker's refusal carries "
        "no error string (42)"
    )


def test_socket_transport_rejects_a_non_string_stream(
    tmp_path: Path, fake_daemon
) -> None:
    """A non-zero exit whose detail is not text cannot be reported verbatim."""
    daemon = fake_daemon(
        lambda request: {"v": 1, "ok": True, "exit": 77, "stderr": 5}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"remove {target} refused by e2b-maint: the broker's stderr is not a "
        "string (5)"
    )


def test_socket_transport_rejects_a_response_that_is_not_version_1(
    tmp_path: Path, fake_daemon
) -> None:
    """The wire version is frozen; another one is a daemon this worker cannot
    interpret, not one to read optimistically."""
    daemon = fake_daemon(
        lambda request: {"v": 2, "ok": True, "exit": 0, "stdout": ""}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} answered protocol "
        "version 2, not 1"
    )


def test_socket_transport_rejects_an_ok_that_is_not_a_boolean(
    tmp_path: Path, fake_daemon
) -> None:
    """``ok`` has to be a *boolean* -- a truthy string is not a completed step.

    The envelope check is the whole difference between "the broker ran the
    request" and "the broker said something that reads as truthy": ``if
    response["ok"]`` accepts ``"yes"`` (and ``1``, and ``[0]``), so a
    hand-rolled or truncated-and-repaired answer would be reported as a
    privileged step that happened. Both halves are pinned: the refusal is
    raised at all, and it names ``ok``.
    """
    daemon = fake_daemon(lambda request: {"v": 1, "ok": "yes", "exit": 0})
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} answered "
        f'{json.dumps({"v": 1, "ok": "yes", "exit": 0})!r}, whose "ok" is not '
        "a boolean"
    )


def test_socket_transport_raises_on_a_timeout(
    tmp_path: Path, fake_daemon, monkeypatch
) -> None:
    """A daemon that accepts and never answers is a refusal, not a hang.

    The worker's budget is the request's ``timeout_s`` plus the five seconds
    decision 4 leaves for the daemon's own kill-and-answer step.
    """
    assert ph.BROKER_TIMEOUT_SLACK_S == 5
    monkeypatch.setitem(ph.BROKER_TIMEOUT_S, "rm", 1)
    # The slack itself is what makes this test slow, and it is pinned above:
    # waiting out the real five seconds would buy nothing here.
    monkeypatch.setattr(ph, "BROKER_TIMEOUT_SLACK_S", 0)
    daemon = fake_daemon(lambda request: None)
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} is unreachable: timed out"
    )


def test_socket_transport_refuses_an_answer_over_the_read_limit(
    tmp_path: Path, fake_daemon, monkeypatch
) -> None:
    """The one-line answer is read with a ceiling, not with a timer alone.

    A socket timeout bounds how *long* the worker waits, not how many bytes it
    buffers, and the daemon's own output cap does not bound the line either:
    it counts **unescaped** bytes, and the daemon writes every undecodable
    filename byte as ``\\udcXX`` -- six times the size it judged. So the
    ceiling here is that cap at the escape blow-up (times both streams); the
    test drives it with an injected small value instead of producing 256 MiB,
    and asserts that crossing it is a refusal that names the limit (not a
    ``MemoryError``, and not a worker that keeps reading forever).
    """
    monkeypatch.setattr(ph, "BROKER_MAX_RESPONSE_BYTES", 4096)
    daemon = fake_daemon(
        lambda request: {"v": 1, "ok": True, "exit": 0, "stdout": "x" * 8192}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} answered more than "
        "4096 bytes without a newline to a rm request: refusing to buffer an "
        "answer over the rm limit"
    )


def test_a_walk_answer_over_the_walk_limit_is_named_and_refused(
    tmp_path: Path, fake_daemon, monkeypatch
) -> None:
    """``walk`` reads under its own, smaller ceiling -- and the refusal says so.

    A ``walk`` answer is one sandbox tree, and a tree is capped at
    ``E2B_DISK_MAX_ENTRIES`` (500_000) entries (~80 B a line, ~40 MB), so the
    walk line is read under ``BROKER_MAX_WALK_RESPONSE_BYTES`` instead of the
    two-stream escape-blowup ceiling the other verbs keep. The worker buffers
    that line and the worker container is capped at ``limits.memory: 2Gi``:
    without the smaller walk ceiling an overrun is an OOM kill before it is
    ever a refusal. Injected small, the way the ceiling above is driven, and
    asserted to name both the verb and the ceiling it used.
    """
    monkeypatch.setattr(ph, "BROKER_MAX_WALK_RESPONSE_BYTES", 4096)
    daemon = fake_daemon(
        lambda request: {"v": 1, "ok": True, "exit": 0, "stdout": "x" * 8192}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.walk(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} answered more than "
        "4096 bytes without a newline to a walk request: refusing to buffer "
        "an answer over the walk limit"
    )


def test_a_chown_answer_of_the_same_size_is_not_held_to_the_walk_limit(
    tmp_path: Path, fake_daemon, monkeypatch
) -> None:
    """The smaller ceiling is the walk verb's, not a quieter global one.

    Only ``walk`` can be big; a ``chown`` answer is a handful of bytes. An
    answer the *walk* ceiling refuses must still pass for ``chown`` -- proving
    the split is per-verb, not a shrunken ceiling for every verb.
    """
    monkeypatch.setattr(ph, "BROKER_MAX_WALK_RESPONSE_BYTES", 4096)
    daemon = fake_daemon(
        lambda request: {"v": 1, "ok": True, "exit": 0, "stdout": "x" * 8192}
    )
    helpers = _helpers(tmp_path, socket_path=daemon.socket_path)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    helpers.chown(uid=10000, path=target)

    assert daemon.requests[0]["args"][0] == "chown"


def test_the_walk_read_limit_is_below_the_generic_one_and_the_worker_memory() -> None:
    """The ordering that keeps a legitimate ``walk`` readable and safe.

    The ceiling has to be:

    * *above* the worst single-tree answer **on the wire** -- otherwise a
      legitimate tree is refused (the silent-staleness bug: the accounting and
      checkpoint paths only warn/skip). The worst tree is one at the
      ``E2B_DISK_MAX_ENTRIES`` cap (500000, ``deploy/k8s/worker.yaml``), ~80 B
      a line (an estimate, not an upper bound), ~40 MB unescaped, times the 6x
      wire escape blow-up (~229 MiB). 2x that is the margin this pins;
    * *below* the generic two-stream ceiling, so ``walk`` is the verb that
      reads under the smaller bound;
    * *below* the worker container's ``limits.memory`` (2Gi) so crossing it is
      a named refusal, never the kernel's OOM kill first. That memory number
      itself is pinned off the render in
      ``test_worker_manifest_permissions.test_the_worker_limits_match_the_walk_derivation``
      -- the two drift together or not at all.
    """
    worst_single_tree_wire_bytes = 500_000 * 80 * 6
    assert ph.BROKER_MAX_WALK_RESPONSE_BYTES < ph.BROKER_MAX_RESPONSE_BYTES
    assert ph.BROKER_MAX_WALK_RESPONSE_BYTES >= 2 * worst_single_tree_wire_bytes
    assert ph.BROKER_MAX_WALK_RESPONSE_BYTES < 2 * 1024**3


def test_socket_transport_raises_when_the_socket_is_gone(tmp_path: Path) -> None:
    """No listener: the worker refuses instead of falling back to exec."""
    missing = tmp_path / "nowhere.sock"
    helpers = _helpers(tmp_path, socket_path=missing)
    target = helpers.workspace_base / "sbx_a"
    target.mkdir()

    with pytest.raises(ph.PrivHelperError) as excinfo:
        helpers.remove(target)

    assert str(excinfo.value) == (
        f"the maintenance broker at {missing} is unreachable: "
        "No such file or directory"
    )


# ---------------------------------------------------------------- hello check


def test_hello_mismatch_refuses_to_start(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """The whitelist is the broker's, so a drift has to be named at startup.

    A daemon that never learned the image cache root is the c1 deployment
    defect this guard exists for: the worker would hand it every secret file
    under ``<E2B_IMAGE_CACHE_DIR>/secrets/`` and the broker would refuse each
    one. Fail closed, and say which list differs.
    """
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    daemon = fake_daemon(
        lambda request: _hello([str(_workspace(tmp_path)), str(_shared(tmp_path))]),
        name="hello.sock",
    )
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))
    worker_roots = [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
        str(tmp_path / "images"),
    ]

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} holds roots "
        f"{[str(_workspace(tmp_path)), str(_shared(tmp_path))]}, this worker "
        f"holds {worker_roots}: E2B_IMAGE_CACHE_DIR (and every other "
        "whitelisted root) must be the same on both sides of the socket"
    )
    assert daemon.requests == [{"v": 1, "hello": True}]


def test_a_matching_daemon_resolves_over_the_socket(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """The positive half of the guard: an agreeing daemon is accepted."""
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)
    roots = [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
        str(tmp_path / "images"),
    ]
    daemon = fake_daemon(lambda request: _hello(roots), name="ok.sock")
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))

    helpers = ph.resolve_priv_helpers(_settings(tmp_path))

    assert helpers is not None
    assert helpers.transport == "socket"
    assert helpers.broker_socket == daemon.socket_path
    assert [str(p) for p in helpers._root_paths()] == roots
    assert daemon.requests == [{"v": 1, "hello": True}]


def test_a_daemon_that_authenticated_a_different_uid_refuses_to_start(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """The daemon must treat *this* worker as its peer, by uid.

    ``peer_uid``/``peer_gid`` are the credentials the daemon gated the socket
    on -- the identity every ``chown --worker`` and ``--gid <own>`` decision is
    made against. Reading them and not comparing them is how a broker that
    authenticates somebody else passes two green unit suites and a broken
    deployment: fail at startup and print both sides.
    """
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    _stub_worker_identity(monkeypatch)
    roots = [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
        str(tmp_path / "images"),
    ]
    hello = _hello(roots)
    hello["peer_uid"] = 10001
    daemon = fake_daemon(lambda request: hello, name="peer-uid.sock")
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} authenticated the "
        "peer as 10001:65534, this worker runs as 65534:65534: the broker's "
        "E2B_BROKER_PEER_UID/GID must be this worker's own uid/gid"
    )


def test_a_daemon_that_authenticated_a_different_gid_refuses_to_start(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """...and by gid: the gid is what ``--gid <worker gid>`` is judged against.

    The socket lane that shares a gid between daemon and peer cannot see this,
    so a daemon whose gate names another group has to be refused here.
    """
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    _stub_worker_identity(monkeypatch)
    roots = [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
        str(tmp_path / "images"),
    ]
    hello = _hello(roots)
    hello["peer_gid"] = 0
    daemon = fake_daemon(lambda request: hello, name="peer-gid.sock")
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} authenticated the "
        "peer as 65534:0, this worker runs as 65534:65534: the broker's "
        "E2B_BROKER_PEER_UID/GID must be this worker's own uid/gid"
    )


def _symlinked_layout(tmp_path: Path) -> tuple[Path, Path]:
    """``<tmp>/real`` with ``<tmp>/link`` pointing at it (two spellings)."""
    real = tmp_path / "real"
    (real / "sandboxes").mkdir(parents=True, exist_ok=True)
    (real / "shared").mkdir(exist_ok=True)
    link = tmp_path / "link"
    if not link.exists():
        link.symlink_to(real, target_is_directory=True)
    return real, link


def test_both_transports_build_through_the_same_shape_self_check(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """One shape self-check, two transports -- the drift guard for the split.

    ``exec`` and ``socket`` differ in *who* performs a privileged step, not in
    what this worker accepts as a shape, so both have to ask the same
    questions in the same order *and* both have to come out of the one
    builder: a check (or a shape field) added to a branch and forgotten in the
    other -- with ``socket`` the production path -- would resolve a shape with
    a pool / route-B / scratch-root hole and only fail at the first privileged
    step, in the field.
    """
    calls: list[str] = []
    for name, marker in (
        ("_require_consistent_shape", "shape"),
        ("check_worker_identity_outside_pool", "identity"),
        ("_require_route_b_scratch_root", "scratch_root"),
    ):
        original = getattr(ph, name)

        def _spy(*args, _marker=marker, _original=original, **kwargs):
            calls.append(_marker)
            return _original(*args, **kwargs)

        monkeypatch.setattr(ph, name, _spy)

    built: list[str] = []
    original_build = ph._build_helpers

    def _build(settings, *, transport, broker_socket=None):
        built.append(transport)
        return original_build(
            settings, transport=transport, broker_socket=broker_socket
        )

    monkeypatch.setattr(ph, "_build_helpers", _build)

    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(tmp_path / "images"))
    daemon = fake_daemon(
        lambda request: _hello(
            [
                str(_workspace(tmp_path)),
                str(_shared(tmp_path)),
                str(tmp_path / "images"),
            ]
        ),
        name="both.sock",
    )
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))

    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    over_the_socket = ph.resolve_priv_helpers(_settings(tmp_path))
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "exec")
    through_exec = ph.resolve_priv_helpers(_settings(tmp_path))

    assert calls == ["shape", "identity", "scratch_root"] * 2
    assert built == ["socket", "exec"]
    assert over_the_socket.transport == "socket"
    assert over_the_socket.broker_socket == daemon.socket_path
    assert through_exec.transport == "exec"
    assert through_exec.broker_socket is None
    assert daemon.requests == [{"v": 1, "hello": True}]


def test_a_symlinked_spelling_of_the_same_roots_still_agrees(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """Same directory, two spellings: the handshake compares one normalization.

    The worker's roots come out of ``Settings`` (whose workspace base is
    ``.resolve()``d -- ``config.py`` calls that load-bearing, because the NFS
    export and ``tmp/`` both carry symlinks), while the daemon names the very
    same directories through ``getenv``, verbatim (Task 1). Refusing a daemon
    over a spelling would take the whole shape down, so ``realpath`` decides
    the comparison -- and only the comparison: the refusal still prints each
    side's own strings.
    """
    real, link = _symlinked_layout(tmp_path)
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(real / "images"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)
    daemon = fake_daemon(
        lambda request: _hello(
            [
                str(link / "sandboxes"),
                str(link / "shared"),
                str(link / "images"),
            ]
        ),
        name="link.sock",
    )
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))
    settings = _settings(
        tmp_path,
        workspace_base=real / "sandboxes",
        shared_volume_root=str(real / "shared"),
        route_b_tmp_root=real / "sandboxes" / ".route-b",
    )

    helpers = ph.resolve_priv_helpers(settings)

    assert helpers is not None
    assert helpers.transport == "socket"
    assert helpers.broker_socket == daemon.socket_path
    assert [str(p) for p in helpers._root_paths()] == [
        str(real / "sandboxes"),
        str(real / "shared"),
        str(real / "images"),
    ]
    assert daemon.requests == [{"v": 1, "hello": True}]


def test_a_genuinely_different_root_is_not_hidden_by_the_normalization(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """Normalizing spelling must not paper over a *different* directory."""
    real, link = _symlinked_layout(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(real / "images"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)
    daemon_roots = [
        str(link / "sandboxes"),
        str(link / "other"),
        str(link / "images"),
    ]
    daemon = fake_daemon(
        lambda request: _hello(daemon_roots),
        name="other.sock",
    )
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))
    settings = _settings(
        tmp_path,
        workspace_base=real / "sandboxes",
        shared_volume_root=str(real / "shared"),
        route_b_tmp_root=real / "sandboxes" / ".route-b",
    )
    worker_roots = [
        str(real / "sandboxes"),
        str(real / "shared"),
        str(real / "images"),
    ]

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(settings)

    assert str(excinfo.value) == (
        f"the maintenance broker at {daemon.socket_path} holds roots "
        f"{daemon_roots}, this worker holds {worker_roots}: "
        "E2B_IMAGE_CACHE_DIR (and every other whitelisted root) must be the "
        "same on both sides of the socket"
    )


def test_missing_socket_refuses_to_start_when_transport_is_socket(
    tmp_path: Path, monkeypatch
) -> None:
    """An explicit socket shape has no fallback: refuse, never run exec."""
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(tmp_path / "gone.sock"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        f"E2B_PRIV_HELPER_TRANSPORT=socket but the broker socket "
        f"{tmp_path / 'gone.sock'} does not exist: start the per-node broker "
        "before the worker (an explicit socket shape must not silently fall "
        "back to the file-capability binaries)"
    )


def test_socket_transport_still_needs_the_local_slot_spawn(
    tmp_path: Path, monkeypatch, fake_daemon
) -> None:
    """``e2b-slot-spawn`` is never externalized, so the socket shape needs it.

    Only ``chown``/``rm``/``walk`` travel to the node's daemon; a route-B slot
    has to start inside *this* pod's namespaces, so ``spawn_argv`` runs the
    local binary in both transports. Without this gate the shape resolved with
    no binary at all (the exec branch's "no broker installed" answer leaked
    into the branch that has a daemon) and the missing file only surfaced at
    the first ``Sandbox.create()`` -- a half-installed broker has to be named
    at startup. The check is local, so no handshake is involved.
    """
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "socket")
    _stub_worker_identity(monkeypatch)
    # A daemon that would agree, and a helper directory the image did *not*
    # populate: nothing in it, not even the binary the worker still runs.
    monkeypatch.setattr(ph, "DEFAULT_HELPER_DIR", tmp_path / "e2b-priv")
    daemon = fake_daemon(
        lambda request: _hello([str(_workspace(tmp_path)), str(_shared(tmp_path))]),
        name="uninstalled.sock",
    )
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(daemon.socket_path))

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        f"E2B_PRIV_HELPER_TRANSPORT=socket but the local "
        f"{ph.SLOT_SPAWN_NAME} ({tmp_path / 'e2b-priv' / 'e2b-slot-spawn'}) is "
        "missing: route-B slots start inside this worker's own namespaces, so "
        "e2b-slot-spawn is never handed to the node's daemon -- a socket shape "
        "without it is a half-installed broker and is named at startup, not at "
        "the first Sandbox.create()"
    )


def test_transport_auto_falls_back_to_exec_when_no_socket(
    tmp_path: Path, monkeypatch
) -> None:
    """``auto`` is today's behaviour wherever no daemon answers: exec."""
    monkeypatch.delenv("E2B_PRIV_HELPER_TRANSPORT", raising=False)
    monkeypatch.setenv("E2B_PRIV_HELPER_SOCKET", str(tmp_path / "absent.sock"))
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)
    supervise = (
        tmp_path / "site-packages" / "sandlock" / "bin" / "sandlock-supervise"
    )
    import envd_service.route_b as route_b

    monkeypatch.setattr(route_b, "default_supervise_bin", lambda: supervise)

    helpers = ph.resolve_priv_helpers(_settings(tmp_path))

    assert helpers is not None
    assert helpers.transport == "exec"
    assert helpers.broker_socket is None


def test_root_paths_include_the_image_cache(tmp_path: Path, monkeypatch) -> None:
    """Decision 2: sandbox secrets live under the image cache, so a non-root
    worker needs it whitelisted -- after the shared volume root."""
    cache = tmp_path / "images"
    monkeypatch.setenv("E2B_IMAGE_CACHE_DIR", str(cache))

    helpers = _helpers(tmp_path, socket_path=tmp_path / "unused.sock")

    assert [str(p) for p in helpers._root_paths()] == [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
        str(cache),
    ]


def test_an_unset_image_cache_does_not_widen_the_whitelist(
    tmp_path: Path, monkeypatch
) -> None:
    """Nothing named, nothing added: the whitelist is what the shape says."""
    monkeypatch.delenv("E2B_IMAGE_CACHE_DIR", raising=False)

    helpers = _helpers(tmp_path, socket_path=tmp_path / "unused.sock")

    assert [str(p) for p in helpers._root_paths()] == [
        str(_workspace(tmp_path)),
        str(_shared(tmp_path)),
    ]


def test_an_unknown_transport_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "telepathy")
    _stub_worker_identity(monkeypatch)
    _install(tmp_path, monkeypatch)

    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.resolve_priv_helpers(_settings(tmp_path))

    assert str(excinfo.value) == (
        "E2B_PRIV_HELPER_TRANSPORT must be 'auto', 'exec' or 'socket' "
        "(got 'telepathy')"
    )
