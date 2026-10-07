"""The env-var alias layer: a new name wins, a legacy name still works once.

The rename moves an environment variable, and a deployment and its manifests
are not updated in the same instant. So for one version both spellings are
read, with a fixed precedence that can never be silently the wrong one:

* the **new** name wins whenever it is set to a non-empty value, even when the
  old one is also set (and disagrees) -- the migration direction is one-way,
  and an operator who has moved on must not be dragged back by a stale line;
* the old name is still honoured when the new one is absent or empty, and its
  use is **warned exactly once per process** -- not once per read, so a hot
  path does not flood the log, and not never, so the deprecation is visible.

Both cases name *both* spellings in the warning, because the whole point is to
tell the operator which line to change.
"""

from __future__ import annotations

import logging

import pytest

from envd_service import env_alias
from envd_service.env_alias import read as read_alias
from envd_service.config import Settings


@pytest.fixture(autouse=True)
def _fresh_warn_once():
    """The warn-once set is process-wide; each case starts from a clean one."""
    env_alias.reset_warnings()
    yield
    env_alias.reset_warnings()


def _settings() -> Settings:
    # A fresh Settings re-reads the environment (the fields are
    # ``default_factory`` lambdas), so the monkeypatched variables are what
    # this object sees.
    return Settings()


def test_the_new_name_wins_when_both_are_set(monkeypatch, caplog) -> None:
    monkeypatch.setenv("E2B_OWN_IDENTITY", "off")
    monkeypatch.setenv("E2B_ROUTE_B", "on")
    with caplog.at_level(logging.WARNING, logger="envd_service.env_alias"):
        assert _settings().own_identity == "off"
    assert "E2B_ROUTE_B" in caplog.text and "E2B_OWN_IDENTITY" in caplog.text


def test_the_legacy_name_still_works_and_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.delenv("E2B_OWN_IDENTITY", raising=False)
    monkeypatch.setenv("E2B_ROUTE_B", "on")
    with caplog.at_level(logging.WARNING, logger="envd_service.env_alias"):
        assert _settings().own_identity == "on"
        assert caplog.text.count("E2B_ROUTE_B") == 1
        # A second read must not warn again (the warn-once set is
        # process-wide, so this is the same process's second read).
        assert _settings().own_identity == "on"
    assert caplog.text.count("E2B_ROUTE_B") == 1


def test_the_new_name_alone_is_read_without_a_warning(monkeypatch, caplog) -> None:
    monkeypatch.delenv("E2B_ROUTE_B", raising=False)
    monkeypatch.setenv("E2B_OWN_IDENTITY", "on")
    with caplog.at_level(logging.WARNING, logger="envd_service.env_alias"):
        assert _settings().own_identity == "on"
    assert caplog.text == ""


def test_neither_name_means_the_default(monkeypatch, caplog) -> None:
    monkeypatch.delenv("E2B_ROUTE_B", raising=False)
    monkeypatch.delenv("E2B_OWN_IDENTITY", raising=False)
    with caplog.at_level(logging.WARNING, logger="envd_service.env_alias"):
        assert read_alias(
            "E2B_OWN_IDENTITY", legacy="E2B_ROUTE_B", default="auto"
        ) == "auto"
    assert caplog.text == ""
