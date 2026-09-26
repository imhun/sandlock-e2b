"""Network configuration normalization and sandlock policy mapping."""

from __future__ import annotations

from pathlib import Path

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
        {"denyOut": ["example.com"]},
        {"allowOut": "8.8.8.8"},
        {"allowPublicTraffic": "yes"},
        {"maskRequestHost": "bad host"},
        {"maskRequestHost": "https://x.com"},
        {
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-A": ""}}}
                ]
            }
        },
        {
            "rules": {
                "api.example.com": [
                    {"transform": {"body": {"foo": "bar"}}}
                ]
            }
        },
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


def test_wildcard_domains_allowed_without_egress_proxy():
    """``*.example.com`` is accepted on the fork sandlock net_allow path
    (per-sandbox DNS gateway), with or without an egress proxy."""
    net = normalize_network_config({"allowOut": ["*.example.com"]})
    assert net["allowOut"] == ["*.example.com"]
    update = normalize_network_update({"allowOut": ["*.example.com"]})
    assert update["allowOut"] == ["*.example.com"]


def test_normalize_accepts_mask_request_host_and_transform_headers():
    net = normalize_network_config(
        {
            "maskRequestHost": "localhost:${PORT}",
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-API-Key": "secret"}}}
                ]
            },
        }
    )
    assert net["maskRequestHost"] == "localhost:${PORT}"
    assert net["rules"] == {
        "api.example.com": [
            {"transform": {"headers": {"X-API-Key": "secret"}}}
        ]
    }
    # maskRequestHost is create-only (rejected on update, like the official API).
    with pytest.raises(NetworkConfigError):
        normalize_network_update({"maskRequestHost": "localhost:${PORT}"})


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


def test_policy_internet_on_with_private_deny_uses_denylist():
    """The implicit full-egress branch becomes a private-range DenyList
    (default-allow for the public internet) when private_deny_cidrs is set."""
    cidrs = ["10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8"]
    policy = sandlock_network_policy(
        {"allowInternetAccess": True},
        allow_internet_access=False,
        enable_network=True,
        private_deny_cidrs=cidrs,
    )
    assert policy["net_allow"] == []
    assert policy["net_deny"] == cidrs


def test_policy_explicit_allowout_keeps_private_entries():
    """Explicit allowOut grants are never filtered by the private denylist:
    a caller that deliberately allows an internal service keeps it."""
    policy = sandlock_network_policy(
        {"allowOut": ["10.0.0.5:443", "8.8.8.8"]},
        allow_internet_access=True,
        enable_network=True,
        private_deny_cidrs=["10.0.0.0/8", "127.0.0.0/8"],
    )
    assert "10.0.0.5:443" in policy["net_allow"]
    assert "tcp://8.8.8.8:*" in policy["net_allow"]
    assert policy["net_deny"] == []


def test_policy_internet_on_with_rules_and_private_deny():
    """rules (http_allow) coexist with the private-range DenyList: the HTTP
    ACL still maps and the net policy stays a DenyList (sandlock gives
    net_deny precedence; http_allow works through the transparent proxy)."""
    policy = sandlock_network_policy(
        {"rules": {"api.example.com": []}, "allowInternetAccess": True},
        allow_internet_access=False,
        enable_network=True,
        private_deny_cidrs=["10.0.0.0/8"],
    )
    assert policy["http_allow"] == ["* api.example.com/*"]
    assert policy["net_deny"] == ["10.0.0.0/8"]


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


def test_policy_transform_headers_and_host_mask_map_to_sandlock_kwargs():
    policy = sandlock_network_policy(
        {
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-API-Key": "secret"}}}
                ]
            },
            "maskRequestHost": "localhost:${PORT}",
            "allowInternetAccess": True,
        },
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["host_mask"] == "localhost:${PORT}"
    assert policy["http_inject"] == [
        {
            "matcher": "api.example.com",
            "auth": "header:X-API-Key",
            "value": "secret",
            "name": "hdr_api_example_com_x_api_key",
            "on_existing": "replace",
        }
    ]


def test_policy_egress_proxy_passes_through():
    policy = sandlock_network_policy(
        {
            "egressProxy": {
                "address": "1.1.1.1:1080",
                "username": "u",
                "password": "p",
            },
            "allowOut": ["example.com"],
        },
        allow_internet_access=True,
        enable_network=True,
    )
    assert policy["egress_proxy"] == {
        "address": "1.1.1.1:1080",
        "username": "u",
        "password": "p",
    }
    # In egress mode the filter stays on the real destination; the proxy
    # endpoint is dialed by the supervisor and not part of net_allow.
    assert policy["net_allow"] == ["tcp://example.com:*"]


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
    assert kwargs.http_inject == []
    assert kwargs.host_mask is None
    assert kwargs.egress_proxy is None


def test_executor_materializes_transform_headers_and_egress_proxy(tmp_path):
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    secrets = tmp_path / "secrets"
    executor = SandlockExecutor(
        workspace_dir=str(tmp_path / "sbx_1"),
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
            "allowOut": ["api.example.com"],
            "maskRequestHost": "internal.test:${PORT}",
            "egressProxy": {"address": "1.1.1.1:1080"},
            "rules": {
                "api.example.com": [
                    {"transform": {"headers": {"X-API-Key": "sk-literal"}}}
                ]
            },
        },
        secrets_dir=secrets,
    )
    config = ExecConfig(
        cmd=["true"],
        env={},
        cwd=str(tmp_path / "sbx_1"),
        stdin_enabled=False,
    )
    kwargs = executor._build_sandbox(config)
    assert kwargs.host_mask == "internal.test:${PORT}"
    assert kwargs.egress_proxy == {"address": "1.1.1.1:1080"}
    assert len(kwargs.http_inject) == 1
    entry = kwargs.http_inject[0]
    assert entry["secret"].startswith("file:")
    secret_path = Path(entry["secret"].split(":", 1)[1])
    assert secret_path.read_text(encoding="utf-8") == "sk-literal"
    assert oct(secret_path.stat().st_mode & 0o777) == "0o600"


def test_executor_identity_token_placeholder_requires_env(tmp_path, monkeypatch):
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    executor = SandlockExecutor(
        workspace_dir=str(tmp_path / "sbx_2"),
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
            "allowOut": ["api.example.com"],
            "rules": {
                "api.example.com": [
                    {
                        "transform": {
                            "headers": {
                                "Authorization": "${e2b.identity.tokens.openai}"
                            }
                        }
                    }
                ]
            },
        },
        secrets_dir=tmp_path / "secrets",
    )
    config = ExecConfig(
        cmd=["true"],
        env={},
        cwd=str(tmp_path / "sbx_2"),
        stdin_enabled=False,
    )
    monkeypatch.delenv("E2B_IDENTITY_TOKEN_openai", raising=False)
    with pytest.raises(RuntimeError, match="E2B_IDENTITY_TOKEN_openai"):
        executor._build_sandbox(config)

    monkeypatch.setenv("E2B_IDENTITY_TOKEN_openai", "sk-token")
    kwargs = executor._build_sandbox(config)
    assert kwargs.http_inject[0]["secret"] == "env:E2B_IDENTITY_TOKEN_openai"
    assert kwargs.net_deny == []
    assert kwargs.http_allow == ["* api.example.com/*"]
    # netns-free fork line: the executor no longer forwards a netns flag.
    assert not hasattr(kwargs, "netns")

    # Dynamic update replaces the policy for the next command.
    executor.update_network({"denyOut": ["169.254.169.254"]})
    kwargs = executor._build_sandbox(config)
    assert kwargs.net_allow == []
    assert kwargs.net_deny == ["169.254.169.254"]


def test_netns_flag_accepted_but_not_passed_through(monkeypatch):
    """Wildcard allowOut is accepted without an egress proxy (the fork
    sandlock's unprivileged DNS gateway serves it; the deployment default is
    already per-sandbox netns through `E2B_ENABLE_NET_ISOLATION` +
    `E2B_FD_INJECT_CONNECT`, and only the arm lane still runs the shared
    netns). The netns-free fork line dropped the netns *kwarg* (the legacy
    worker-side path), so enable_netns/E2B_ENABLE_NETNS are accepted for
    config compatibility but no netns kwarg reaches the sandbox."""
    from gateway_common import network

    monkeypatch.setenv("E2B_ENABLE_NETNS", "1")
    normalized = network.normalize_network_config(
        {"allowOut": ["*.example.com:443"], "denyOut": ["10.0.0.0/8"]}
    )
    assert normalized == {"allowOut": ["*.example.com:443"], "denyOut": ["10.0.0.0/8"]}

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
        enable_netns=True,
        network=normalized,
    )
    config = ExecConfig(cmd=["true"], env={}, cwd="/tmp/ws", stdin_enabled=False)
    kwargs = executor._build_sandbox(config)
    assert not hasattr(kwargs, "netns")
    assert kwargs.net_allow == ["*.example.com:443"]


def test_executor_mints_iam_jwt_for_registered_token(tmp_path, monkeypatch):
    """A ${e2b.identity.tokens.<name>} placeholder resolves to a minted
    JWT-SVID when the sandbox registered an iam token for that name (falls
    back to the worker env only when no iam token is registered)."""
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    monkeypatch.delenv("E2B_IDENTITY_TOKEN_openai", raising=False)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    executor = SandlockExecutor(
        workspace_dir=str(tmp_path / "ws"),
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=True,
        iam_tokens={
            "openai": {"audience": "test-aud", "token_type": "JWT-SVID"}
        },
        network={
            "rules": {
                "api.example.com": [
                    {
                        "transform": {
                            "headers": {
                                "Authorization": (
                                    "Bearer ${e2b.identity.tokens.openai}"
                                )
                            }
                        }
                    }
                ]
            }
        },
        secrets_dir=str(secrets),
    )
    kwargs = executor._build_sandbox(
        ExecConfig(
            cmd=["true"],
            env={},
            cwd=str(tmp_path / "ws"),
            stdin_enabled=False,
        )
    )
    secret = kwargs.http_inject[0]["secret"]
    assert secret.startswith("file:"), secret
    import base64
    import json

    jwt = Path(secret.split(":", 1)[1]).read_text(encoding="utf-8").strip()
    # The placeholder was substituted in place: "Bearer ${...}" -> "Bearer <jwt>".
    jwt = jwt.split("Bearer ", 1)[1]
    header, payload, signature = jwt.split(".")

    def _b64decode(part: str) -> dict:
        return json.loads(
            base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        )

    assert _b64decode(header)["alg"] == "HS256"
    assert _b64decode(payload)["aud"] == "test-aud"
    assert signature


def test_wildcard_allowout_accepted_without_netns_flag(monkeypatch):
    """Wildcard allowOut no longer requires E2B_ENABLE_NETNS: the fork's
    unprivileged DNS gateway serves it. The deployment default is already
    per-sandbox netns (`E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`);
    the shared netns survives only on the arm lane."""
    from gateway_common import network

    monkeypatch.delenv("E2B_ENABLE_NETNS", raising=False)
    normalized = network.normalize_network_config(
        {"allowOut": ["*.example.com:443"]}
    )
    assert normalized == {"allowOut": ["*.example.com:443"]}
