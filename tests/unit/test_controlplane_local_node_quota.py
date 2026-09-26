"""W4/1: the combined node's volume quota must read the envd quota switch.

``control_plane/api/sandboxes.py`` handed a hardcoded ``via_agent=False`` to
the envd volume-quota helpers, while the very same process (a combined
"合体" node) is the envd service for its sandboxes: the *volume* half of the
quota is provisioned here, the *workspace* half belongs to envd's worker
path, and both have to obey one switch -- ``E2B_QUOTA_AGENT_URL`` present,
else ``E2B_QUOTA_VIA_AGENT`` (default false), i.e. the envd service's own
``Settings.quota_via_agent``.

The switch is not cosmetic in this shape: the merged image runs non-root (no
``xfs_quota``, no ``CAP_SYS_ADMIN``) and a volume may live on an NFS mount,
where the project quota is server-side. Two things are pinned here:

1. the call sites pass that switch instead of a literal (provision *and*
   release -- the GC fallback in ``cleanup_volume_projects`` goes through the
   agent too);
2. the merged process *wires* the agent client, because
   ``control_plane.combined_main`` never builds
   ``envd_service.app.create_app`` (where envd wires it). Without that step a
   configured agent would still resolve to "quota-agent not configured".
"""

from __future__ import annotations

from types import SimpleNamespace

AGENT_URL = "http://quota-agent:49984"


def _clear_switch(monkeypatch) -> None:
    """Drop every agent-related env var so the defaults are the ones asserted."""
    for name in (
        "E2B_QUOTA_AGENT_URL",
        "E2B_QUOTA_VIA_AGENT",
        "E2B_QUOTA_AGENT_TOKEN",
        "E2B_QUOTA_AGENT_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ the switch


def test_the_switch_is_the_envd_services_own_setting(monkeypatch):
    """One rule, asked of the envd service -- not a second copy of it."""
    from control_plane import config as control_config
    from envd_service.config import Settings as EnvdSettings

    cases = (
        (None, None),
        (AGENT_URL, None),
        (None, "true"),
        (AGENT_URL, "false"),
        ("   ", "false"),
    )
    for url, flag in cases:
        _clear_switch(monkeypatch)
        if url is not None:
            monkeypatch.setenv("E2B_QUOTA_AGENT_URL", url)
        if flag is not None:
            monkeypatch.setenv("E2B_QUOTA_VIA_AGENT", flag)

        assert (
            control_config.local_node_quota_via_agent()
            is EnvdSettings().quota_via_agent
        ), (url, flag)


def test_the_switch_is_on_for_a_url_and_for_the_legacy_flag(monkeypatch):
    """The two documented ways to turn the agent form on (and the default)."""
    from control_plane import config as control_config

    _clear_switch(monkeypatch)
    assert control_config.local_node_quota_via_agent() is False

    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)
    assert control_config.local_node_quota_via_agent() is True

    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_VIA_AGENT", "true")
    assert control_config.local_node_quota_via_agent() is True

    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", "")
    monkeypatch.setenv("E2B_QUOTA_VIA_AGENT", "false")
    assert control_config.local_node_quota_via_agent() is False


# ------------------------------------------------------- the two call sites


class _PoolStub:
    """Only what ``_provision_local`` touches on the uid pool."""

    def __init__(self) -> None:
        self.acquired: tuple[str, object] | None = None
        self.committed = False
        self.released = False

    def acquire(self, sandbox_id: str, preferred=None) -> int:
        self.acquired = (sandbox_id, preferred)
        return 20000

    def commit(self, sandbox_id: str) -> None:
        self.committed = True

    def release(self, sandbox_id: str) -> None:
        self.released = True


class _RuntimeStub:
    def __init__(self, pool: _PoolStub | None) -> None:
        self.uid_pool = pool
        self.registered: dict = {}
        self.volume_projects: list = []

    def get(self, sandbox_id: str):
        return None

    def register(self, **kwargs) -> None:
        self.registered = kwargs

    def unregister(self, sandbox_id: str) -> None:
        pass


class _LocalRuntimeStub(_RuntimeStub):
    """A registry whose ``get`` returns the runtime the destroy path edits."""

    def __init__(self, volume_projects: list) -> None:
        super().__init__(pool=None)
        # The verified-target check reads the runtime record's own id the way
        # ``RuntimeSandbox`` carries it (the real registry materialises the
        # record from ``<base>/<id>/sandbox.json``, so it always matches).
        self.sandbox_id = _Record.sandbox_id
        self.volume_projects = volume_projects

    def get(self, sandbox_id: str):
        return self


class _Record:
    sandbox_id = "sbx_w4"
    envd_access_token = "tok"
    workspace_dir = None
    env_vars: dict = {}
    base_image = None
    memory_mb = 1024
    cpu_count = 1
    disk_size_mb = 1024
    max_processes = 64
    allow_internet_access = False
    mcp = None
    network: dict = {}
    iam_tokens: dict = {}
    #: Set by the registry before provisioning (OBS-9); ``None`` here keeps
    #: this test on the worker-pool fallback path it is about.
    host_uid: int | None = None


def _provision_request(tmp_path, pool):
    runtime = _RuntimeStub(pool)
    settings = SimpleNamespace(
        workspace_base=tmp_path,
        shared_volume_root=tmp_path / "shared-volumes",
        max_command_timeout=600,
    )
    state = SimpleNamespace(
        settings=settings,
        workspace_base=tmp_path / "workspaces",
        volumes={},
        snapshots=SimpleNamespace(expand_to=lambda snapshot, target: None),
        runtime_registry=runtime,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _recorded_provision_kwargs(tmp_path, monkeypatch) -> dict:
    import control_plane.api.sandboxes as sandboxes

    request = _provision_request(tmp_path, _PoolStub())
    recorded: dict = {}

    def _build_volume_mounts(**kwargs):
        recorded.update(kwargs)
        return [], []

    monkeypatch.setattr(sandboxes.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        "envd_service.volumes.build_volume_mounts", _build_volume_mounts
    )
    monkeypatch.setattr(
        "envd_service.uid_pool.apply_sandbox_ownership",
        lambda workspace_dir, host_uid: None,
    )

    sandboxes._provision_local(
        request,
        _Record(),
        snapshot=None,
        volume_mounts=[],
        settings=request.app.state.settings,
    )
    return recorded


def test_provision_local_asks_the_agent_when_the_switch_is_on(
    tmp_path, monkeypatch
):
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)

    assert _recorded_provision_kwargs(tmp_path, monkeypatch)["via_agent"] is True


def test_provision_local_stays_local_when_the_switch_is_off(
    tmp_path, monkeypatch
):
    """No agent configured: today's direct path, unchanged."""
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_VIA_AGENT", "false")

    assert _recorded_provision_kwargs(tmp_path, monkeypatch)["via_agent"] is False


def _recorded_cleanup_kwargs(tmp_path, monkeypatch) -> list[dict]:
    import control_plane.api.sandboxes as sandboxes

    volume_projects = [
        {
            "volume_id": "vol_w4",
            "sandbox_id": _Record.sandbox_id,
            "mount_path": "mnt/data",
            "sandbox_dir": str(tmp_path / "shared-volumes" / "vol_w4" / "sbx_w4"),
            "projid": 4242,
        }
    ]
    runtime = _LocalRuntimeStub(volume_projects)
    state = SimpleNamespace(
        runtime_registry=runtime,
        workspace_base=tmp_path / "workspaces",
        # N27: ``app.state`` carries the platform's own base beside the tree
        # base; the teardown takes the record's directory from it.
        state_base=tmp_path / "workspaces",
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        "envd_service.volumes.cleanup_volume_projects",
        lambda **kwargs: calls.append(kwargs),
    )

    sandboxes._destroy_local(state, _Record())
    return calls


def test_destroy_local_releases_volume_quota_through_the_agent(
    tmp_path, monkeypatch
):
    """The release side follows the same switch (GC's quota fallback)."""
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)

    calls = _recorded_cleanup_kwargs(tmp_path, monkeypatch)

    assert len(calls) == 1
    assert calls[0]["via_agent"] is True


def test_destroy_local_stays_local_when_the_switch_is_off(tmp_path, monkeypatch):
    _clear_switch(monkeypatch)

    calls = _recorded_cleanup_kwargs(tmp_path, monkeypatch)

    assert len(calls) == 1
    assert calls[0]["via_agent"] is False


# ------------------------------------------------- the combined process wiring


class _ClientStub:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _create_control_app(tmp_path, *, enable_local_node: bool):
    from control_plane.app import create_app
    from control_plane.config import Settings

    return create_app(
        settings=Settings(
            enable_local_node=enable_local_node,
            workspace_base=tmp_path / "workspaces",
        )
    )


def test_a_combined_node_wires_the_agent_client_when_the_switch_is_on(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    client = _ClientStub()
    monkeypatch.setattr(
        "envd_service.quota_agent.configure_quota_agent_client",
        lambda **kwargs: (calls.append(kwargs), client)[1],
    )
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)
    monkeypatch.setenv("E2B_QUOTA_AGENT_TOKEN", "agent-token")

    app = _create_control_app(tmp_path, enable_local_node=True)

    assert calls == [
        {"url": AGENT_URL, "token": "agent-token", "timeout_s": 5.0}
    ], calls
    assert app.state.quota_agent_client is client


def test_the_wiring_coordinates_match_the_envd_services_own_settings(
    tmp_path, monkeypatch
):
    """The three env names/defaults the control plane reads are envd's.

    The combined process reads them without building the envd ``Settings``
    (that would make the control plane validate unrelated worker config), so
    this pins the two spellings together: same env vars in, same client
    coordinates out.
    """
    from envd_service.config import Settings as EnvdSettings

    calls: list[dict] = []
    monkeypatch.setattr(
        "envd_service.quota_agent.configure_quota_agent_client",
        lambda **kwargs: calls.append(kwargs),
    )
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)
    monkeypatch.setenv("E2B_QUOTA_AGENT_TOKEN", "agent-token")
    monkeypatch.setenv("E2B_QUOTA_AGENT_TIMEOUT_S", "7.5")
    envd = EnvdSettings()

    _create_control_app(tmp_path, enable_local_node=True)

    assert calls == [
        {
            "url": envd.quota_agent_url,
            "token": envd.quota_agent_token,
            "timeout_s": envd.quota_agent_timeout_s,
        }
    ], calls
    assert envd.quota_agent_url == AGENT_URL
    assert envd.quota_agent_timeout_s == 7.5


def test_the_wiring_does_not_read_the_rest_of_the_worker_config(
    tmp_path, monkeypatch
):
    """A malformed *unrelated* worker env must not take the control plane down.

    ``envd_service.config.Settings`` raises on e.g. bad JSON in
    ``E2B_PORT_MAPPINGS``; the combined control plane never looked at that key
    before, and wiring the agent must not start now.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "envd_service.quota_agent.configure_quota_agent_client",
        lambda **kwargs: calls.append(kwargs),
    )
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)
    monkeypatch.setenv("E2B_PORT_MAPPINGS", "{not json")

    app = _create_control_app(tmp_path, enable_local_node=True)

    assert calls == [
        {"url": AGENT_URL, "token": None, "timeout_s": 5.0}
    ], calls
    assert app.state.quota_agent_client is None


def test_a_combined_node_wires_nothing_when_the_switch_is_off(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    monkeypatch.setattr(
        "envd_service.quota_agent.configure_quota_agent_client",
        lambda **kwargs: calls.append(kwargs),
    )
    _clear_switch(monkeypatch)

    app = _create_control_app(tmp_path, enable_local_node=True)

    assert calls == []
    assert app.state.quota_agent_client is None


def test_a_separated_control_plane_never_wires_an_agent(tmp_path, monkeypatch):
    """``E2B_ENABLE_LOCAL_NODE=false`` provisions no quota in this process."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "envd_service.quota_agent.configure_quota_agent_client",
        lambda **kwargs: calls.append(kwargs),
    )
    _clear_switch(monkeypatch)
    monkeypatch.setenv("E2B_QUOTA_AGENT_URL", AGENT_URL)

    app = _create_control_app(tmp_path, enable_local_node=False)

    assert calls == []
    assert app.state.quota_agent_client is None
