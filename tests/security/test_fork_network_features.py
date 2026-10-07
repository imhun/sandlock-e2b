"""e2b <-> sandlock fork integration coverage for the netns-free line.

The fork's own hermetic suite covers the sandlock mechanisms; these tests
cover the project-side mapping paths end to end:

* wildcard ``allowOut`` through the unprivileged per-sandbox DNS gateway
  (Block A, no egress proxy / no netns);
* HTTP header injection + ``maskRequestHost`` observed on the wire, plus
  ``${e2b.identity.tokens.*}`` env-token injection (Block B);
* the HTTPS MITM CA splice in image-rootfs mode (executor -> sandlock);
* sandbox children always running unprivileged (no-root requirement).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.security.conftest import route_b_sandbox, sandbox_tmpdir

from envd_service.executors.base import ExecConfig
from envd_service.executors.sandlock import SandlockExecutor

#: The pooled host uid this file's identity contract runs the sandbox at. The
#: value is arbitrary (any uid outside the runner's own), it only has to be the
#: uid the route-B slot and the workspace ownership agree on.
POOLED_UID = 10000


class RecordingOrigin:
    """Minimal origin server recording raw request bytes, replying 200."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.requests: list[bytes] = []
        self._server = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        data = await reader.read(65536)
        self.requests.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()


def _executor(
    ws: str,
    network: dict | None,
    secrets_dir: str | None = None,
) -> SandlockExecutor:
    """A pure sandbox with this network policy, in the deployment's shape.

    Through `route_b_sandbox` rather than hand-built (N15): the pure shape is
    mediated now, and a hand-built one on a root worker is the shape the fork
    refuses (SL-1).
    """
    executor, _ = route_b_sandbox(
        None,
        None,
        workspace=ws,
        enable_network=True,
        network=network,
        secrets_dir=secrets_dir,
    )
    return executor


def _own_identity_executor(workspace: Path, pooled_uid: int) -> SandlockExecutor:
    """The deployed identity shape for route B (§2.4.1, 决定 #1).

    **Host-side the sandbox runs at the pooled uid; inside its namespace it is
    uid 0** (the fork's F18 self-map of the single-entry userns). There is one
    slot-identity shape left (C3, open-issues N52): the pool forks an
    unprivileged child that unshares and polls, and the identity is *granted*
    by writing that child's ``uid_map``/``gid_map`` -- the per-node agent's step
    in production, performed in-process by this lane's reporter when it runs as
    root.
    """
    from envd_service.config import Settings as EnvdSettings
    from envd_service.own_identity import OwnIdentityConfig
    from envd_service.uid_pool import apply_sandbox_ownership
    from tests.security.conftest import _lane_identity_reporter

    base = Path(workspace).parent
    scratch = base / "route-b"
    scratch.mkdir(parents=True, exist_ok=True)
    EnvdSettings(  # the shape the worker resolves at startup, named here too
        per_sandbox_uid=True,
        workspace_base=base,
        own_identity="on",
        uid_pool_start=pooled_uid,
        uid_pool_size=1,
        slot_tmp_root=scratch,
    )
    apply_sandbox_ownership(workspace, pooled_uid)
    return SandlockExecutor(
        workspace_dir=str(workspace),
        base_image=None,
        image_rootfs=None,
        host_uid=pooled_uid,
        per_sandbox_uid=True,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=True,
        network=None,
        sandbox_id="sbx_identity_probe",
        own_identity=OwnIdentityConfig(
            mode="on",
            uid_start=pooled_uid,
            uid_size=1,
            tmp_root=scratch,
            identity_grant="agent-grant",
            identity_reporter=_lane_identity_reporter(pooled_uid, 1),
        ),
    )


def _start_bg_origin(
    host: str, port: int
) -> tuple[RecordingOrigin, threading.Thread, asyncio.AbstractEventLoop, dict[str, asyncio.Event]]:
    """Run a RecordingOrigin on a background loop so it keeps accepting while
    the synchronous e2b SDK blocks the test's event loop."""
    origin = RecordingOrigin(host=host, port=port)
    loop = asyncio.new_event_loop()
    state: dict[str, asyncio.Event] = {}

    async def _serve() -> None:
        await origin.start()
        state["shutdown"] = asyncio.Event()
        await state["shutdown"].wait()

    thread = threading.Thread(
        target=loop.run_until_complete, args=(_serve(),), daemon=True
    )
    thread.start()
    deadline = time.time() + 5
    while origin._server is None and time.time() < deadline:
        time.sleep(0.02)
    return origin, thread, loop, state


async def _run(executor: SandlockExecutor, ws: str, code: str) -> tuple[int, bytes, bytes]:
    probe = ExecConfig(
        cmd=["/usr/local/bin/python3", "-c", code],
        env={},
        cwd=ws,
        stdin_enabled=False,
    )
    proc = await executor.start(probe)
    out = b""
    err = b""
    async for kind, data in proc.output():
        if kind in ("stdout", "pty"):
            out += data
        elif kind == "stderr":
            err += data
    return await proc.exit_code(), out, err


@pytest.fixture()
def loopback_alias():
    """Put 198.18.0.99 (the SSRF-guard-allowed benchmark range) on lo with a
    hosts entry, so a wildcard rule can resolve to a local origin."""
    addr = "198.18.0.99"
    hostname = "api.wild.test"
    add = subprocess.run(
        ["ip", "addr", "add", f"{addr}/32", "dev", "lo"],
        check=False,
        capture_output=True,
        text=True,
    )
    # "Address already assigned" means the address is on lo and usable (a
    # shared-VM netns usually keeps it from an earlier session). Reporting that
    # as "no NET_ADMIN" skipped a test that could really run.
    detail = (add.stderr or add.stdout or "").strip()
    already_there = add.returncode != 0 and (
        "already assigned" in detail.lower() or "file exists" in detail.lower()
    )
    if add.returncode != 0 and not already_there:
        pytest.skip(
            f"cannot put {addr}/32 on lo (needs NET_ADMIN, run with "
            f"--cap-add NET_ADMIN): {detail[:160]}"
        )
    added_by_us = add.returncode == 0
    hosts_line = f"{addr} {hostname}\n"
    with open("/etc/hosts", "a", encoding="utf-8") as f:
        f.write(hosts_line)
    try:
        yield hostname
    finally:
        try:
            with open("/etc/hosts", "r", encoding="utf-8") as f:
                lines = [line for line in f if line != hosts_line]
            with open("/etc/hosts", "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass
        if added_by_us:
            subprocess.run(
                ["ip", "addr", "del", f"{addr}/32", "dev", "lo"],
                check=False,
                capture_output=True,
            )


@pytest.mark.usefixtures("require_sandlock")
async def test_wildcard_allowout_unprivileged_dns_gateway(loopback_alias):
    """``*.wild.test`` is served by the fork's unprivileged per-sandbox DNS
    gateway: the subdomain resolves to a synthetic IP and the supervisor
    connects on-behalf to the real (guard-allowed) destination — no egress
    proxy, no netns, no privileges."""
    hostname = loopback_alias
    origin = RecordingOrigin(host="198.18.0.99")
    await origin.start()
    try:
        ws = str(sandbox_tmpdir())
        executor = _executor(ws, {"allowOut": [f"*.wild.test:{origin.port}"]})
        code = (
            "import urllib.request; "
            f"print(urllib.request.urlopen('http://{hostname}:{origin.port}/', "
            "timeout=10).status)"
        )
        exit_code, out, err = await _run(executor, ws, code)
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        assert origin.requests, "origin must receive the wildcard-tunneled request"

        # The bare apex must NOT match ``*.wild.test`` (fork semantics).
        apex_code = (
            "import urllib.request; "
            f"urllib.request.urlopen('http://wild.test:{origin.port}/', timeout=10)"
        )
        exit_code, _out, _err = await _run(executor, ws, apex_code)
        assert exit_code != 0, "bare apex must not match *.wild.test"
    finally:
        await origin.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_header_inject_and_host_mask_on_the_wire(tmp_path, monkeypatch):
    """Block B observed at the origin: an injected header arrives and the
    wire ``Host`` is rewritten to the mask. Literal and ``${e2b.identity.
    tokens.*}`` sources are exercised together in one rule — every matching
    credential is applied (multiple headers per host)."""
    monkeypatch.setenv("E2B_IDENTITY_TOKEN_openai", "sk-token")
    origin = RecordingOrigin(host="127.0.0.1", port=80)
    await origin.start()
    try:
        ws = str(tmp_path / "ws")
        Path(ws).mkdir()
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        executor = _executor(
            ws,
            {
                "allowOut": ["127.0.0.1:80"],
                "maskRequestHost": "localhost:${PORT}",
                "rules": {
                    "127.0.0.1": [
                        {
                            "transform": {
                                "headers": {
                                    "X-API-Key": "sk-secret",
                                    "X-Token": "${e2b.identity.tokens.openai}",
                                }
                            }
                        }
                    ]
                },
            },
            secrets_dir=str(secrets),
        )
        exit_code, out, err = await _run(
            executor,
            ws,
            "import urllib.request; "
            "print(urllib.request.urlopen('http://127.0.0.1/', timeout=10).status)",
        )
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "200"
        assert origin.requests, "origin must receive the injected request"
        lines = origin.requests[-1].decode("latin-1").split("\r\n")
        headers = {
            k.lower(): v.strip()
            for k, _, v in (line.partition(":") for line in lines[1:] if ":" in line)
        }
        assert headers.get("x-api-key") == "sk-secret", origin.requests[-1]
        assert headers.get("x-token") == "sk-token", origin.requests[-1]
        assert headers.get("host") == "localhost:80", origin.requests[-1]
    finally:
        await origin.stop()


@pytest.mark.usefixtures("require_sandlock")
async def test_sandbox_child_runs_unprivileged():
    """S1.2 identity semantics **in the deployed shape** (§2.4.1, 决定 #1).

    ``E2B_PER_SANDBOX_UID`` + route B: the sandbox's *host* uid is the pooled
    uid and its single-entry namespace maps that uid to 0 (fork F18 self-map),
    so the child sees ``0 0`` while everything it writes on the host belongs
    to the pooled uid. Both halves are asserted below — the in-namespace
    answer and the host-side owner of a file the child created.

    The child still holds no root privileges: Landlock keeps every host path
    outside the workspace unwritable, so a uid-0 child cannot touch host root
    paths.
    """
    from tests.security.conftest import require_mediation_capable

    ws = str(sandbox_tmpdir(suffix="-identity", uid=POOLED_UID))
    executor = _own_identity_executor(Path(ws), POOLED_UID)
    require_mediation_capable(executor)
    try:
        exit_code, out, err = await _run(
            executor, ws, "import os; print(os.getuid(), os.getgid())"
        )
        assert exit_code == 0, err.decode()
        assert out.decode().strip() == "0 0"
        # The identity came from a route-B slot leased at the pooled uid (not
        # from an in-process fallback that would leave the worker's identity).
        assert executor._own_identity_active is True
        assert executor._instance._handle.uid == POOLED_UID

        # Host-side half of the identity: written in the sandbox, owned by the
        # pooled uid (not by the worker, and not by root).
        exit_code, out, err = await _run(
            executor, ws, "open('host-side.txt', 'w').write('x')"
        )
        assert exit_code == 0, err.decode()
        assert (Path(ws) / "host-side.txt").stat().st_uid == POOLED_UID

        # uid 0 inside the namespace must not grant root capabilities: writing
        # to a host system path is denied by the sandbox policy.
        denied_code = "open('/bin/root-cap-probe', 'w').write('x')"
        exit_code, out, err = await _run(executor, ws, denied_code)
        assert exit_code != 0, "uid 0 child must not write host system paths"
    finally:
        executor.close()
        priv_helpers.configure_priv_helpers(EnvdSettings(priv_helpers="off"))


@pytest.mark.usefixtures("require_sandlock")
async def test_https_mitm_ca_spliced_in_image_rootfs(multinode_two_workers):
    """In image-rootfs mode an http_allow rule makes the executor copy the
    image trust bundle into ``.e2b-ca`` and pin ``SSL_CERT_FILE``; sandlock
    appends its ephemeral MITM CA to that bundle, so the workload trusts it."""
    if not os.environ.get("E2B_BASE_IMAGE"):
        pytest.skip("requires E2B_BASE_IMAGE (image-rootfs sandboxes)")

    harness = multinode_two_workers
    from e2b import Sandbox

    sandbox = Sandbox.create(
        network={
            "allow_out": ["127.0.0.1:80"],
            "rules": {"127.0.0.1": [{"transform": {"headers": {"X-K": "v"}}}]},
        },
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    try:
        result = sandbox.commands.run("cat .e2b-ca/ca-certificates.crt")
        assert result.exit_code == 0, result.error
        assert "BEGIN CERTIFICATE" in result.stdout, result.stdout

        # The executor pins SSL_CERT_FILE to the chroot-visible path
        # (/workspace is the sandbox cwd; /home/user is the legacy alias).
        env = sandbox.commands.run(
            "python3 -c \"import os; print(os.environ.get('SSL_CERT_FILE',''))\""
        )
        assert env.stdout.strip() == "/workspace/.e2b-ca/ca-certificates.crt"
    finally:
        sandbox.kill()


@pytest.mark.usefixtures("require_sandlock")
async def test_sdk_iam_token_injected_as_jwt(multinode_two_workers):
    """SDK workload identity: ``iam=...`` plus a transform callable that
    references ``ctx.iam.tokens`` results in a minted JWT-SVID on the wire
    (``Authorization: Bearer <jwt>`` with the requested audience)."""
    import base64
    import json

    from e2b import Sandbox

    harness = multinode_two_workers
    origin, thread, loop, state = _start_bg_origin("127.0.0.1", 80)
    try:
        sandbox = Sandbox.create(
            iam={
                "tokens": {
                    "openai": {
                        "audience": "test-aud",
                        "token_type": "JWT-SVID",
                    }
                }
            },
            network={
                "allow_out": ["127.0.0.1:80"],
                "rules": {
                    "127.0.0.1": [
                        {
                            "transform": lambda ctx: {
                                "headers": {
                                    "Authorization": (
                                        f"Bearer {ctx.iam.tokens['openai']}"
                                    )
                                }
                            }
                        }
                    ]
                },
            },
            api_url=harness["api_url"],
            sandbox_url=harness["sandbox_url"],
            api_key="local-key",
        )
        try:
            result = sandbox.commands.run(
                "python3 -c \"import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1/', timeout=10).status)\""
            )
            assert result.exit_code == 0, result.error
            assert origin.requests, "origin must receive the iam-authenticated request"
            raw = origin.requests[-1].decode("latin-1")
            auth = next(
                line
                for line in raw.split("\r\n")
                if line.lower().startswith("authorization:")
            )
            token = auth.split("Bearer ", 1)[1].strip()
            header, payload, signature = token.split(".")
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            assert claims["aud"] == "test-aud"
            assert signature
        finally:
            sandbox.kill()
    finally:
        origin._server.close()
        loop.call_soon_threadsafe(state["shutdown"].set)
        thread.join(timeout=5)
        loop.close()
