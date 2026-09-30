"""``E2B_SLOT_IDENTITY`` unset must not silently pick the pre-C3 shape.

The field used to default to ``spawn`` -- "the default, and the fallback until
Task 4/7" -- and C3 is now the shape every shipped manifest runs
(``agent-grant``, set explicitly in `deploy/k8s/worker.yaml` and both compose
production stacks). A deployment that *has* an agent but forgot the key should
therefore get the C3 path, not the in-process one, and a deployment that has no
agent must still work -- with a line that says which of the two it is, so
nobody has to read the manifest to find out.

Explicit stays explicit: a deployment that names the mode gets it, warning or
not.
"""

from __future__ import annotations

import logging

import pytest

from envd_service import config as envd_config


@pytest.fixture(autouse=True)
def _fresh_warning_latch(monkeypatch):
    """The once-per-process latch is process state; reset it per test."""
    monkeypatch.setattr(envd_config, "_slot_identity_default_reported", False)
    for name in (
        "E2B_SLOT_IDENTITY",
        "E2B_PRIV_HELPER_TRANSPORT",
    ):
        monkeypatch.delenv(name, raising=False)


def _identity() -> str:
    return envd_config.Settings().slot_identity


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == envd_config.__name__
        and record.levelno == logging.WARNING
    ]


def test_an_agent_shaped_deployment_defaults_to_the_c3_path(monkeypatch):
    """Every C3-shaped worker declares the agent transport.

    That is the one signal this package may read -- the agent's *address* stays
    unknown to the worker on purpose
    (`tests/unit/test_route_b_slot_identity.py` pins it).
    """
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    assert _identity() == "agent-grant"


def test_a_shape_without_an_agent_keeps_the_only_thing_it_can_do(caplog):
    """No agent means no grant: ``spawn`` is not a downgrade here, it is the
    answer -- and the line below is how the operator learns that."""
    with caplog.at_level(logging.WARNING, logger=envd_config.__name__):
        assert _identity() == "spawn"

    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "E2B_SLOT_IDENTITY is unset" in messages[0]
    assert "'spawn'" in messages[0]
    assert "no agent is configured" in messages[0]


def test_an_explicit_mode_wins_and_says_nothing(monkeypatch, caplog):
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    monkeypatch.setenv("E2B_SLOT_IDENTITY", "spawn")

    with caplog.at_level(logging.WARNING, logger=envd_config.__name__):
        assert _identity() == "spawn"

    assert _warnings(caplog) == []


def test_the_fallback_line_is_one_per_process(monkeypatch, caplog):
    """Settings() is built hundreds of times per process (tests, embedders)."""
    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")

    with caplog.at_level(logging.WARNING, logger=envd_config.__name__):
        for _ in range(3):
            assert _identity() == "agent-grant"

    assert len(_warnings(caplog)) == 1
