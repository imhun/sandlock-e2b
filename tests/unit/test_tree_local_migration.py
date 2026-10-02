"""Task 3: 沙箱树本地化 + 迁移经共享中转（N57）。

翻转 ``E2B_TREES_SHARED`` 之后，沙箱树只存在于**它自己那个节点**的盘上：

* ``_export_sandbox_archive`` 的 tar 必须**流式**穿过控制面（今天两端都是
  ``resp.content`` / ``read_bytes()``，一棵 1 GiB 的树就是一个 2 GiB 的进程），
  带按字节的具名上限（``E2B_TREE_COPY_MAX_BYTES``），超限具名拒绝；
* 源节点不可达 = 树不可达，必须具名（``source-node-unreachable``），不是 502 泛化；
* 目标节点的导入必须**要么整棵树、要么没有树**（半棵树在"源节点已经放手"之后
  就是数据损坏的样子）；
* 迁移判据是 ``settings.trees_shared``，不是共享根的真假 —— 重切之后共享根仍然
  设着（``_snapshots`` / ``_migrate`` / ``_volumes`` 在它下面）。

这里用**真的** envd agent（ASGI）当两个节点：迁移的导出/导入端点是被测对象，
只有 worker 侧的 provision / stop / destroy 三个 hop 是桩（那些不是本任务的代码）。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

import control_plane.api.sandboxes as sandboxes
from control_plane.app import create_app as create_control_app
from control_plane.c3_agent_client import AgentClientError
from control_plane.config import Settings as ControlSettings
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from control_plane.registry.manager import SandboxRegistry
from control_plane.registry.nodes import NodeRegistry
from control_plane.registry.volumes import VolumeRegistry
from control_plane.worker_identity_source import StaticWorkerIdentitySource
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common import paths

KEY_A = "key-node-a"
KEY_B = "key-node-b"
IP_A = "10.0.0.1"
IP_B = "10.0.0.2"
NODE_A = "node_a"
NODE_B = "node_b"
HOST_A = "node-a"
HOST_B = "node-b"
ENDPOINT_A = NodeEndpoint(f"http://{HOST_A}:49983", IP_A)
ENDPOINT_B = NodeEndpoint(f"http://{HOST_B}:49983", IP_B)
WORKER_UID = 65534
WORKER_GID = 65534
SANDBOX = "sbx_local_tree"
INTERNAL_KEY = "fleet-key"
#: The real client, captured before any test patches the module attribute.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
KUBECTL = shutil.which("kubectl")
K8S_BASE = REPO_ROOT / "deploy" / "k8s"
K8S_OVERLAY = REPO_ROOT / "deploy" / "k8s-k0s"
LOCAL_TREE_ROOT = "/var/lib/e2b/workspaces"
SHARED_ROOT = "/var/lib/e2b-sandboxes"


# --------------------------------------------------------------------------
# 夹具：一个控制面 + 两个真的 envd agent（ASGI），共享一个中转目录
# --------------------------------------------------------------------------


def _control_settings(workspace: Path, *, trees_shared: bool) -> ControlSettings:
    return ControlSettings(
        api_keys=("local-key",),
        internal_api_key=INTERNAL_KEY,
        internal_api_keys=(),
        internal_node_keys={KEY_A: NODE_A, KEY_B: NODE_B},
        max_sandboxes=200,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        workspace_base=workspace / "control-plane-tree-root",
        state_base=workspace / "control-plane-state",
        shared_workspace_root=str(workspace / "shared"),
        trees_shared=trees_shared,
    )


class _NodeApps(httpx.AsyncBaseTransport):
    """Dial each "node" by the host in the control plane's URL.

    The migration endpoints are the tested surface, so the two nodes are the
    real envd apps (`/agent/sandboxes/<id>/{export,import}`) mounted on their own
    tree roots. A host nobody registered raises ``ConnectError`` -- that is the
    unreachable-source case, not a stubbed 502.
    """

    def __init__(self, apps: dict[str, object]) -> None:
        self.apps = dict(apps)
        self.requests: list[dict[str, object]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append(
            {
                "method": request.method,
                "host": host,
                "path": request.url.path,
                "transfer-encoding": request.headers.get("transfer-encoding"),
                "content-length": request.headers.get("content-length"),
            }
        )
        app = self.apps.get(host)
        if app is None:
            raise httpx.ConnectError(f"connection refused: {host}", request=request)
        return await httpx.ASGITransport(app=app).handle_async_request(request)


class _RecordingAgentClient:
    """The CP→agent file-op channel (``C3AgentClient``'s shape we use)."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.calls: list[dict] = []
        self.refuse = refuse

    async def rm(self, **kwargs) -> dict:
        self.calls.append({"op": "rm", **kwargs})
        if self.refuse is not None:
            raise AgentClientError(self.refuse)
        return {"op": "rm", "path": kwargs["path"]}


class _HeaderRecorder:
    """Record the ASGI request headers of the (only) import call."""

    def __init__(self, app: object, sink: list[dict[str, str]]) -> None:
        self.app = app
        self.sink = sink

    async def __call__(self, scope, receive, send):  # pragma: no cover - glue
        if scope["type"] == "http":
            self.sink.append(
                {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            )
        await self.app(scope, receive, send)


def _agent_app(
    disk: Path,
    shared: Path,
    headers: list[dict[str, str]] | None = None,
    *,
    tree_copy_max_bytes: int | None = None,
):
    settings = EnvdSettings(
        executor="local",
        workspace_base=disk,
        shared_volume_root=str(shared),
        internal_api_key=INTERNAL_KEY,
        **(
            {}
            if tree_copy_max_bytes is None
            else {"tree_copy_max_bytes": tree_copy_max_bytes}
        ),
    )
    app = create_envd_app(
        settings=settings, runtime_registry=RuntimeRegistry(disk)
    )
    return _HeaderRecorder(app, headers) if headers is not None else app


def _node_layout(workspace: Path) -> dict[str, Path]:
    """Two node-local tree roots and one shared store, all under ``workspace``."""
    disk_a = workspace / "node-a-disk" / "workspaces"
    disk_b = workspace / "node-b-disk" / "workspaces"
    shared = workspace / "shared"
    for path in (disk_a, disk_b, shared / "_migrate", shared / "_snapshots"):
        path.mkdir(parents=True, exist_ok=True)
    return {"a": disk_a, "b": disk_b, "shared": shared}


def _control_app(
    workspace: Path,
    layout: dict[str, Path],
    *,
    trees_shared: bool,
    c3_agent_client=None,
    redis_client=None,
):
    settings = _control_settings(workspace, trees_shared=trees_shared)
    app = create_control_app(
        settings=settings,
        registry=SandboxRegistry(settings),
        nodes_registry=NodeRegistry(
            heartbeat_timeout=600.0, redis_client=redis_client
        ),
        volumes_registry=VolumeRegistry(workspace / "_volumes_base"),
        workspace_base=settings.workspace_base,
        node_address_resolver=StaticAddressResolver(
            {NODE_A: ENDPOINT_A, NODE_B: ENDPOINT_B}
        ),
        # The node-agent channel the source release uses; a stub here (never the
        # real resolver) because these tests have no cluster.
        c3_agent_client=c3_agent_client,
        worker_identity_source=StaticWorkerIdentitySource(
            {
                NODE_A: (WORKER_UID, WORKER_GID),
                NODE_B: (WORKER_UID, WORKER_GID),
            }
        ),
    )
    return app


def _client(app, *, source_ip: str = IP_A):
    return _REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=app, client=(source_ip, 44444)),
        base_url="http://control",
    )


async def _register(app, *, node_id: str, key: str, source_ip: str) -> None:
    async with _client(app, source_ip=source_ip) as client:
        resp = await client.post(
            "/internal/nodes/register",
            headers={"X-Internal-Key": key},
            json={
                "nodeID": node_id,
                "totalMemoryMB": 8192,
                "totalCPUPercent": 800,
                "totalDiskMB": 16384,
                "totalProcesses": 512,
                "workerUID": WORKER_UID,
                "workerGID": WORKER_GID,
            },
        )
    assert resp.status_code == 200, resp.text


def _enroll(app, *, node_id: str = NODE_A) -> None:
    registry = app.state.registry
    record = registry.create(
        template_id="base",
        sandbox_id=SANDBOX,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.node_id = node_id
    record.host_uid = 10007
    registry.save(record)


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _tree_binary(root: Path, name: str, size: int) -> None:
    """An incompressible file, so a *compressed* tar really is bigger than a cap."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))


def _install_node_hop(
    monkeypatch: pytest.MonkeyPatch,
    layout: dict[str, Path],
    *,
    destroy_acknowledged: bool = True,
) -> dict:
    """The three worker-side hops this task does not test; record them."""
    seen: dict = {"provision": [], "destroy": [], "stop": []}

    async def _provision(request, record, node, settings, snapshot, mounts, **kw):
        seen["provision"].append(node.node_id)

    async def _stop(request, record, node):
        seen["stop"].append(node.node_id)
        return True

    async def _destroy(request, record, node, keep_files=False, **kw):
        seen["destroy"].append({"node": node.node_id, "keep_files": keep_files})
        # The real hop returns a _TeardownOutcome; the source release checks it
        # (a refused teardown is a tree left behind, named on the record).
        return sandboxes._TeardownOutcome(acknowledged=destroy_acknowledged)

    monkeypatch.setattr(sandboxes, "_provision_remote", _provision)
    monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop)
    monkeypatch.setattr(sandboxes, "_destroy_on_node", _destroy)
    return seen


def _install_transport(monkeypatch: pytest.MonkeyPatch, app: _NodeApps) -> None:
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=app)

    # ``control_plane.api.sandboxes`` imports ``httpx`` inside the functions
    # that dial a node, so the patch goes on the module itself -- the one every
    # ``import httpx`` in this process resolves to.
    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _migrate(app, *, node_id: str = NODE_B):
    async with _client(app, source_ip=IP_A) as client:
        return await client.post(
            f"/sandboxes/{SANDBOX}/migrate",
            headers={"X-API-Key": "local-key"},
            json={"nodeID": node_id},
        )


def _staging_leftovers(shared: Path) -> list[str]:
    return sorted(
        str(path.relative_to(shared))
        for path in (shared / "_migrate").rglob("*")
        if path.is_file() or path.is_dir()
    )


# --------------------------------------------------------------------------
# 1. 树在节点本地：共享卷不再是树根（清单与介质归属）
# --------------------------------------------------------------------------


def _rendered(overlay: Path) -> str:
    proc = subprocess.run(
        [KUBECTL, "kustomize", str(overlay)],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _rendered_workload(rendered: str, kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == kind
        and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}: {len(matches)}"
    return matches[0]


def _env(container: dict) -> dict:
    return {e["name"]: e.get("value") for e in container["env"]}


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_a_local_tree_is_not_visible_from_the_shared_volume() -> None:
    """翻转之后，三个组件都必须把树根指到节点本地盘上。

    今天三份清单都把 ``E2B_WORKSPACE_BASE`` 指在共享卷里
    （``<shared>/workspaces``），而 ``E2B_TREES_SHARED`` 还写着 ``1``。
    介质翻转是"本地卷 + 判据"两件事一起：只改指向不换判据会让迁移静默建空树，
    只换判据不挂盘会让第一次建箱就 ENOENT。
    """
    for rendered in (_rendered(K8S_BASE), _rendered(K8S_OVERLAY)):
        agent = _rendered_workload(rendered, "DaemonSet", "e2b-c3-agent")
        face_b = next(
            c
            for c in agent["spec"]["template"]["spec"]["containers"]
            if c["name"] == "maint"
        )
        env = _env(face_b)
        assert env["E2B_WORKSPACE_BASE"] == LOCAL_TREE_ROOT
        assert not env["E2B_WORKSPACE_BASE"].startswith(env["E2B_SHARED_VOLUME_ROOT"])
        mounts = {m["name"]: m for m in face_b["volumeMounts"]}
        assert mounts["workspace-root"]["mountPath"] == LOCAL_TREE_ROOT
        pod = agent["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod["volumes"]}
        assert volumes["workspace-root"]["hostPath"] == {
            "path": LOCAL_TREE_ROOT,
            "type": "DirectoryOrCreate",
        }

    worker = _rendered_workload(_rendered(K8S_OVERLAY), "StatefulSet", "e2b-worker")
    env = _env(worker["spec"]["template"]["spec"]["containers"][0])
    assert env["E2B_WORKSPACE_BASE"] == LOCAL_TREE_ROOT
    assert env["E2B_SHARED_VOLUME_ROOT"] == SHARED_ROOT
    assert not env["E2B_WORKSPACE_BASE"].startswith(SHARED_ROOT)
    pod = worker["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["workspace-root"] == LOCAL_TREE_ROOT
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["workspace-root"]["hostPath"] == {
        "path": LOCAL_TREE_ROOT,
        "type": "DirectoryOrCreate",
    }

    plane = _rendered_workload(_rendered(K8S_OVERLAY), "Deployment", "control-plane")
    env = _env(plane["spec"]["template"]["spec"]["containers"][0])
    assert env["E2B_WORKSPACE_BASE"] == LOCAL_TREE_ROOT
    assert env["E2B_TREES_SHARED"] == "0"
    assert env["E2B_SHARED_WORKSPACE_ROOT"] == SHARED_ROOT
    shared = [
        m
        for m in plane["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        if m["name"] == "shared"
    ]
    assert [
        m for m in shared if m.get("subPath", "").startswith("workspaces")
    ] == []


# --------------------------------------------------------------------------
# 2. 迁移经共享中转：内容随人走，源节点的树放手
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_moves_the_tree_through_the_shared_store(
    workspace, monkeypatch
) -> None:
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    headers: list[dict[str, str]] = []
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"], headers),
        }
    )
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    app = _control_app(workspace, layout, trees_shared=False)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 200, resp.text
    assert resp.json()["nodeID"] == NODE_B
    # ...and the success path must NOT give the target's reservation back: the
    # sandbox serves there now, so N59's single release point is the *failure*
    # path only. (A release here would be the under-counting direction.)
    record = app.state.registry.get(SANDBOX)
    assert record.node_id == NODE_B
    assert app.state.nodes.get(NODE_B).reserved_memory_mb == record.memory_mb
    # 内容真的落在目标节点的本地盘上，且与源一致。
    assert (layout["b"] / SANDBOX / "workspace" / "kept.txt").read_text() == "hello"
    # 源节点的树被放手（keep_files=False），不是"改个记录、树留在原地"。
    assert seen["destroy"] == [{"node": NODE_A, "keep_files": False}]
    assert seen["provision"] == [NODE_B]
    # 在途是流式的：整个 tar 没有作为 bytes 进控制面内存（那样会带 Content-Length）。
    # The recorder is installed on the *target* only, so every header set it
    # saw is an import call.
    assert len(headers) == 1, headers
    assert headers[0].get("transfer-encoding") == "chunked"
    assert "content-length" not in headers[0]
    # 中转落点是共享根上的 `_migrate`，迁移结束后不留任何东西。
    assert _staging_leftovers(layout["shared"]) == []


# --------------------------------------------------------------------------
# 3. 判据不是共享根：部署清单说什么，迁移就走哪条路
# --------------------------------------------------------------------------


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
@pytest.mark.asyncio
async def test_the_migration_judge_is_not_the_shared_root(
    workspace, monkeypatch
) -> None:
    """``E2B_SHARED_WORKSPACE_ROOT`` 设着 + ``E2B_TREES_SHARED=0`` ⇒ 必须导出/导入。

    这一条从**部署清单**里取判据：清单写 ``1`` 时，共享根设着会让迁移走"原地
    共享、只切路由"（不导 tar、目标节点建空树）；写 ``0`` 时它必须走 tar 通道。
    共享卷上放一棵**诱饵树**，目标节点拿到的内容只能是源节点导出的那份。
    """
    plane = _rendered_workload(_rendered(K8S_OVERLAY), "Deployment", "control-plane")
    env = _env(plane["spec"]["template"]["spec"]["containers"][0])
    trees_shared = env["E2B_TREES_SHARED"].strip().lower() in {"1", "true", "yes", "on"}

    layout = _node_layout(workspace)
    decoy = layout["shared"] / "workspaces" / SANDBOX / "workspace"
    decoy.mkdir(parents=True)
    (decoy / "decoy.txt").write_text("shared-volume-decoy", encoding="utf-8")
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "from-the-node"})
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    _install_node_hop(monkeypatch, layout)
    app = _control_app(workspace, layout, trees_shared=trees_shared)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 200, resp.text
    assert trees_shared is False, "the shipped manifest must name the local shape"
    assert (layout["b"] / SANDBOX / "workspace" / "kept.txt").read_text() == "from-the-node"
    assert not (layout["b"] / SANDBOX / "workspace" / "decoy.txt").exists()
    assert (decoy / "decoy.txt").read_text() == "shared-volume-decoy"


# --------------------------------------------------------------------------
# 4. 传输失败：目标节点不留半棵树，记录留在源节点
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_transfer_leaves_neither_a_half_tree_nor_a_record(
    workspace, monkeypatch
) -> None:
    """目标节点的导入被具名拒绝时：记录回滚、目标只留下它原来那棵树。

    目标节点的树里先放一份 ``stale.txt``：今天的导入会**先删掉它、再解包**，
    解包一失败就留下一个半成品目录；记录这时候已经切到目标节点了，所以"记录
    指向的节点上有一棵不完整的树"。修好之后失败发生在发布之前，旧树原样留着。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    _tree_binary(layout["a"] / SANDBOX, "workspace/big.bin", 256 * 1024)
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            # 4 KiB 的具名上限：tar（256 KiB 不可压数据）比它大，导入必须具名
            # 拒绝而不是在内存里堆整棵树。
            HOST_B: _agent_app(
                layout["b"], layout["shared"], tree_copy_max_bytes=4096
            ),
        }
    )
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    app = _control_app(workspace, layout, trees_shared=False)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 413, resp.text
    assert resp.json()["message"].startswith("tree-copy-too-large")
    # 记录回到源节点；目标节点只留下它原来那棵树（没有半棵树）。
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    # 目标节点上没有半棵树：要么整棵，要么没有 —— 连一个暂存目录都不许留。
    assert not (layout["b"] / SANDBOX).exists()
    assert [p.name for p in layout["b"].iterdir()] == []
    # 源节点的树没有被删（迁移没成功），中转也不留残留。
    assert (layout["a"] / SANDBOX / "workspace" / "kept.txt").is_file()
    assert _staging_leftovers(layout["shared"]) == []
    # The import is refused before the target is provisioned, so the only
    # provision in this run is the rollback putting the source back on duty.
    assert seen["provision"] == [NODE_A]


# --------------------------------------------------------------------------
# 5. 源节点不可达：具名拒绝，不是 502 泛化
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_migration_from_an_unreachable_source_is_refused_by_name(
    workspace, monkeypatch
) -> None:
    """本地化之后源节点掉线 = 树不可达，而且没有事后补救的路。

    导出端点在源节点上，所以这个状态必须有自己的名字（``source-node-unreachable``），
    而不是一句"Node X unavailable"：操作员看到它就知道该做什么（节点回来之前，
    这棵树只能等）。记录必须留在源节点，目标节点一个字节都不该收到。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps({HOST_B: _agent_app(layout["b"], layout["shared"])})
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)

    async def _stop_unreachable(request, record, node):
        # The source node does not answer: the control plane cannot even stop a
        # runtime there, let alone export the tree.
        return False

    monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop_unreachable)
    app = _control_app(workspace, layout, trees_shared=False)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 502, resp.text
    assert resp.json()["message"].startswith("source-node-unreachable")
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    assert not (layout["b"] / SANDBOX).exists()
    assert seen["provision"] == []
    assert seen["destroy"] == []
    assert _staging_leftovers(layout["shared"]) == []


@pytest.mark.asyncio
async def test_a_source_whose_node_row_is_gone_is_refused_by_the_same_name(
    workspace, monkeypatch
) -> None:
    """同一个具名拒绝必须覆盖"节点行也没了"的时序。

    源节点掉线先变成 ``unhealthy``（心跳窗口），再被"空且长期失联"的清理摘掉
    （`NodeRegistry._prune_empty_unhealthy`）。两个时刻说的是同一件事 —— 树在
    那台节点的盘上、够不着 —— 所以答案必须是同一个名字，而不是"节点未找到"那种
    泛化 502：操作员靠这个名字决定是不是还要等节点回来。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps({HOST_B: _agent_app(layout["b"], layout["shared"])})
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    app = _control_app(workspace, layout, trees_shared=False)
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)
    app.state.nodes.remove(NODE_A)

    resp = await _migrate(app)

    assert resp.status_code == 502, resp.text
    assert resp.json()["message"].startswith("source-node-unreachable")
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    assert not (layout["b"] / SANDBOX).exists()
    assert seen["provision"] == []
    assert seen["destroy"] == []


@pytest.mark.asyncio
async def test_a_source_that_stops_but_cannot_export_is_refused_by_the_same_name(
    workspace, monkeypatch
) -> None:
    """导出握手失败也必须走同一个名字（裁定第 3 条的另一半）。

    源节点可以先应答"停运行时"，然后在**导出**那一步掉线（agent 在导出中途死掉、
    或那台节点上根本没有能应答的 agent）。两条路说的是同一件事：树在那台机器的盘上、
    够不着 —— 名字必须是同一个，目标节点一个字节都不许收到、记录不许动。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    # No entry for HOST_A: the source's export GET is a connection error.
    nodes = _NodeApps({HOST_B: _agent_app(layout["b"], layout["shared"])})
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    agent_client = _RecordingAgentClient()
    app = _control_app(
        workspace, layout, trees_shared=False, c3_agent_client=agent_client
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 502, resp.text
    assert resp.json()["message"].startswith("source-node-unreachable")
    # The stop was acknowledged (that hop did not fail); the export did.
    assert seen["stop"] == [NODE_A]
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    assert not (layout["b"] / SANDBOX).exists()
    assert agent_client.calls == []
    assert _staging_leftovers(layout["shared"]) == []


@pytest.mark.asyncio
async def test_a_named_refusal_gives_the_target_reservation_back(
    workspace, monkeypatch
) -> None:
    """N59：具名拒绝必须把**目标节点的预约**还回去，两个台账都要。

    2026-10-02 的现场（`0.1.0-905`/`908`）：`migrate` 在 `_stop_source_runtime`
    **之前**就 `reserve_node(target)`（内存视图 + Redis 台账各记一份），而
    `source-node-unreachable` 走外层回滚 —— 那条回滚只把记录指回源节点、重建源的运行时，
    **从不 `release_quota(target)`**。于是每一次拒绝白留一个节点名额：worker-0 的 Redis
    台账 **3072 MB = 3 × 1024**，`/internal/nodes` 的内存视图同样虚高，最后
    `MULTI-NODE` / `DEPLOYMENT` 两条冒烟全在 `503`（控制者手工清台账 + 杀 8 个孤儿沙箱
    之后才恢复）。

    这里两个台账都在（节点注册表挂 fakeredis）：拒绝之后目标两边都必须是 0，源节点自己
    的预约原样保留。
    """
    fakeredis = pytest.importorskip("fakeredis")
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps({HOST_B: _agent_app(layout["b"], layout["shared"])})
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)

    async def _stop_unreachable(request, record, node):
        return False

    monkeypatch.setattr(sandboxes, "_stop_source_runtime", _stop_unreachable)
    app = _control_app(
        workspace,
        layout,
        trees_shared=False,
        redis_client=fakeredis.FakeRedis(),
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)
    record = app.state.registry.get(SANDBOX)
    dims = {
        "memory_mb": record.memory_mb,
        "cpu_percent": record.cpu_count * 100,
        "disk_mb": record.disk_size_mb,
        "processes": record.max_processes,
    }
    app.state.nodes.reserve_node(NODE_A, **dims)

    resp = await _migrate(app)

    assert resp.status_code == 502, resp.text
    assert resp.json()["message"].startswith("source-node-unreachable")
    assert seen["provision"] == []
    assert seen["destroy"] == []
    # The target's reservation is back in **both** ledgers...
    assert app.state.nodes.get(NODE_B).reserved_memory_mb == 0
    assert app.state.nodes.get(NODE_B).reserved_cpu_percent == 0
    assert app.state.nodes.get(NODE_B).reserved_disk_mb == 0
    assert app.state.nodes.get(NODE_B).reserved_processes == 0
    assert app.state.nodes._quota_store.get(NODE_B) == {
        "memory": 0,
        "cpu": 0,
        "disk": 0,
        "processes": 0,
    }
    # ...while the source keeps its own (the sandbox is still there).
    assert app.state.nodes.get(NODE_A).reserved_memory_mb == record.memory_mb
    assert app.state.nodes._quota_store.get(NODE_A)["memory"] == record.memory_mb


@pytest.mark.asyncio
async def test_the_source_tree_is_released_through_that_nodes_agent(
    workspace, monkeypatch
) -> None:
    """迁移成功后**源节点的树必须真的消失**，而且要走 CP→agent 那条通道。

    2026-10-02 的实测（`0.1.0-905`，见 task-3-report.md §10）抓到 happy path 泄漏：
    F1 把记录先切到目标节点（那是为了让目标的 file-op 通过作用域检查），于是源节点
    worker 的 DELETE 向控制面申请 `remove-workspace` 时，控制面按**记录**判它
    "属于目标节点"，拒绝了自己刚下的指令（403）；迁移不检查那个返回值，照样
    `200 migrated` —— 源节点的树原地留下，而孤儿回收按记录判它 `protected`，
    永不回收（每个迁移留一整棵树在旧节点 68–75 GiB 的盘上）。

    修法：源节点的释放不再绕 worker 的 DELETE，而是控制面**自己派生路径**、直接
    指令那台节点的 agent（root，白名单含树根）删——与 materialize /
    scope-slot-document 同一条通道。目标侧的清理仍走 worker（记录指着它）。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    agent_client = _RecordingAgentClient()
    app = _control_app(
        workspace,
        layout,
        trees_shared=False,
        c3_agent_client=agent_client,
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 200, resp.text
    assert (layout["b"] / SANDBOX / "workspace" / "kept.txt").read_text() == "hello"
    assert agent_client.calls == [
        {
            "op": "rm",
            "node_id": NODE_A,
            "sandbox_id": SANDBOX,
            "path": str(app.state.workspace_base / SANDBOX),
        }
    ]
    # ...and the source's tree was *not* left to the worker DELETE (the hop the
    # control plane's own scoping refuses once the record names the target).
    assert seen["destroy"] == []


@pytest.mark.asyncio
async def test_a_refused_source_release_is_named_and_never_rolls_back(
    workspace, monkeypatch
) -> None:
    """释放被拒 ⇒ 迁移仍然成功（沙箱已经在目标服务），但**必须具名**。

    这是那条裁定的真正要点：释放失败 **不能** 静默成 200 —— 它要在记录里留下
    `stale-tree-on-former-source`，让操作员知道旧节点上还有一棵不会被自愈回收的树
    （记录还认领这个 id）。同时它绝不能走回滚：回滚会删掉**目标**的树并用
    `snapshot_id=None` 重建源 —— 那正是"整棵树消失"的路径。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout)
    agent_client = _RecordingAgentClient(refuse="agent rm refused (test)")
    app = _control_app(
        workspace,
        layout,
        trees_shared=False,
        c3_agent_client=agent_client,
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 200, resp.text
    # The target really has the tree (the migration is committed)...
    assert (layout["b"] / SANDBOX / "workspace" / "kept.txt").read_text() == "hello"
    record = app.state.registry.get(SANDBOX)
    assert record.node_id == NODE_B
    # ...and the refusal is on the record, not swallowed.
    lines = [entry["line"] for entry in record.logs]
    assert any("migrated to node node_b" in line for line in lines), lines
    assert any(
        "source tree retained" in line
        and "stale-tree-on-former-source" in line
        and NODE_A in line
        for line in lines
    ), lines
    # No rollback ran: the target was not torn down, and the worker DELETE (the
    # hop that would 403) was never used for the source.
    assert seen["destroy"] == []


@pytest.mark.asyncio
async def test_a_write_failure_after_the_record_save_cannot_reach_the_release(
    workspace, monkeypatch
) -> None:
    """成功路上的写失败**不能**跑在释放之后 —— 那是全量数据丢失的窗口。

    顺序是：导入 → provision → 记录落盘（"migrated to node X"）→ 才释放源节点的树。
    反过来（释放 → 记录落盘）时，落盘失败会触发回滚：回滚删掉**目标**的树、把记录指回
    源节点、用 `snapshot_id=None` 重建源 —— 而源节点的树刚刚被释放步骤删掉，暂存的 tar
    也在 `finally` 里丢掉 ⇒ 数据没了。这条用例让**成功路上的第二次 `registry.save` 抛错**，
    断言：释放一次都没发生（agent rm 调用为空）、记录回到源节点、源节点的树还在。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    _install_node_hop(monkeypatch, layout)
    agent_client = _RecordingAgentClient()
    app = _control_app(
        workspace,
        layout,
        trees_shared=False,
        c3_agent_client=agent_client,
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)
    # Give the source a real reservation, so "the rollback re-provisions the
    # source but only releases the target's quota" is observable.
    record = app.state.registry.get(SANDBOX)
    app.state.nodes.reserve_node(
        NODE_A,
        memory_mb=record.memory_mb,
        cpu_percent=record.cpu_count * 100,
        disk_mb=record.disk_size_mb,
        processes=record.max_processes,
    )

    real_save = app.state.registry.save
    saves = {"n": 0}

    def _save(record):
        saves["n"] += 1
        if saves["n"] == 2:  # the F1 re-point is #1; this is the commit write
            raise RuntimeError("record save failed (test)")
        return real_save(record)

    monkeypatch.setattr(app.state.registry, "save", _save)

    with pytest.raises(RuntimeError, match="record save failed"):
        await _migrate(app)

    # The release had not run when the write failed, so the source tree is
    # untouched and the rollback path has something to fall back to.
    assert agent_client.calls == []
    assert (layout["a"] / SANDBOX / "workspace" / "kept.txt").read_text() == "hello"
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    # ...and the target's partial tree was cleaned up by the rollback.
    assert not (layout["b"] / SANDBOX).exists()
    # Accounting stays symmetric: the source reservation is only returned
    # *after* the record write, so a failure before it leaves the source held.
    assert app.state.nodes.get(NODE_A).reserved_memory_mb == record.memory_mb
    assert app.state.nodes.get(NODE_B).reserved_memory_mb == 0


@pytest.mark.asyncio
async def test_the_worker_fallback_release_is_named_when_the_teardown_is_refused(
    workspace, monkeypatch
) -> None:
    """没有 agent 通道时的回落：worker 的 DELETE **被拒也要具名**，不能当成功。

    `create_app` 在真实部署里总会构造 `C3AgentClient`，所以这条回落是 `local://`
    源与显式传 `c3_agent_client=None` 的嵌入/测试形状；它保留是因为 worker DELETE 正是
    这次修复之前唯一用过的路径 —— 而现在它的回答是被**读**的。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    seen = _install_node_hop(monkeypatch, layout, destroy_acknowledged=False)
    app = _control_app(workspace, layout, trees_shared=False)  # no agent client
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 200, resp.text
    assert seen["destroy"] == [{"node": NODE_A, "keep_files": False}]
    record = app.state.registry.get(SANDBOX)
    assert record.node_id == NODE_B
    lines = [entry["line"] for entry in record.logs]
    assert any(
        "source tree retained" in line and "stale-tree-on-former-source" in line
        for line in lines
    ), lines


# --------------------------------------------------------------------------
# 6. 孤儿回收：本地化之后 agent 仍然看得见自己的树
# --------------------------------------------------------------------------


@pytest.mark.skipif(KUBECTL is None, reason="kubectl needed to render the kustomize overlay")
def test_the_orphan_sweep_still_sees_a_local_tree(tmp_path: Path) -> None:
    """巡检是 agent 的活，它扫的就是它挂着的那个树根。

    翻转之后 face B 挂的是节点本地的树根（要用 hostPath 真的挂上），
    ``E2B_C3_AGENT_SCAN`` 仍然开着：看不见树的 agent = 永远不收敛的孤儿回收。
    """
    agent = _rendered_workload(_rendered(K8S_BASE), "DaemonSet", "e2b-c3-agent")
    face_b = next(
        c
        for c in agent["spec"]["template"]["spec"]["containers"]
        if c["name"] == "maint"
    )
    env = _env(face_b)
    assert env["E2B_C3_AGENT_SCAN"] == "on"
    assert env["E2B_WORKSPACE_BASE"] == LOCAL_TREE_ROOT
    mounts = {m["name"]: m["mountPath"] for m in face_b["volumeMounts"]}
    assert mounts["workspace-root"] == LOCAL_TREE_ROOT

    # 行为那一半：扫描器只看自己的树根，共享卷上的同 id 目录不是它的事。
    from c3_agent.config import Settings as AgentSettings
    from c3_agent.scan import InventoryScanner

    local = tmp_path / "node-local" / "workspaces"
    local.mkdir(parents=True)
    (local / "sbx_orphan").mkdir()
    (local / "sbx_orphan" / "sandbox.json").write_text("{}", encoding="utf-8")
    shared = tmp_path / "shared" / "workspaces"
    shared.mkdir(parents=True)
    (shared / "sbx_decoy").mkdir()

    scanner = InventoryScanner(
        settings=AgentSettings(workspace_base=str(local)),
        reporter=None,
    )
    assert scanner.scan_once() == ["sbx_orphan"]


# --------------------------------------------------------------------------
# 附加钉子（同一条裁定的另外两面）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transfer_over_the_byte_cap_is_refused_by_name(
    workspace, monkeypatch
) -> None:
    """在途上限是**按字节**、具名，且越界即拒（不是把整棵树读进来再说）。

    控制面的限额是 2 GiB、两端各 120 s，按 NAS 上 13 ms/文件算约 4600 个文件就
    到顶；本地化之后这条路径第一次真的跑起来。上限默认 1 GiB（= 一个沙箱的默认
    配额），``0`` = 不限。
    """
    layout = _node_layout(workspace)
    _tree(layout["a"] / SANDBOX, {"workspace/kept.txt": "hello"})
    _tree_binary(layout["a"] / SANDBOX, "workspace/big.bin", 256 * 1024)
    nodes = _NodeApps(
        {
            HOST_A: _agent_app(layout["a"], layout["shared"]),
            HOST_B: _agent_app(layout["b"], layout["shared"]),
        }
    )
    _install_transport(monkeypatch, nodes)
    _install_node_hop(monkeypatch, layout)
    settings = _control_settings(workspace, trees_shared=False)
    settings.tree_copy_max_bytes = 4096
    app = create_control_app(
        settings=settings,
        registry=SandboxRegistry(settings),
        nodes_registry=NodeRegistry(heartbeat_timeout=600.0),
        volumes_registry=VolumeRegistry(workspace / "_volumes_base"),
        workspace_base=settings.workspace_base,
        node_address_resolver=StaticAddressResolver(
            {NODE_A: ENDPOINT_A, NODE_B: ENDPOINT_B}
        ),
        worker_identity_source=StaticWorkerIdentitySource(
            {NODE_A: (WORKER_UID, WORKER_GID), NODE_B: (WORKER_UID, WORKER_GID)}
        ),
    )
    await _register(app, node_id=NODE_A, key=KEY_A, source_ip=IP_A)
    await _register(app, node_id=NODE_B, key=KEY_B, source_ip=IP_B)
    _enroll(app)

    resp = await _migrate(app)

    assert resp.status_code == 413, resp.text
    assert resp.json()["message"].startswith("tree-copy-too-large")
    assert app.state.registry.get(SANDBOX).node_id == NODE_A
    assert not (layout["b"] / SANDBOX).exists()
    assert _staging_leftovers(layout["shared"]) == []


@pytest.mark.asyncio
async def test_a_failed_import_does_not_leave_a_half_tree_on_the_target(
    tmp_path: Path,
) -> None:
    """导入要么整棵树、要么没有树：半棵树比"没有树"危险得多。

    目标节点的树先有 ``stale.txt``，收到的 tar 是截断的（解包到一半就失败）。
    今天这一步先把旧树删掉再就地解包，于是留下一个半成品目录；修好之后它解到
    同文件系统的暂存目录里，成功了才发布。
    """
    import os

    disk = tmp_path / "node-b-disk" / "workspaces"
    shared = tmp_path / "shared"
    (disk / SANDBOX / "workspace").mkdir(parents=True)
    (disk / SANDBOX / "workspace" / "stale.txt").write_text("keep", encoding="utf-8")
    (shared / "_migrate").mkdir(parents=True)
    app = _agent_app(disk, shared)

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        data = b"restored"
        info = tarfile.TarInfo("workspace/note.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    truncated = payload.getvalue()[: len(payload.getvalue()) // 2]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post(
            f"/agent/sandboxes/{SANDBOX}/import",
            headers={"X-Internal-Key": INTERNAL_KEY},
            content=truncated,
        )

    assert resp.status_code == 400, resp.text
    assert (disk / SANDBOX / "workspace" / "stale.txt").read_text() == "keep"
    assert sorted(p.name for p in (disk / SANDBOX / "workspace").iterdir()) == [
        "stale.txt"
    ]
    assert [p.name for p in (disk / SANDBOX).iterdir()] == ["workspace"]
    assert os.listdir(shared / "_migrate") == []


def test_the_migrate_transfer_copy_is_not_the_nodes_landing_path() -> None:
    """控制面的中转副本与节点侧的落点不能是同一个文件名。

    节点侧（agent 的导出/导入）落 ``<shared>/_migrate/<id>.tar.gz``：导出端一边
    写一边把它流回控制面，导入端把收到的 body 落到同一个名字再解包。控制面如果
    也写这个名字，流式写就会在源节点还在读它的时候把文件截断。
    """
    cp = paths.migrate_transfer_path(Path("/ws"), "sbx_1", shared_root=Path("/sh"))
    node = paths.migrate_staging_dir(Path("/ws"), shared_root=Path("/sh")) / "sbx_1.tar.gz"
    assert cp == Path("/sh") / "_migrate" / "control-plane" / "sbx_1.tar.gz"
    assert cp != node
