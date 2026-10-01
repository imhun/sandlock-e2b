"""Task 3: face B's grant entry -- six checks, then one materialization.

The create path hands its own node's agent a signed plan
(``POST /internal/grants/file-op``, body ``{"grant": ...}``) instead of paying
a worker→CP→agent→CP→worker round trip per step. Routing the plan *around* the
control plane is only safe because the plan is the control plane's own signed
derivation, and because this route re-checks everything itself:

    ① signature over ``E2B_C3_AGENT_TOKEN``;
    ② ``host`` == this agent's own node identity (D12);
    ③ ``iat <= now <= exp``, with a hard lifetime ceiling;
    ④ single use -- a ``jti`` is spent the moment it is accepted;
    ⑤ the op is the one verb this route knows;
    ⑥ every path re-resolves against this agent's own four roots.

⑤ and ⑥ are what make the plan's *contents* trustworthy rather than merely
authentic: ⑥ is the same ``realpath`` + four-root discipline the control-plane
relay uses, run independently, and neither layer replaces the other (C3 §14.4).

The tests below are ordered by that list: one happy path, then one refusal per
way the entry could be made to accept something it should not.
"""

from __future__ import annotations

import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest

from c3_agent import materialize
from c3_agent.app import create_app
from c3_agent.config import Settings
from c3_agent.fileops import AgentFileOpRefusal
from gateway_common.create_grant import OP, VERSION, mint

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


def _grant(
    *,
    sandbox_id: str = SANDBOX,
    tree: dict[str, Any] | None = None,
    op: str = OP,
    host: str = HOST,
    jti: str | None = None,
    iat: int | None = None,
    exp: int | None = None,
    slices: list[dict[str, Any]] | None = None,
) -> str:
    """A signed plan, shaped exactly as the control plane mints one."""
    now = int(time.time())
    payload = {
        "v": VERSION,
        "host": host,
        "sandbox_id": sandbox_id,
        "op": op,
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
        "jti": jti if jti is not None else secrets.token_hex(8),
        "iat": now if iat is None else iat,
        "exp": (now + 10) if exp is None else exp,
    }
    return mint(payload, secret=AGENT_TOKEN)


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
        return await client.post("/internal/grants/file-op", json=body)


# ---------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_a_valid_grant_creates_and_chowns_the_tree(workspace: Path) -> None:
    agent = _Agent(workspace)

    resp = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent))}
    )

    assert resp.status_code == 200
    root = agent.tree()
    assert root.is_dir()
    assert (root / "workspace").is_dir()
    assert resp.json()["materialized"]["tree"]["path"] == str(root)
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
async def test_the_mode_and_group_come_from_the_plan(workspace: Path) -> None:
    """Nothing here is the agent's own choice -- not the mode, not the gid."""
    agent = _Agent(workspace)
    plan_gid = 12345

    resp = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent, gid=plan_gid))}
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
    # child must be told the same number the plan names.
    assert agent.runner.envs[0]["E2B_BROKER_WORKER_GID"] == str(plan_gid)
    # ...and *only* the gid: the plan names no worker uid, and a step that
    # never uses ``--worker`` must not be handed an identity it would never
    # read (``c3_agent.fileops.maint_env``).
    assert "E2B_BROKER_WORKER_UID" not in agent.runner.envs[0]


# ----------------------------------------------------------------- refusals


@pytest.mark.asyncio
async def test_a_path_outside_the_roots_is_refused_named(workspace: Path) -> None:
    agent = _Agent(workspace)
    outside = agent.workspace_base / ".." / ".." / "etc"

    resp = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent, path=str(outside)))}
    )

    assert resp.status_code == 400
    assert "path-outside-roots" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_a_grant_is_single_use(workspace: Path) -> None:
    agent = _Agent(workspace)
    token = _grant(tree=_plan_tree(agent))

    first = await _post(agent, {"grant": token})
    second = await _post(agent, {"grant": token})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"] == "grant already used"
    assert len(agent.runner.calls) == 1


@pytest.mark.asyncio
async def test_a_bad_signature_is_refused(workspace: Path) -> None:
    agent = _Agent(workspace)
    token = _grant(tree=_plan_tree(agent))
    head, signature = token.split(".")
    tampered = f"{head}.{signature[:-2]}{'AB' if signature[-2:] != 'AB' else 'CD'}"

    resp = await _post(agent, {"grant": tampered})

    assert resp.status_code == 401
    assert "bad-signature" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_a_grant_for_another_host_is_refused(workspace: Path) -> None:
    agent = _Agent(workspace)

    resp = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent), host="node_b")}
    )

    assert resp.status_code == 403
    assert "wrong-host" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_expired_grant_is_refused(workspace: Path) -> None:
    agent = _Agent(workspace)
    past = int(time.time()) - 100

    resp = await _post(
        agent,
        {"grant": _grant(tree=_plan_tree(agent), iat=past, exp=past + 10)},
    )

    assert resp.status_code == 401
    assert "expired" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_an_unknown_op_in_the_grant_is_refused(workspace: Path) -> None:
    agent = _Agent(workspace)

    resp = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent), op="chown-workspace")}
    )

    assert resp.status_code == 400
    assert "op-not-allowed" in resp.json()["error"]
    assert agent.runner.calls == []


@pytest.mark.asyncio
async def test_a_body_with_more_than_the_grant_is_refused(workspace: Path) -> None:
    """The plan is the only input: a caller may not add a path beside it."""
    agent = _Agent(workspace)

    resp = await _post(
        agent,
        {
            "grant": _grant(tree=_plan_tree(agent)),
            "path": "/etc",
        },
    )

    assert resp.status_code == 400
    assert "path" in resp.json()["error"]
    assert agent.runner.calls == []


# -------------------------------------------------------- two grants, one node


@pytest.mark.asyncio
async def test_two_sandboxes_grants_do_not_interfere(workspace: Path) -> None:
    agent = _Agent(workspace)

    first = await _post(
        agent, {"grant": _grant(tree=_plan_tree(agent, SANDBOX))}
    )
    second = await _post(
        agent, {"grant": _grant(sandbox_id=OTHER_SANDBOX, tree=_plan_tree(agent, OTHER_SANDBOX))}
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
        {"grant": _grant(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs())))},
    )

    assert resp.status_code == 200
    target = agent.tree() / "workspace"
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
    target = agent.tree() / "workspace"
    target.mkdir(parents=True)
    (target / "sub").symlink_to(outside)
    _snapshot(agent, {"sub": {"file": "payload\n"}})

    resp = await _post(
        agent,
        {"grant": _grant(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs())))},
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
    target = agent.tree() / "workspace"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("from the previous incarnation\n", encoding="utf-8")
    (target / "same.txt").write_text("stale\n", encoding="utf-8")
    _snapshot(agent, {"same.txt": "fresh\n"})

    resp = await _post(
        agent,
        {"grant": _grant(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs())))},
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
    target = agent.tree() / "workspace"
    target.mkdir(parents=True)
    (target / "sub").write_text("I am a file\n", encoding="utf-8")
    _snapshot(agent, {"sub": {"file": "payload\n"}})

    resp = await _post(
        agent,
        {"grant": _grant(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs())))},
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
        {"grant": _grant(tree=_plan_tree(agent, copy_from=str(agent.snapshot_fs())))},
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
        agent, {"grant": _grant(tree=_plan_tree(agent), slices=slices)}
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
        agent, {"grant": _grant(tree=_plan_tree(agent), slices=slices)}
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

    resp = await _post(agent, {"grant": _grant(tree=_plan_tree(agent))})

    assert resp.status_code == 200
    assert sorted(os.listdir(volume_root)) == ["other-sandbox"]
    assert len(agent.runner.calls) == 1
