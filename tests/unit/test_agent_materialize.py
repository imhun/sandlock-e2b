"""Face B's one-call create materialization.

The control plane sends **one** instruction and the agent does the whole
materialization (tree, snapshot copy, volume slices, ownership hand-over), so
the create stops paying a worker→CP→agent→CP→worker round trip per step. Two
properties are the whole reason this is safe, and the tests below are organised
around them:

* the instruction arrives on the **existing** authenticated CP→agent channel
  (the same one ``chown``/``rm``/``walk`` use) and is addressed to this agent's
  own node (D12);
* every path in it is the control plane's derivation, and this agent
  re-resolves each one against its own four roots -- the same ``realpath``
  discipline the relayed ops use, run independently (C3 §14.4: the two do not
  replace each other).

The copy is the dangerous part (design §4.3.1) and has its own section below.
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any, Mapping

import asyncio
import httpx
import pytest

from c3_agent import materialize
from c3_agent.app import create_app
from c3_agent.config import Settings
from c3_agent.fileops import AgentFileOpRefusal

AGENT_TOKEN = "agent-token-0123456789"
HOST = "node_a"
UID_X = 10007
WORKER_GID = 65534
SANDBOX = "sbx_grant01"
OTHER_SANDBOX = "sbx_grant02"
SNAPSHOT = "snap_0123456789abcdef"


class _RecordingRunner:
    """The seam: records one ``e2b-maint`` invocation instead of exec'ing it."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self._fail = fail

    def run(self, argv: list[str], *, env: Mapping[str, str]) -> str:
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        if self._fail is not None:
            raise AgentFileOpRefusal(self._fail)
        return ""


class _Agent:
    """One test's agent: its settings, its runner, its app."""

    def __init__(self, workspace: Path, *, runner=None) -> None:
        self.workspace = workspace
        self.workspace_base = workspace / "workspaces"
        self.state_base = workspace / "state"
        self.shared_root = workspace / "volumes"
        for path in (self.workspace_base, self.state_base, self.shared_root):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(
            token=AGENT_TOKEN,
            node_id=HOST,
            workspace_base=str(self.workspace_base),
            state_base=str(self.state_base),
            shared_volume_root=str(self.shared_root),
            uid_pool_start=10000,
            uid_pool_size=1000,
            maint_path="/usr/lib/e2b-priv/e2b-maint",
        )
        self.runner = runner if runner is not None else _RecordingRunner()
        # ``inventory=None``: this lane is not about the scan loop, and the
        # shipped default would start one.
        self.app = create_app(
            settings=self.settings, maint_runner=self.runner, inventory=None
        )

    def tree(self, sandbox_id: str = SANDBOX) -> Path:
        return self.workspace_base / sandbox_id

    def snapshot_fs(self, snapshot_id: str = SNAPSHOT) -> Path:
        return self.workspace_base / "_snapshots" / snapshot_id / "fs"


def _instruction(
    *,
    sandbox_id: str = SANDBOX,
    tree: dict[str, Any] | None = None,
    worker: dict[str, int] | None = None,
    slices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One instruction, shaped exactly as the control plane sends it.

    No ticket: the credential is the ``X-Internal-Key`` header every relayed
    op already carries, and the *content* is the control plane's own
    derivation -- which this agent re-checks (design §4.2).
    """
    return {
        "sandbox_id": sandbox_id,
        "worker": worker if worker is not None else {"uid": 65534, "gid": WORKER_GID},
        "tree": tree
        if tree is not None
        else {
            "path": "",
            "subdir": "workspace",
            "mode": "0770",
            "uid": UID_X,
            "gid": WORKER_GID,
        },
        "slices": list(slices or []),
    }


def _plan_tree(agent: _Agent, sandbox_id: str = SANDBOX, **overrides) -> dict[str, Any]:
    tree = {
        "path": str(agent.tree(sandbox_id)),
        "subdir": "workspace",
        "mode": "0770",
        "uid": UID_X,
        "gid": WORKER_GID,
    }
    tree.update(overrides)
    return tree


async def _post(agent: _Agent, body: dict):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://agent"
    ) as client:
        return await client.post(
            f"/internal/nodes/{HOST}/agent/materialize",
            json=body,
            headers={"X-Internal-Key": AGENT_TOKEN},
        )


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_a_valid_instruction_creates_and_chowns_the_tree(workspace: Path) -> None:
    agent = _Agent(workspace)

    resp = await _post(
        agent, _instruction(tree=_plan_tree(agent))
    )

    assert resp.status_code == 200
    root = agent.tree()
    assert root.is_dir()
    assert (root / "workspace").is_dir()
    assert resp.json() == {
        "op": "materialize",
        "sandboxID": SANDBOX,
        "tree": {
            "path": str(root),
            "subdir": "workspace",
            "mode": "0770",
            "uid": UID_X,
            "gid": WORKER_GID,
            "created": True,
        },
        "slices": [],
    }
    assert agent.runner.calls == [
        [
            agent.settings.maint_path,
            "chown",
            "--uid",
            str(UID_X),
            "--gid",
            str(WORKER_GID),
            "--recursive",
            "--path",
            str(root),
        ]
    ]


@pytest.mark.asyncio
async def test_the_worker_identity_reaches_the_child_environment(
    workspace: Path,
) -> None:
    """The mode, the gid and the identity all come from the instruction."""
    agent = _Agent(workspace)
    plan_gid = 12345

    resp = await _post(
        agent,
        _instruction(
            tree=_plan_tree(agent, gid=plan_gid),
            worker={"uid": 65534, "gid": plan_gid},
        ),
    )

    assert resp.status_code == 200
    root = agent.tree()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o770
    assert stat.S_IMODE(os.stat(root / "workspace").st_mode) == 0o770
    assert agent.runner.calls == [
        [
            agent.settings.maint_path,
            "chown",
            "--uid",
            str(UID_X),
            "--gid",
            str(plan_gid),
            "--recursive",
            "--path",
            str(root),
        ]
    ]
    # ``--gid`` is checked by the binary against the worker's own gid, so the
    # child must be told the same number the instruction names.
    assert agent.runner.envs[0]["E2B_BROKER_WORKER_GID"] == str(plan_gid)
    # ...and the uid too: unlike the relayed file ops, this instruction carries
    # the worker's whole verified identity, so ``maint_env`` writes both halves
    # (``c3_agent.fileops.maint_env``).
    assert agent.runner.envs[0]["E2B_BROKER_WORKER_UID"] == "65534"


# ----------------------------------------------------------------- refusals


@pytest.mark.asyncio
async def test_a_path_outside_the_roots_is_refused_named(workspace: Path) -> None:
    agent = _Agent(workspace)
    outside = agent.workspace_base / ".." / ".." / "etc"

    resp = await _post(
        agent, _instruction(tree=_plan_tree(agent, path=str(outside)))
    )

    assert resp.status_code == 400
    assert "path-outside-roots" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_instruction_for_another_host_is_refused_named(
    workspace: Path,
) -> None:
    """The instruction is addressed to a node -- and this agent is not it."""
    agent = _Agent(workspace)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://agent"
    ) as client:
        resp = await client.post(
            "/internal/nodes/node_b/agent/materialize",
            json=_instruction(tree=_plan_tree(agent)),
            headers={"X-Internal-Key": AGENT_TOKEN},
        )

    assert resp.status_code == 403
    assert resp.json() == {
        "error": (
            "request is addressed to node node_b, but this agent is node node_a"
        )
    }
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_unknown_op_is_still_refused_named(workspace: Path) -> None:
    """The op whitelist did not get looser: ``materialize`` is a new entry."""
    agent = _Agent(workspace)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://agent"
    ) as client:
        resp = await client.post(
            f"/internal/nodes/{HOST}/agent/materialize-everything",
            json=_instruction(tree=_plan_tree(agent)),
            headers={"X-Internal-Key": AGENT_TOKEN},
        )

    assert resp.status_code == 404
    assert resp.json() == {"error": "unknown agent op 'materialize-everything'"}
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_instruction_without_the_token_is_refused(workspace: Path) -> None:
    agent = _Agent(workspace)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://agent"
    ) as client:
        resp = await client.post(
            f"/internal/nodes/{HOST}/agent/materialize",
            json=_instruction(tree=_plan_tree(agent)),
        )

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_a_busy_agent_answers_busy_without_touching_the_tree(
    workspace: Path,
) -> None:
    """Over its own budget, this op says so -- it does not queue in silence.

    Materialization can take tens of seconds on the shared NAS, and the agent
    answers face A's ``grant-slot`` from the same anyio thread pool, so an
    unbounded number of copies would be a way to starve slot grants (design
    §4.6). Over the budget: a named 503, before anything is created.
    """
    released = threading.Event()
    entered = threading.Event()

    class _SlowRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, argv: list[str], *, env: Mapping[str, str]) -> str:
            self.calls += 1
            entered.set()
            released.wait(timeout=10)
            return ""

    runner = _SlowRunner()
    agent = _Agent(workspace, runner=runner)
    agent.settings.materialize_max_concurrency = 1
    agent.settings.materialize_busy_timeout_s = 0.2
    agent.app = create_app(
        settings=agent.settings, maint_runner=runner, inventory=None
    )

    first = asyncio.create_task(
        _post(agent, _instruction(tree=_plan_tree(agent, "sbx_busy_a")))
    )
    await asyncio.to_thread(entered.wait, 5)
    second = await _post(
        agent, _instruction(tree=_plan_tree(agent, "sbx_busy_b"))
    )
    released.set()
    assert (await first).status_code == 200

    assert second.status_code == 503
    assert "materialize is busy" in second.json()["error"]
    # The refused instruction never reached the tree or the privileged step.
    assert agent.tree("sbx_busy_b").exists() is False
    assert runner.calls == 1


# -------------------------------------------------------- two sandboxes, one node


@pytest.mark.asyncio
async def test_two_sandboxes_do_not_interfere(workspace: Path) -> None:
    agent = _Agent(workspace)

    first = await _post(
        agent, _instruction(tree=_plan_tree(agent, SANDBOX))
    )
    second = await _post(
        agent,
        _instruction(
            sandbox_id=OTHER_SANDBOX, tree=_plan_tree(agent, OTHER_SANDBOX)
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert agent.tree(SANDBOX).is_dir()
    assert agent.tree(OTHER_SANDBOX).is_dir()
    assert len(agent.runner.calls) == 2
    assert agent.runner.calls[0][-1] == str(agent.tree(SANDBOX))
    assert agent.runner.calls[1][-1] == str(agent.tree(OTHER_SANDBOX))


# ------------------------------------------- the hardened recursive copy
#
# This is the design's most dangerous piece (§4.3.1). The copy runs with the
# agent's privileges, and it has **two** sides a hostile tree can attack: the
# snapshot it reads (a link must be recreated, never dereferenced) and the
# destination it merges into (the previous incarnation of the sandbox could
# write there, so a link in the target is a way out of the tree). Neither is a
# new privilege -- it is a new privileged code path, which is why each rule
# gets its own test rather than one "copy works" happy path.


def _snapshot(agent: _Agent, entries: dict[str, Any]) -> Path:
    """A snapshot payload: ``fs/`` with the given entries.

    A ``bytes``/``str`` value is a file, ``"-> <target>"`` is a symlink, and a
    dict is one nested directory level -- the three shapes the copy has to get
    right on the source side.
    """
    fs = agent.snapshot_fs()
    fs.mkdir(parents=True, exist_ok=True)
    for name, value in entries.items():
        path = fs / name
        if isinstance(value, bytes):
            path.write_bytes(value)
        elif isinstance(value, str) and value.startswith("-> "):
            path.symlink_to(value[3:])
        elif isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:  # a mapping: one nested directory level
            path.mkdir(parents=True, exist_ok=True)
            for child, child_value in value.items():
                (path / child).write_text(child_value, encoding="utf-8")
    return fs


def _relative_entries(root: Path) -> list[tuple[str, str]]:
    """Every entry under ``root`` as ``(relative path, kind)``, sorted.

    ``os.walk(followlinks=False)`` and not ``rglob``: the trees under test
    contain links, and the comparison must not walk them.
    """
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames + filenames:
            path = base / name
            if path.is_symlink():
                kind = "link"
            elif path.is_dir():
                kind = "dir"
            else:
                kind = "file"
            found.append((str(path.relative_to(root)), kind))
    return sorted(found)


@pytest.mark.asyncio
async def test_a_snapshot_lands_at_the_tree_root(workspace: Path) -> None:
    """``fs/`` is a copy of the tree **root**, so the merge lands there.

    The snapshot side makes that true (``agent_create_snapshot`` copies
    ``workspace_base/<id>`` into ``<snapshot>/fs``) and so does the control
    plane's in-process shape (``expand_to(snapshot, workspace_dir)``). Copying
    into ``<root>/<subdir>`` instead put the whole sandbox one level too deep
    (``workspace/workspace/...``) -- a tree no other code path produces.
    """
    agent = _Agent(workspace)
    _snapshot(agent, {"workspace": {"kept.txt": "kept\n"}})

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 200
    tree = agent.tree()
    assert (tree / "workspace" / "kept.txt").read_text(encoding="utf-8") == "kept\n"
    assert (tree / "workspace" / "workspace").exists() is False


@pytest.mark.asyncio
async def test_the_fast_path_and_the_fallback_produce_the_same_tree(
    workspace: Path,
) -> None:
    """Same snapshot, same tree -- whichever side materializes it.

    The fallback is ``shutil.copytree(snapshot_fs, tree, dirs_exist_ok=True,
    symlinks=True)``: that call *is* the behavioural spec of the merge (it is
    the worker's own path, unchanged by this work), so a tree this agent
    produces has to match it entry for entry, links included.
    """
    agent = _Agent(workspace)
    _snapshot(
        agent,
        {
            "workspace": {"kept.txt": "kept\n"},
            "link": "-> /etc/passwd",
            "nested": {"deep.txt": "deep\n"},
        },
    )

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 200
    reference = workspace / "copytree"
    shutil.copytree(
        agent.snapshot_fs(), reference, dirs_exist_ok=True, symlinks=True
    )
    assert _relative_entries(agent.tree()) == _relative_entries(reference)


@pytest.mark.asyncio
async def test_a_symlink_in_the_snapshot_is_recreated_not_followed(
    workspace: Path,
) -> None:
    """The source side: a link is an entry to recreate, never a door to open."""
    agent = _Agent(workspace)
    outside = workspace / "outside"
    outside.mkdir()
    prey = outside / "prey.txt"
    prey.write_text("untouched\n", encoding="utf-8")
    before = (prey.read_text(encoding="utf-8"), os.stat(prey).st_mtime_ns)
    _snapshot(
        agent,
        {
            "link": "-> /etc/passwd",
            "escape": f"-> {outside}",
            "real": "hello\n",
        },
    )

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 200
    # The merge target is the tree root: ``fs/`` carries the root's contents.
    target = agent.tree()
    assert os.path.islink(target / "link") is True
    assert os.readlink(target / "link") == "/etc/passwd"
    assert os.path.islink(target / "escape") is True
    assert os.readlink(target / "escape") == str(outside)
    # The link was *recreated*, not walked: the pointed-at tree is untouched.
    assert (prey.read_text(encoding="utf-8"), os.stat(prey).st_mtime_ns) == before
    assert (target / "real").read_text(encoding="utf-8") == "hello\n"


@pytest.mark.asyncio
async def test_a_symlinked_destination_segment_is_refused_named(
    workspace: Path,
) -> None:
    """The destination side: the sandbox could have left a link in the tree."""
    agent = _Agent(workspace)
    outside = workspace / "outside"
    outside.mkdir()
    target = agent.tree()
    target.mkdir(parents=True)
    (target / "sub").symlink_to(outside)
    _snapshot(agent, {"sub": {"file": "payload\n"}})

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 400
    assert "destination-is-a-symlink" in resp.json()["error"]
    # Nothing was written through the link, and nothing was chowned either:
    # a refused materialization must not leave a half-claimed tree.
    assert sorted(os.listdir(outside)) == []
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_ordinary_merge_keeps_existing_files(workspace: Path) -> None:
    """``dirs_exist_ok`` semantics survive (migration/rebuild keeps files)."""
    agent = _Agent(workspace)
    target = agent.tree()
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("from the previous incarnation\n", encoding="utf-8")
    (target / "same.txt").write_text("stale\n", encoding="utf-8")
    _snapshot(agent, {"same.txt": "fresh\n"})

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 200
    assert (target / "keep.txt").read_text(encoding="utf-8") == (
        "from the previous incarnation\n"
    )
    assert (target / "same.txt").read_text(encoding="utf-8") == "fresh\n"
    # Every directory in the tree stays ``0770`` (the worker's own
    # ``apply_sandbox_ownership`` rule): the worker is the data-plane owner and
    # must be able to write one level down.
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o770


@pytest.mark.asyncio
async def test_a_file_where_a_directory_belongs_is_refused_named(
    workspace: Path,
) -> None:
    """A type conflict is refused, not resolved by deleting the sandbox's file."""
    agent = _Agent(workspace)
    target = agent.tree()
    target.mkdir(parents=True)
    (target / "sub").write_text("I am a file\n", encoding="utf-8")
    _snapshot(agent, {"sub": {"file": "payload\n"}})

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 400
    assert "already-exists-as-a-file" in resp.json()["error"]
    assert (target / "sub").read_text(encoding="utf-8") == "I am a file\n"


@pytest.mark.asyncio
async def test_a_partial_copy_is_reported_as_failure(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-copied tree must never read as success (§4.3.1 requirement 3)."""
    agent = _Agent(workspace)
    _snapshot(agent, {"one": "1\n", "two": "2\n"})

    def _boom(src_fd: int, dst_fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(materialize, "_splice", _boom)

    resp = await _post(
        agent,
        _instruction(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs()))),
    )

    assert resp.status_code == 502
    assert "partial-copy" in resp.json()["error"]
    assert agent.runner.calls == []


# ------------------------------------------------------------- the slices
#
# A mounted volume with a per-sandbox quota gets its own slice directory
# (``<volume root>/<sandbox id>``, ``0770 <sandbox uid>:<worker gid>``). It is
# materialization like the tree is, so it travels in the same plan and is
# created and handed over by the same call -- and, like every other path in
# the plan, it is only ever the control plane's derivation: a path outside
# this agent's roots is refused, and a slice the plan does not name is never
# created.


def _slice_entry(
    agent: _Agent,
    volume: str = "data",
    *,
    sandbox_id: str = SANDBOX,
    path: str | None = None,
    uid: int = UID_X,
    gid: int = WORKER_GID,
) -> dict[str, Any]:
    root = agent.shared_root / volume
    root.mkdir(parents=True, exist_ok=True)
    return {
        "volume": volume,
        "path": str(path if path is not None else root / sandbox_id),
        "uid": uid,
        "gid": gid,
    }


@pytest.mark.asyncio
async def test_the_agent_creates_and_chowns_every_slice(workspace: Path) -> None:
    agent = _Agent(workspace)
    slices = [_slice_entry(agent, "data"), _slice_entry(agent, "media")]

    resp = await _post(
        agent, _instruction(tree=_plan_tree(agent), slices=slices)
    )

    assert resp.status_code == 200
    root = agent.tree()
    for entry in slices:
        assert Path(entry["path"]).is_dir()
        assert stat.S_IMODE(os.stat(entry["path"]).st_mode) == 0o770
    assert agent.runner.calls == [
        [
            agent.settings.maint_path,
            "chown",
            "--uid",
            str(UID_X),
            "--gid",
            str(WORKER_GID),
            "--recursive",
            "--path",
            str(root),
        ],
        [
            agent.settings.maint_path,
            "chown",
            "--uid",
            str(UID_X),
            "--gid",
            str(WORKER_GID),
            "--recursive",
            "--path",
            slices[0]["path"],
        ],
        [
            agent.settings.maint_path,
            "chown",
            "--uid",
            str(UID_X),
            "--gid",
            str(WORKER_GID),
            "--recursive",
            "--path",
            slices[1]["path"],
        ],
    ]


@pytest.mark.asyncio
async def test_a_slice_path_outside_the_volume_root_is_refused_named(
    workspace: Path,
) -> None:
    agent = _Agent(workspace)
    outside = workspace / "elsewhere" / SANDBOX
    slices = [_slice_entry(agent, "data", path=str(outside))]

    resp = await _post(
        agent, _instruction(tree=_plan_tree(agent), slices=slices)
    )

    assert resp.status_code == 400
    assert "path-outside-roots" in resp.json()["error"]
    assert outside.exists() is False
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_a_slice_not_in_the_plan_is_never_created(workspace: Path) -> None:
    """The op does what the plan says and nothing else -- no local derivation."""
    agent = _Agent(workspace)
    volume_root = agent.shared_root / "data"
    volume_root.mkdir(parents=True)
    (volume_root / "other-sandbox").mkdir()

    resp = await _post(agent, _instruction(tree=_plan_tree(agent)))

    assert resp.status_code == 200
    assert sorted(os.listdir(volume_root)) == ["other-sandbox"]
    assert len(agent.runner.calls) == 1
