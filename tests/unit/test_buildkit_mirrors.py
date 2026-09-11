"""buildkitd's docker.io mirror is not a second source of truth.

The buildkit fixture (``tests/conftest.py``) must render exactly the mirrors
the envd resolver would use for the same ``E2B_REGISTRY_MIRRORS`` value;
previously it hardcoded one mirror, so a build could fail on a source the
resolver had already fallen through.
"""

from __future__ import annotations

from tests.conftest import _buildkit_mirror_urls


def test_unset_env_renders_the_default_multi_source_chain(monkeypatch):
    monkeypatch.delenv("E2B_REGISTRY_MIRRORS", raising=False)
    assert _buildkit_mirror_urls() == [
        "https://docker.m.daocloud.io",
        "https://docker.1ms.run",
    ]


def test_configured_sources_keep_their_order_and_loopback_is_plain_http(monkeypatch):
    monkeypatch.setenv(
        "E2B_REGISTRY_MIRRORS",
        "registry-1.docker.io=127.0.0.1:5080|mirror.example",
    )
    assert _buildkit_mirror_urls() == [
        "http://127.0.0.1:5080",
        "https://mirror.example",
    ]


def test_non_loopback_uses_the_resolver_scheme_rule(monkeypatch):
    """Plain HTTP is loopback-only in the resolver too (same rule, no drift)."""
    monkeypatch.setenv(
        "E2B_REGISTRY_MIRRORS", "docker.io=https://mirror.example/|http://other.example"
    )
    assert _buildkit_mirror_urls() == [
        "https://mirror.example",
        "https://other.example",
    ]


def test_explicitly_empty_env_renders_no_mirror(monkeypatch):
    monkeypatch.setenv("E2B_REGISTRY_MIRRORS", "")
    assert _buildkit_mirror_urls() == []
