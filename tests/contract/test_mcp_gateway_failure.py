"""FUP #4 / Task D1 contract: a gateway that dies at startup is visible to the
SDK.

Task 10/11 made envd *log* an early gateway exit (sandbox_id/port/stderr/exit
code) but left the SDK contract alone: ``Sandbox.create(mcp=...)`` still got an
immediate exit-0 from the gateway-start command, so a sandbox whose gateway
never came up looked healthy (and an MCP client saw a bare connection error).

Decision ④ (user, 2026-09-10): the SDK must see the failure. This contract
pins the chosen surface:

* the gateway-start command stays asynchronous (the SDK still must not block
  on the gateway's lifetime) -- but the watcher records the death as a typed
  ``McpGatewayFailure`` on the sandbox's runtime context, with the exact text
  the SDK will see (``mcp gateway failed to start sandbox_id=… port=…
  exit_code=… stderr=…``) and the gateway's own non-zero exit code;
* the next ``commands.run`` fails *before it execs anything*: its whole stderr
  is that recorded text verbatim and its exit code is the gateway's own, so a
  red run cannot be mistaken for a successful command;
* an ``/mcp`` call answers 503 with the same text instead of a proxy error.

Run requirements: Linux + sandlock (the Docker test runner) *and* an
MCP-capable base image (``E2B_BASE_IMAGE=python-mcp:3.14``, built from
``deploy/docker/Dockerfile.mcp-base``) so the gateway script exists inside the
sandbox rootfs -- the same shape requirement as ``tests/contract/test_mcp_netns.py``
documents. The broken entry below is a *config* failure, not a missing-gateway
base image.
"""

from __future__ import annotations

import ast
import sys
import time

import pytest
from tests.conftest import TMP_ROOT, _start_multinode

#: An MCP server entry that cannot exist: the gateway process starts (the
#: interpreter and ``mcp-gateway`` are present) and then dies in
#: ``stdio_client`` with FileNotFoundError, which is exactly the "gateway
#: start failure" Task 10/11 made visible in the worker log only.
BROKEN_MCP_SERVER = {
    "name": "broken",
    "command": "definitely-not-a-real-mcp-server",
}

#: The pinned prefix of the SDK-visible failure text (Step 1 of the task).
FAILURE_PREFIX = "mcp gateway failed to start"


def _linux_sandlock_ready() -> bool:
    if sys.platform != "linux":
        return False
    try:
        import sandlock

        return sandlock.landlock_abi_version() >= 6
    except Exception:
        return False


@pytest.fixture(scope="session")
def mcp_failure_servers():
    if not _linux_sandlock_ready():
        pytest.skip(
            "MCP gateway failure contract needs Linux + sandlock "
            "(run inside the Docker test runner)"
        )
    # ``warm_base_image`` because the SDK does not send ``X-Sandbox-Id``: a
    # cold image would fast-fail the create with 428 before the gateway is
    # ever started. No buildkit dependency -- nothing here builds a template.
    harness = _start_multinode(
        TMP_ROOT / "multinode-mcp-failure",
        1,
        buildkit_addr=None,
        warm_base_image=True,
    )
    yield harness
    harness["_stop"]()


def _opts(harness):
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


def _sdk_probe(sandbox) -> str:
    """One live SDK command, rendered for an assertion message.

    The red run (pre-FUP#4) must document yesterday's behaviour instead of
    just "attribute missing": this probe prints the exit code and stderr the
    SDK actually received.
    """
    from e2b.sandbox.commands.command_handle import CommandExitException

    try:
        result = sandbox.commands.run("echo d1-probe")
    except CommandExitException as exc:
        return (
            "raised CommandExitException "
            f"exit_code={exc.exit_code} stderr={exc.stderr!r}"
        )
    return (
        f"exit_code={result.exit_code} stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def _await_recorded_failure(app, sandbox, *, timeout: float = 30.0):
    """The context + the gateway death the watcher recorded for it.

    Returns ``(ctx, failure)``. The SDK cannot observe the record directly, so
    the test waits for the envd-side state the SDK-visible assertion is then
    compared against -- "stderr contains the watcher-recorded text" is only
    checkable if the recorded text is read somewhere.
    """
    deadline = time.time() + timeout
    ctx = None
    while time.time() < deadline:
        ctx = app.state.runtimes.get(sandbox.sandbox_id)
        failure = getattr(ctx, "mcp_gateway_failure", None)
        if failure is not None:
            return ctx, failure
        time.sleep(0.2)
    raise AssertionError(
        "the sandbox runtime context never recorded an MCP gateway start "
        f"failure (ctx={ctx!r}); SDK probe now: {_sdk_probe(sandbox)}"
    )


def test_mcp_gateway_startup_failure_is_visible_to_the_sdk(mcp_failure_servers):
    """A gateway that dies right after ``Sandbox.create(mcp=...)`` must make
    the next command (and the next ``/mcp`` call) fail with its own reason."""
    import httpx
    from e2b import Sandbox
    from e2b.sandbox.commands.command_handle import CommandExitException

    harness = mcp_failure_servers
    worker_app = harness["worker_apps"][0]
    sandbox = Sandbox.create(mcp=dict(BROKEN_MCP_SERVER), **_opts(harness))
    try:
        # The sandbox is created and the gateway has already died in the
        # background: the failure is recorded against this sandbox's runtime.
        ctx, failure = _await_recorded_failure(worker_app, sandbox)

        # 1. The command the SDK runs after the create must fail, not exit 0.
        with pytest.raises(CommandExitException) as excinfo:
            sandbox.commands.run("echo must-not-run")
        assert excinfo.value.exit_code == failure.exit_code
        assert excinfo.value.stderr == failure.text + "\n"
        assert failure.exit_code != 0

        # 2. The visible text *is* the watcher's record, verbatim: the pinned
        #    prefix, this sandbox's identity and the gateway's own stderr tail
        #    (whose last line is the exact error the MCP server entry caused).
        head, separator, stderr_repr = failure.text.partition(" stderr=")
        assert separator == " stderr="
        assert head == (
            f"{FAILURE_PREFIX} sandbox_id={sandbox.sandbox_id} "
            f"port={ctx.mcp_port} exit_code={failure.exit_code}"
        )
        gateway_stderr = ast.literal_eval(stderr_repr)
        assert gateway_stderr.splitlines()[-1] == (
            "FileNotFoundError: [Errno 2] No such file or directory: "
            f"'{BROKEN_MCP_SERVER['command']}'"
        )

        # 3. An MCP client is not left with a bare proxy error either: the
        #    /mcp route reports the same reason.
        token = sandbox.get_mcp_token()
        assert token
        response = httpx.get(
            f"{harness['sandbox_url'].rstrip('/')}/mcp",
            headers={
                "E2b-Sandbox-Id": sandbox.sandbox_id,
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
        assert response.status_code == 503
        assert response.json() == {"message": failure.text}
    finally:
        sandbox.kill()
