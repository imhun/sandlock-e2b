"""N27 Task 3: the control plane's platform state base (``E2B_STATE_BASE``).

The worker side landed in ``e671cd0``; this pins the same semantics for the
control plane: one ``state_base`` on ``Settings`` (unset = *this object's*
workspace base), every ``sandbox_runtime_dir`` / non-legacy
``sandbox_command_log_path`` call site carrying it, ``app.state`` exposing the
base the registry actually serves records from, and the platform's own shared
directories (``_secrets`` / ``_snapshots`` / ``_templates`` / ``_volumes``)
anchored at the shared export root rather than at the tree root, which N27
sinks one level (``<export>/workspaces``).
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace

from control_plane import api
from control_plane.api.sandboxes import _remove_local_tree_confirming
from control_plane.app import create_app
from control_plane.config import Settings
from envd_service.runtime.registry import RuntimeRegistry

WS = Path("/ws")
ST = Path("/st")

SANDBOXES_PY = Path(inspect.getfile(api.sandboxes))


def _calls(module_file: Path, name: str) -> list[ast.Call]:
    """Every ``name(...)`` call in the module, by parsed source (not grep)."""
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _keyword_names(call: ast.Call) -> list[str]:
    return [keyword.arg for keyword in call.keywords]


# --- the setting -----------------------------------------------------------


def test_cp_state_base_defaults_to_the_workspace_base(monkeypatch):
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    monkeypatch.delenv("E2B_WORKSPACE_BASE", raising=False)
    assert Settings(workspace_base=WS).state_base == WS


def test_cp_state_base_reads_only_the_state_env(monkeypatch):
    """``E2B_WORKSPACE_BASE`` must not leak into the platform base: a
    deployment that named one base while the trees sit under another is
    exactly the split this switch exists to make explicit."""
    monkeypatch.setenv("E2B_STATE_BASE", str(ST))
    monkeypatch.setenv("E2B_WORKSPACE_BASE", "/elsewhere")
    settings = Settings(workspace_base=WS)
    assert settings.state_base == ST
    assert settings.workspace_base == WS


def test_cp_state_base_env_is_resolved(monkeypatch, tmp_path):
    raw = f"{tmp_path}/export/../state"
    monkeypatch.setenv("E2B_STATE_BASE", raw)
    assert Settings(workspace_base=WS).state_base == Path(raw).resolve()


# --- the call sites --------------------------------------------------------


def test_every_runtime_dir_call_passes_a_state_base():
    calls = _calls(SANDBOXES_PY, "sandbox_runtime_dir")
    assert len(calls) == 2
    for call in calls:
        assert _keyword_names(call) == ["state_base"]


def test_the_command_log_read_carries_the_state_base():
    calls = _calls(SANDBOXES_PY, "sandbox_command_log_path")
    assert len(calls) == 2
    primary = [c for c in calls if "legacy" not in _keyword_names(c)]
    legacy = [c for c in calls if "legacy" in _keyword_names(c)]
    assert len(primary) == 1
    assert len(legacy) == 1
    # The primary read is the platform file, so it follows the platform's base.
    assert _keyword_names(primary[0]) == ["state_base"]
    # The pre-split location is inside the sandbox's own tree: a *workspace*
    # base question, deliberately without a state base.
    assert _keyword_names(legacy[0]) == ["legacy"]


# --- the app wires them together -------------------------------------------


def _app(workspace_base: Path, **settings_kwargs):
    """A control-plane app whose settings and seam name the same tree base --
    the shape every deployment has."""
    settings_kwargs.setdefault("workspace_base", workspace_base)
    return create_app(
        settings=Settings(
            api_keys=("local-key",),
            create_queue_timeout_s=0,
            **settings_kwargs,
        ),
        workspace_base=workspace_base,
    )


def test_app_state_exposes_the_base_the_registry_serves_records_from(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    monkeypatch.setenv("E2B_STATE_BASE", str(state))
    app = _app(tmp_path / "trees")
    assert app.state.state_base == state
    assert app.state.runtime_registry.state_base == state


def test_the_registrys_state_base_wins_over_the_settings(tmp_path, monkeypatch):
    """``create_app`` may be handed a registry of its own (the shared test
    fixture does exactly that) sitting on a base the settings do not name;
    every reader has to follow the registry, because that is where the record
    was written (``envd_service.agent._registry_state_base`` has the same
    rule)."""
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    trees = tmp_path / "trees"
    trees.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    app = create_app(
        settings=Settings(
            api_keys=("local-key",),
            create_queue_timeout_s=0,
            workspace_base=tmp_path / "unnamed",
        ),
        runtime_registry=RuntimeRegistry(trees, state_base=state),
        workspace_base=trees,
    )
    assert app.state.state_base == state


def test_without_a_state_base_everything_stays_on_the_workspace_base(
    tmp_path, monkeypatch
):
    """The zero-change half of the invariant: no ``E2B_STATE_BASE``, no
    ``E2B_SHARED_WORKSPACE_ROOT`` -- every base is the one ``create_app`` was
    pointed at, exactly as before N27."""
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    monkeypatch.delenv("E2B_SHARED_WORKSPACE_ROOT", raising=False)
    trees = tmp_path / "trees"
    trees.mkdir()
    app = _app(trees, shared_workspace_root=None)
    assert app.state.state_base == trees
    assert app.state.secrets._base == trees / "_secrets"
    assert app.state.snapshots._base == trees
    assert app.state.snapshots._snapshot_dir("snap_a") == (
        trees / "_snapshots" / "snap_a"
    )
    assert app.state.templates._base == trees / "_templates"
    assert app.state.volumes._base == trees / "_volumes"


def test_platform_directories_follow_the_shared_root_not_the_tree_root(
    tmp_path, monkeypatch
):
    """With the tree root sunk (``<export>/workspaces``) the platform's own
    shared directories stay on the export root -- the same six subPath mounts
    ``deploy/k8s/control-plane.yaml`` keeps there, and where the worker's
    ``workspace-root-init`` creates them."""
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    export = tmp_path / "export"
    trees = export / "workspaces"
    trees.mkdir(parents=True)
    app = _app(trees, shared_workspace_root=str(export))
    assert app.state.workspace_base == trees
    assert app.state.platform_root == export
    assert app.state.secrets._base == export / "_secrets"
    assert app.state.snapshots._base == export
    assert app.state.snapshots._snapshot_dir("snap_a") == (
        export / "_snapshots" / "snap_a"
    )
    assert app.state.templates._base == export / "_templates"
    assert app.state.volumes._base == export / "_volumes"


def test_the_template_build_directory_is_not_derived_from_the_tree_base():
    """``_builds`` is a platform namespace like the registries' bases: it stays
    on the shared export root (this pod's writable subPath), so the module that
    writes build contexts must not name the tree base."""
    source = inspect.getsource(api.templates)
    assert source.count("app.state.workspace_base") == 0
    assert source.count("app.state.platform_root") == 2


def test_the_local_delete_removes_the_runtime_dir_from_the_state_base(tmp_path):
    """The teardown pairs the tree with the platform's files, and those are the
    state base's now: removing ``<tree base>/_runtime/<id>`` would leave the
    record -- and the next delete's own evidence -- behind."""
    trees = tmp_path / "trees"
    state = tmp_path / "state"
    (trees / "_runtime" / "sbx_a").mkdir(parents=True)
    (state / "_runtime" / "sbx_a").mkdir(parents=True)
    app_state = SimpleNamespace(workspace_base=trees, state_base=state)
    assert _remove_local_tree_confirming(app_state, "sbx_a") is True
    assert not (state / "_runtime" / "sbx_a").exists()
    assert (trees / "_runtime" / "sbx_a").is_dir()


def test_the_startup_line_names_the_platform_state_base(tmp_path, monkeypatch, caplog):
    """Operators reconcile the two processes by grepping one line (Task 7)."""
    state = tmp_path / "state"
    monkeypatch.setenv("E2B_STATE_BASE", str(state))
    with caplog.at_level(logging.INFO, logger="control_plane.app"):
        _app(tmp_path / "trees")
    messages = [record.getMessage() for record in caplog.records]
    assert f"platform state base = {state}" in messages
