"""Track F (Task F1): route B from a **non-root** worker, end to end.

The deployed worker runs as uid 65534 with no effective capabilities. Route B
still has to start one ``sandlock-supervise`` per sandbox *as that sandbox's
own host uid*, and E3.2 still has to own the sandbox's ``0700`` workspace --
which a non-root worker can only do through the two file-capability brokers
(``envd_service/priv_helpers.py``, ``deploy/priv/``). This contract drives the
real worker path (control plane create -> agent create -> first exec) and
asserts, in the shape the process actually runs in:

① the worker logs ``route-B instance ready …`` with the pooled host uid (the
   F4 log fix is what makes the line visible at all);
② the slot process is ``sandlock-supervise --uid <that sandbox's uid>``;
③ the two sandboxes' workspaces are owned by **two different** pool uids;
④ a cross-uid write/delete through the shared 1777+sticky volume is refused
   with real kernel semantics (EPERM), i.e. the mediated write really landed
   with the sandbox's own identity;
⑤ the chroot shape's canonical cwd is ``/home/user``.

Run as root it exercises the ``setpriv`` starter, run as uid 65534 with the
brokers installed it exercises ``e2b-slot-spawn``; both must satisfy the same
five assertions, which is exactly the point of the two brokers (a non-root
worker is not a different behaviour, it is the same behaviour with the
privileged step delegated).
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from tests.contract.test_uid_permissions import _result, _run_cmd
from tests.security.conftest import sandlock_ready

POOL_START = 21000
POOL_SIZE = 16
ROUTE_B_LOGGER = "envd_service.executors.sandlock"

# Mirrors tests/contract/test_shared_volume_relative_cwd.py: ⑤ is about the
# image-rootfs (chroot) shape, whose workspace alias is /home/user.
pytestmark = pytest.mark.skipif(
    not sandlock_ready() or not os.environ.get("E2B_BASE_IMAGE"),
    reason=(
        "the route-B contract needs the sandlock wheel (with the supervise "
        "binary) and the image-rootfs shape (E2B_BASE_IMAGE)"
    ),
)


def _envd_settings(workspace: Path) -> EnvdSettings:
    return EnvdSettings(
        executor="sandlock",
        per_sandbox_uid=True,
        uid_pool_start=POOL_START,
        uid_pool_size=POOL_SIZE,
        workspace_base=workspace,
        # Track F: the slot's policy/program documents must live under the
        # broker whitelist (workspace base / shared volume root), otherwise the
        # worker refuses the broker shape at startup by name.
        route_b_tmp_root=workspace / ".route-b",
    )


@pytest.fixture()
def route_b_workspace() -> Path:
    """A worker workspace base on **container-native** storage.

    Not the shared ``workspace`` fixture: that one lives under
    ``E2B_TEST_TMP_ROOT``, which is root-owned in the runner image, so the
    uid-65534 phase of the lane cannot create a test directory under it. Nor
    the repo bind mount: virtiofs cannot record ownership at all, and ③/④ are
    ownership assertions. ``tempfile`` (TMPDIR, container-native) satisfies
    both shapes.
    """
    path = Path(tempfile.mkdtemp(prefix="f1-route-b-"))
    path.chmod(0o711)
    return path


def _make_apps(workspace: Path):
    """Control plane + envd sharing one registry, like ``make_apps`` does."""
    registry = RuntimeRegistry(workspace)
    control = create_control_app(
        settings=ControlSettings(api_keys=("local-key",), create_queue_timeout_s=0),
        runtime_registry=registry,
        workspace_base=workspace,
    )
    envd = create_envd_app(
        settings=_envd_settings(workspace),
        runtime_registry=registry,
        workspace_base=workspace,
    )
    return control, envd


def _supervise_argv(uid: int) -> list[str]:
    """The argv of the live ``sandlock-supervise`` slot for ``uid``."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if not argv or not argv[0].endswith("/sandlock-supervise"):
            continue
        if "--uid" in argv and argv[argv.index("--uid") + 1] == str(uid):
            return argv
    raise AssertionError(
        f"no live 'sandlock-supervise --uid {uid}' slot process found; "
        "route B did not lease a slot for this sandbox"
    )


def _ready_fields(message: str) -> dict[str, str]:
    """Parse the ready line field by field.

    ``channel`` carries ``fd-handoff(pid N)`` -- it contains a space, so this
    is a regex rather than a ``key=value`` token split.
    """
    match = re.match(
        r"^route-B instance ready sandbox_id=(?P<sandbox_id>\S+) "
        r"instance_name=(?P<instance_name>\S+) uid=(?P<uid>\d+) "
        r"slot=(?P<slot>\S+) channel=(?P<channel>.+) "
        r"guest-uid=(?P<guest_uid>\S+)$",
        message,
    )
    assert match is not None, message
    return match.groupdict()


async def test_nonroot_worker_runs_route_b_with_pooled_uids(
    route_b_workspace, caplog
) -> None:
    workspace = route_b_workspace
    caplog.set_level(logging.INFO, logger=ROUTE_B_LOGGER)
    control, envd = _make_apps(workspace)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        created = await client.post(
            "/volumes", headers={"X-API-Key": "local-key"}, json={"name": "shared"}
        )
        assert created.status_code == 201
        vid = created.json()["volumeID"]
        vol_path = workspace / "_volumes" / vid
        mount = [{"name": vid, "path": "mnt/data"}]
        a = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300, "volumeMounts": mount},
        )
        b = await client.post(
            "/sandboxes",
            headers={"X-API-Key": "local-key"},
            json={"templateID": "base", "timeout": 300, "volumeMounts": mount},
        )
        assert (a.status_code, b.status_code) == (201, 201)
        a_payload, b_payload = a.json(), b.json()
        assert a_payload["sandboxID"] != b_payload["sandboxID"]

    registry = envd.state.runtime_registry
    record_a = registry.get(a_payload["sandboxID"])
    record_b = registry.get(b_payload["sandboxID"])
    assert record_a is not None and record_b is not None
    # ③ the host-uid pool is what the slot identity is drawn from, and the two
    # sandboxes get different uids by construction.
    assert (record_a.host_uid, record_b.host_uid) == (POOL_START, POOL_START + 1)
    workspace_a = Path(record_a.workspace_dir)
    workspace_b = Path(record_b.workspace_dir)
    assert (workspace_a.stat().st_uid, workspace_b.stat().st_uid) == (
        POOL_START,
        POOL_START + 1,
    )
    assert stat.S_IMODE(workspace_a.stat().st_mode) == 0o700
    assert stat.S_IMODE(workspace_b.stat().st_mode) == 0o700

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=envd), base_url="http://test"
    ) as client:
        code_a, out_a, err_a = _result(
            await _run_cmd(client, a_payload, "echo owned-by-a > mnt/data/a.txt")
        )
        assert (code_a, out_a, err_a) == (0, b"", b"")
        # The write is what leases A's route-B slot (the first exec creates the
        # instance), so the slot assertions come after it.
        written_by = (vol_path / "a.txt").stat().st_uid
        assert written_by == POOL_START

        # ⑤ the chroot shape's canonical cwd.
        code_pwd, out_pwd, err_pwd = _result(
            await _run_cmd(client, a_payload, "pwd && pwd -P")
        )
        assert (code_pwd, out_pwd, err_pwd) == (0, b"/home/user\n/home/user\n", b"")

        # ④ sticky + per-file DAC: B cannot delete or extend A's file, even
        # though the volume root is world-writable for every tenant.
        code_rm, _out_rm, err_rm = _result(
            await _run_cmd(client, b_payload, "rm mnt/data/a.txt")
        )
        assert code_rm != 0
        assert err_rm == (
            b"rm: cannot remove 'mnt/data/a.txt': Operation not permitted\n"
        )
        code_w, _out_w, err_w = _result(
            await _run_cmd(client, b_payload, "touch mnt/data/a.txt")
        )
        assert code_w == 1
        assert err_w == (
            b"touch: cannot touch 'mnt/data/a.txt': Permission denied\n"
        )
        assert (vol_path / "a.txt").read_text(encoding="utf-8") == "owned-by-a\n"

    # ① the worker's own ready line, with the pooled host uid.
    ready = [
        record.getMessage()
        for record in caplog.records
        if record.name == ROUTE_B_LOGGER
        and record.getMessage().startswith("route-B instance ready")
    ]
    assert len(ready) == 2, ready
    fields = [_ready_fields(message) for message in ready]
    assert sorted(field["uid"] for field in fields) == [
        str(POOL_START),
        str(POOL_START + 1),
    ]
    for field in fields:
        # The slot reports its own workload identity: "host-uid" when the
        # sandbox is the host uid, "uid-0-in-userns" for the F18 self-map.
        assert field["guest_uid"] in ("host-uid", "uid-0-in-userns")
        assert field["channel"].startswith("fd-handoff(pid ")

    # ② the process really is sandlock-supervise at that uid.
    for uid in (POOL_START, POOL_START + 1):
        argv = _supervise_argv(uid)
        assert argv[0].endswith("/sandlock/supervise") or argv[0].endswith(
            "/sandlock-supervise"
        )
        assert argv[argv.index("--policy") + 1].startswith(str(workspace / ".route-b"))


async def test_the_broker_step_is_real_when_this_worker_is_not_root(
    route_b_workspace,
) -> None:
    """The same lease the contract above exercises, pinned at the broker.

    Run as root this is the ``setpriv`` shape (no brokers resolved); run as uid
    65534 with the image's brokers it is ``e2b-slot-spawn`` and the lease has
    to come back with the pooled uid -- the non-root worker's whole route-B
    capability in one assertion pair.
    """
    from envd_service import priv_helpers
    from envd_service.route_b import RouteBConfig, W1SlotPool, default_supervise_bin

    settings = _envd_settings(route_b_workspace)
    priv_helpers.configure_priv_helpers(settings)
    helpers = priv_helpers.active_helpers()
    if os.geteuid() != 0:
        assert helpers is not None, (
            "a non-root worker in this image must resolve the file-capability "
            "brokers (or be told why in the self-check)"
        )
    else:
        assert helpers is None
    config = RouteBConfig.from_settings(settings)
    assert (config.spawner is not None) == (helpers is not None)

    pool = W1SlotPool(
        uid_start=POOL_START,
        size=2,
        tmp_root=Path(settings.route_b_tmp_root),
        spawner=config.spawner,
        supervise_bin=default_supervise_bin(),
    )
    handle = pool.acquire_sync("sbx_broker_probe", {}, name="broker-probe", uid=POOL_START)
    try:
        assert handle.uid == POOL_START
        status = Path(f"/proc/{handle.process.pid}/status").read_text()
        assert "Uid:\t%d\t%d\t%d\t%d" % ((POOL_START,) * 4) in status
        assert "CapEff:\t0000000000000000" in status
    finally:
        pool.release_sync("sbx_broker_probe")
