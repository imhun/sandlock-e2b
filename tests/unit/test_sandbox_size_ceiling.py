"""N83 phase 2 / Task 2 (D6): a create names its own size, the ceiling decides.

``POST /sandboxes`` now reads ``cpuCount`` (**cores**) and ``memoryMB``, and the
record it writes is the *single* truth for both: the admission ledger books
``cpu_count * 100`` percent and ``memory_mb`` MiB, and the worker is handed the
same two numbers. That is what closes N84 -- a record that always claimed one
core while the ledger booked whatever ``E2B_DEFAULT_CPU_PERCENT`` said, and a
client's own ``cpuCount``/``memoryMB`` that used to be dropped without a word.

Three refusals are pinned here, and they are deliberately three different
answers:

* a value that is not a positive integer is the caller's own mistake: ``400``,
  named, never clamped and never a ``503``;
* a size above the target node's per-sandbox ceiling is a named ``400`` -- the
  number in the message is the promise of the node the create is checked
  against, so it cannot belong to a different machine;
* the sandbox's own task budget (``maxProcesses``, the deployment's default,
  not a request field) has no ``400`` to earn: a node whose promise is below it
  answers ``503``, naming the node.

The ceilings come from the node record the create lands on: in most cases that
is the in-process ``local://`` node, whose row carries the control plane's own
resolution (``E2B_MAX_SANDBOX_*`` -> **that node's own total** ->
``E2B_DEFAULT_*``, per node and at stamping time -- Task 10), and in the last
two a hand-built registry, because the rule is about the record
and not about which kind of node it describes. A remote node's row gets the
**same** three numbers: ruling R17 (2026-10-07) made the ceiling the control
plane's policy, so the internal API stamps it into every node record at
register/heartbeat instead of storing whatever a worker reported. That is also
why there is no longer a "this node has not reported a ceiling yet" ``503``:
the control plane always knows its own policy.
"""

from __future__ import annotations

import httpx
import pytest

from control_plane.config import Settings as ControlSettings
from control_plane.registry.nodes import NodeRegistry

API = {"X-API-Key": "local-key"}


def _settings(**overrides) -> ControlSettings:
    defaults = dict(
        api_keys=("local-key",),
        default_memory_mb=1024,
        default_cpu_percent=100,
        default_disk_mb=1024,
        default_max_processes=64,
        max_total_memory_mb=8192,
        max_total_cpu_percent=400,
        max_total_disk_mb=10240,
        max_total_processes=2048,
        # Every case below pins the *admission answer*; a create that cannot be
        # admitted must come back as its own refusal instead of waiting for the
        # queue's window to expire.
        create_queue_timeout_s=0,
    )
    defaults.update(overrides)
    return ControlSettings(**defaults)


def _client(control) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    )


async def _create(client: httpx.AsyncClient, **size):
    return await client.post(
        "/sandboxes",
        headers=API,
        json={"templateID": "base", "timeout": 300, **size},
    )


async def _detail(client: httpx.AsyncClient, sandbox_id: str) -> dict:
    resp = await client.get(f"/sandboxes/{sandbox_id}", headers=API)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------- the sizes


async def test_a_create_that_names_no_size_keeps_the_settings_default(make_apps):
    """No ``cpuCount``/``memoryMB``: today's default, in both units."""
    control, _envd = make_apps(control_settings=_settings(default_memory_mb=512))
    async with _client(control) as client:
        resp = await _create(client)
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandboxID"]
        detail = await _detail(client, sandbox_id)

    record = control.state.registry.get(sandbox_id)
    worker = control.state.runtime_registry.get(sandbox_id)
    assert record.cpu_count == 1
    assert record.memory_mb == 512
    assert detail["cpuCount"] == 1
    assert detail["memoryMB"] == 512
    assert worker.cpu_percent == 100
    assert worker.memory_mb == 512


async def test_cpu_count_is_cores_for_the_record_the_ledger_and_the_worker(
    make_apps,
):
    """``cpuCount: 2`` is two cores: 200% booked, 200% handed over, on the record."""
    control, _envd = make_apps(control_settings=_settings())
    async with _client(control) as client:
        resp = await _create(client, cpuCount=2, memoryMB=2048)
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandboxID"]
        detail = await _detail(client, sandbox_id)

    record = control.state.registry.get(sandbox_id)
    node = control.state.nodes.get("local")
    worker = control.state.runtime_registry.get(sandbox_id)
    assert record.cpu_count == 2
    assert record.memory_mb == 2048
    assert detail["cpuCount"] == 2
    assert detail["memoryMB"] == 2048
    assert worker.cpu_percent == 200
    assert worker.memory_mb == 2048
    # N84: admission books exactly what the record holds -- one number, one
    # source (the record), instead of a reserved dimension that came from
    # somewhere else.
    assert node.reserved_cpu_percent == 200
    assert node.reserved_cpu_percent == record.cpu_count * 100
    assert node.reserved_memory_mb == 2048
    assert node.reserved_memory_mb == record.memory_mb


async def test_the_create_default_books_the_same_number_it_hands_over(make_apps):
    """The N84 shape itself: ``E2B_DEFAULT_CPU_PERCENT=200`` used to book 200%
    for admission while the record (and so the sandbox's own quota) stayed at
    one core."""
    control, _envd = make_apps(
        control_settings=_settings(default_cpu_percent=200)
    )
    async with _client(control) as client:
        resp = await _create(client)
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandboxID"]

    record = control.state.registry.get(sandbox_id)
    assert record.cpu_count == 2
    assert control.state.nodes.get("local").reserved_cpu_percent == 200
    assert control.state.runtime_registry.get(sandbox_id).cpu_percent == 200


async def test_a_size_exactly_at_the_ceiling_is_allowed(make_apps):
    """The ceiling is inclusive; a node sized to one sandbox still takes one."""
    control, _envd = make_apps(control_settings=_settings(max_total_cpu_percent=200))
    async with _client(control) as client:
        resp = await _create(client, cpuCount=2)
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandboxID"]

    assert control.state.registry.get(sandbox_id).cpu_count == 2


# ------------------------------------------------------------- the ceilings


async def test_without_the_trio_a_create_above_the_create_default_is_admitted(
    make_apps, monkeypatch
) -> None:
    """Task 10: with no trio the ceiling follows the node's own total, so a
    create larger than the *create* default is the node's business.

    This is the bare shape -- no ``E2B_MAX_SANDBOX_*``, no ``E2B_MAX_TOTAL_*``
    (a bare ``python -m control_plane``, the SDK test-runner, an embedder). Its
    in-process node's own totals are ``IN_PROCESS_NODE_DEFAULT_TOTALS`` (8192
    MiB / 400% / 2048), so ``memoryMB: 2048`` fits with room to spare -- before
    the fix the ceiling silently resolved to ``E2B_DEFAULT_MEMORY_MB`` (1024)
    and this same create was refused with a named 400.
    """
    for name in (
        "E2B_MAX_SANDBOX_CPU_PERCENT",
        "E2B_MAX_SANDBOX_MEMORY_MB",
        "E2B_MAX_SANDBOX_PROCESSES",
    ):
        monkeypatch.delenv(name, raising=False)
    control, _envd = make_apps(
        control_settings=_settings(
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_processes=0,
        )
    )
    local = control.state.nodes.get("local")
    assert local.sandbox_cpu_percent_max == 400
    assert local.sandbox_memory_mb_max == 8192
    assert local.sandbox_processes_max == 2048

    async with _client(control) as client:
        resp = await _create(client, memoryMB=2048)
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandboxID"]

    assert control.state.registry.get(sandbox_id).memory_mb == 2048
    assert local.reserved_memory_mb == 2048


async def test_a_size_above_the_target_nodes_ceiling_is_a_named_400(make_apps):
    """The number in the refusal is the ceiling of the node it was checked
    against (here: an explicit per-sandbox ceiling below the node's total)."""
    control, _envd = make_apps(
        control_settings=_settings(
            max_total_cpu_percent=1600, max_sandbox_cpu_percent=400
        )
    )
    async with _client(control) as client:
        resp = await _create(client, cpuCount=8)
        assert resp.status_code == 400
        assert resp.json() == {
            "code": 400,
            "message": "cpuCount 8 exceeds this node's per-sandbox maximum (4)",
        }

    # A refused create leaves no reservation behind.
    assert control.state.nodes.get("local").reserved_cpu_percent == 0


async def test_memory_over_the_target_nodes_ceiling_is_a_named_400(make_apps):
    control, _envd = make_apps(
        control_settings=_settings(
            max_total_memory_mb=8192, max_sandbox_memory_mb=4096
        )
    )
    async with _client(control) as client:
        resp = await _create(client, memoryMB=8192)
        assert resp.status_code == 400
        assert resp.json() == {
            "code": 400,
            "message": "memoryMB 8192 exceeds this node's per-sandbox maximum (4096)",
        }

    assert control.state.nodes.get("local").reserved_memory_mb == 0


async def test_a_size_no_node_could_host_is_a_named_400_not_a_capacity_503(
    make_apps,
):
    """The deployed lanes cap one sandbox at the node's own total, so a request
    over the ceiling is *also* over every ledger's total. The answer is still
    the named 400: "No resources available" would read as "retry, it may fit
    later", and it never will."""
    control, _envd = make_apps(control_settings=_settings(max_total_cpu_percent=200))
    async with _client(control) as client:
        resp = await _create(client, cpuCount=8)
        assert resp.status_code == 400
        assert resp.json() == {
            "code": 400,
            "message": "cpuCount 8 exceeds this node's per-sandbox maximum (2)",
        }


async def test_an_unknown_promise_keeps_the_fleet_question_quiet(make_apps):
    """A node whose ceiling has not arrived (``0``) is *not* proof that nobody
    could host the size: the fleet question stays quiet and the honest capacity
    answer stands, instead of a 400 quoting some other machine's number."""
    nodes = NodeRegistry()
    nodes.add_local_node(
        node_id="local",
        total_memory_mb=8192,
        total_cpu_percent=200,
        total_disk_mb=10240,
        total_processes=2048,
    )
    nodes.register(
        node_id="worker-old",
        address="http://worker-old:49983",
        total_memory_mb=1024,
        total_cpu_percent=100,
        total_disk_mb=1024,
        total_processes=128,
    )
    control, _envd = make_apps(
        control_settings=_settings(enable_local_node=False),
        control_kwargs={"nodes_registry": nodes},
    )
    async with _client(control) as client:
        resp = await _create(client, cpuCount=8)

    assert resp.status_code == 503
    assert resp.json() == {"code": 503, "message": "No resources available"}


async def test_the_landed_nodes_ceiling_is_the_one_quoted(make_apps):
    """A fleet whose nodes disagree still gets a truthful message: the number is
    the landed node's promise, and its reservation goes straight back.

    R17 makes every node's policy the control plane's, so a fleet that
    disagrees is no longer something a heartbeat can produce -- this case pins
    the *record-level* rule with explicitly built rows, which is what keeps the
    refusal honest if a row ever carries a different number (an embedder, a
    migration, a stale row from another replica).
    """
    nodes = NodeRegistry()
    nodes.add_local_node(
        node_id="local",
        total_memory_mb=8192,
        total_cpu_percent=400,
        total_disk_mb=10240,
        total_processes=2048,
        sandbox_cpu_percent_max=400,
        sandbox_memory_mb_max=8192,
        sandbox_processes_max=2048,
    )
    # Roomier than the in-process node, so placement lands here -- and its own
    # promise is the smaller one.
    nodes.register(
        node_id="worker-small",
        address="http://worker-small:49983",
        total_memory_mb=8192,
        total_cpu_percent=800,
        total_disk_mb=10240,
        total_processes=2048,
        sandbox_ceiling={
            "cpuPercent": 100,
            "memoryMB": 2048,
            "processes": 512,
        },
    )
    control, _envd = make_apps(
        control_settings=_settings(enable_local_node=False),
        control_kwargs={"nodes_registry": nodes},
    )
    async with _client(control) as client:
        resp = await _create(client, cpuCount=2)

    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": "cpuCount 2 exceeds this node's per-sandbox maximum (1)",
    }
    assert nodes.get("worker-small").reserved_cpu_percent == 0


# ----------------------------------------------------- the values themselves


@pytest.mark.parametrize("field", ("cpuCount", "memoryMB"))
@pytest.mark.parametrize("value", (0, -1, 1.5, "2", True, None))
async def test_a_size_that_is_not_a_positive_integer_is_a_named_400(
    make_apps, field, value
):
    """Named, never clamped, never a 503."""
    control, _envd = make_apps(control_settings=_settings())
    async with _client(control) as client:
        resp = await _create(client, **{field: value})

    assert resp.status_code == 400
    assert resp.json() == {
        "code": 400,
        "message": f"{field} must be a positive integer",
    }
    # Nothing was admitted (or reserved) on the way to the refusal.
    assert control.state.nodes.get("local").reserved_cpu_percent == 0
    assert control.state.nodes.get("local").reserved_memory_mb == 0


# --------------------------------------------------- the unknown ceiling (R12)


async def test_a_node_whose_process_promise_is_below_the_default_cannot_size_work(
    make_apps,
):
    """The third dimension on the same rule: the deployment's own per-sandbox
    default must fit the node's promise, or the node cannot size work."""
    control, _envd = make_apps(
        control_settings=_settings(max_total_processes=8192, max_sandbox_processes=32)
    )
    async with _client(control) as client:
        resp = await _create(client)

    assert resp.status_code == 503
    assert resp.json() == {
        "code": 503,
        "message": (
            "the sandbox's 64 maxProcesses exceed node local's per-sandbox "
            "maximum (32)"
        ),
    }
