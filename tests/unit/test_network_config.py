"""Network configuration normalization and sandlock policy mapping."""

from __future__ import annotations

import pytest

from gateway_common.network import (
    NetworkConfigError,
    normalize_network_config,
    normalize_network_update,
    sandlock_network_policy,
)


def test_normalize_create_keeps_supported_fields():
    raw = {
        "allowOut": ["8.8.8.8", "example.com", "10.0.0.0/8"],
        "denyOut": ["169.254.169.254"],
        "allowPublicTraffic": True,
        "rules": {"api.example.com": []},
    }
    assert normalize_network_config(raw) == raw


def test_normalize_create_rejects_unsupported():
    cases = [
        {"maskRequestHost": "internal.example.com"},
        {"rules": {"api.example.com": [{"transform": {"headers": {"X-A": "1"}}}]}},
        {"denyOut": ["example.com"]},
        {"allowOut": ["*.example.com"]},
        {"allowOut": "8.8.8.8"},
        {"allowPublicTraffic": "yes"},
        {"bogus": 1},
    ]
    for raw in cases:
        with pytest.raises(NetworkConfigError):
            normalize_network_config(raw)


def test_normalize_update_clears_omitted_fields():
    update = normalize_network_update({"allowInternetAccess": False})
    assert update == {"allowInternetAccess": False}
    # Explicit null egressProxy clears the proxy (atomic-replace semantics).
    assert normalize_network_update({"egressProxy": None}) == {
        "egressProxy": None
    }
    with pytest.raises(NetworkConfigError):
        normalize_network_update(None)
    with pytest.raises(NetworkConfigError):
        normalize_network_update({"egressProxy": {"address": "p:1080"}})


def test_egress_proxy_validation():
    net = normalize_network_config(
        {"egressProxy": {"address": "1.1.1.1:1080"}}
    )
    assert net["egressProxy"] == {"address": "1.1.1.1:1080"}
    net = normalize_network_config(
        {
            "egressProxy": {
                "address": "1.1.1.1:1080",
                "username": "u",
                "password": "p",
            }
        }
    )
    assert net["egressProxy"]["username"] == "u"
    assert net["egressProxy"]["password"] == "p"
    for address in (
        "127.0.0.1:1080",
        "10.0.0.1:1080",
        "192.168.1.1:1080",
        "169.254.1.1:1080",
        "no-port",
        "x:70000",
        "1.1.1.1",
    ):
        with pytest.raises(NetworkConfigError):
            normalize_network_config({"egressProxy": {"address": address}})
    with pytest.raises(NetworkConfigError):
        normalize_network_config(
            {
                "egressProxy": {
                    "address": "1.1.1.1:1080",
                    "username": "x" * 256,
                }
            }
        )


def test_wildcard_domains_allowed_only_with_egress_proxy():
    """``*.example.com`` needs the egress-proxy filter: rejected on the
    sandlock net_allow path, accepted once an egress proxy is configured."""
    with pytest.raises(NetworkConfigError):
        normalize_network_config({"allowOut": ["*.example.com"]})
    with pytest.raises(NetworkConfigError):
        normalize_network_update({"allowOut": ["*.example.com"]})

    net = normalize_network_config(
        {
            "egressProxy": {"address": "1.1.1.1:1080"},
            "allowOut": ["*.example.com"],
        }
    )
    assert net["allowOut"] == ["*.example.com"]
    update = normalize_network_update(
        {
            "egressProxy": {"address": "1.1.1.1:1080"},
            "allowOut": ["*.example.com"],
        }
    )
    assert update["allowOut"] == ["*.example.com"]


def test_policy_allow_only_is_default_deny():
    policy = sandlock_network_policy(
        {"allowOut": ["8.8.8.8", "10.0.0.0/8"]},
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["net_allow"] == ["tcp://8.8.8.8:*", "tcp://10.0.0.0/8:*"]
    assert policy["net_deny"] == []


def test_policy_deny_only_is_default_allow():
    policy = sandlock_network_policy(
        {"denyOut": ["169.254.169.254", "10.0.0.0/8"]},
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["net_allow"] == []
    assert policy["net_deny"] == ["169.254.169.254", "10.0.0.0/8"]


def test_policy_both_prefers_allowlist_and_deny_precedence():
    policy = sandlock_network_policy(
        {
            "allowOut": ["8.8.8.8", "10.1.2.3", "10.0.0.0/8", "example.com"],
            "denyOut": ["10.0.0.0/8"],
        },
        allow_internet_access=True,
        enable_network=True,
    )
    # 10.1.2.3 and 10.0.0.0/8 are covered by the deny CIDR and dropped;
    # sandlock forbids mixing net_allow with net_deny.
    assert policy["net_allow"] == ["tcp://8.8.8.8:*", "tcp://example.com:*"]
    assert policy["net_deny"] == []


def test_policy_internet_off_denies_all():
    policy = sandlock_network_policy(
        {"allowInternetAccess": False},
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["net_allow"] == []


def test_policy_internet_on_allows_all():
    policy = sandlock_network_policy(
        {"allowInternetAccess": True},
        allow_internet_access=False,
        enable_network=True,
    )
    assert policy["net_allow"] == ["*:*"]


def test_policy_network_disabled_denies_all():
    policy = sandlock_network_policy(
        {"allowInternetAccess": True},
        allow_internet_access=True,
        enable_network=False,
    )
    assert policy["net_allow"] == []


def test_policy_rules_become_http_allow():
    policy = sandlock_network_policy(
        {
            "rules": {"api.example.com": [], "*.ignored.example": []},
            "allowInternetAccess": True,
        },
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["http_allow"] == ["* api.example.com/*"]


def test_sandlock_executor_maps_network_policy():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    executor = SandlockExecutor(
        workspace_dir="/tmp/ws",
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=True,
        network={
            "allowOut": ["8.8.8.8"],
            "rules": {"api.example.com": []},
        },
    )
    config = ExecConfig(
        cmd=["true"],
        env={},
        cwd="/tmp/ws",
        stdin_enabled=False,
    )
    kwargs = executor._build_sandbox(config)
    assert kwargs.net_allow == ["tcp://8.8.8.8:*"]
    assert kwargs.net_deny == []
    assert kwargs.http_allow == ["* api.example.com/*"]

    # Dynamic update replaces the policy for the next command.
    executor.update_network({"denyOut": ["169.254.169.254"]})
    kwargs = executor._build_sandbox(config)
    assert kwargs.net_allow == []
    assert kwargs.net_deny == ["169.254.169.254"]
