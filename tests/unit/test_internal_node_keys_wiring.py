"""N49's step ①, wired: the worker presents its *own* node-bound credential.

The control plane has enforced the binding for a while (`node_id_for_key` +
`_require_node_identity`: a key bound to node A claiming node B is a 403), but
the deployment shipped no map, so every worker presented the fleet key and the
only scoping left was "the request must come from that node's pod IP". These
pin the worker half: with a map, the credential itself says which node is
calling; without one (compose, tests, older stacks), the fleet key is still
what goes out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from envd_service.agent_fileops import _internal_key_for
from envd_service.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        internal_api_key="fleet-key",
        node_id="e2b-worker-1",
        internal_node_keys={"key-0": "e2b-worker-0", "key-1": "e2b-worker-1"},
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_the_worker_presents_its_own_node_key():
    assert _settings().outbound_internal_key == "key-1"


def test_a_node_missing_from_the_map_falls_back_to_the_fleet_key():
    settings = _settings(node_id="e2b-worker-9")

    assert settings.outbound_internal_key == "fleet-key"


def test_no_map_at_all_falls_back_to_the_fleet_key():
    settings = _settings(internal_node_keys={})

    assert settings.outbound_internal_key == "fleet-key"


def test_the_node_id_and_map_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("E2B_NODE_ID", "e2b-worker-0")
    monkeypatch.setenv("E2B_INTERNAL_API_KEY", "env-fleet")
    monkeypatch.setenv(
        "E2B_INTERNAL_NODE_KEYS", json.dumps({"env-0": "e2b-worker-0"})
    )

    settings = Settings()

    assert (settings.node_id, settings.outbound_internal_key) == (
        "e2b-worker-0",
        "env-0",
    )


def test_the_file_op_client_asks_for_the_same_key():
    assert _internal_key_for(_settings()) == "key-1"


def test_a_stub_without_the_property_still_gets_the_fleet_key():
    stub = SimpleNamespace(internal_api_key="stub-fleet")

    assert _internal_key_for(stub) == "stub-fleet"
