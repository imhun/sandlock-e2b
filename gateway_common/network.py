"""Network configuration shared by the control plane and the envd service.

Mirrors the official E2B wire shape (camelCase, matching what the generated
SDK clients send):

    {
        "allowOut": ["8.8.8.8", "example.com", "10.0.0.0/8"],
        "denyOut": ["169.254.169.254"],
        "allowPublicTraffic": true,
        "rules": {"api.example.com": []},
    }

``egressProxy`` is tunneled by the sandlock fork's SOCKS5 on-behalf path,
``maskRequestHost`` and ``rules[].transform.headers`` are mapped onto the
fork's ``host_mask`` / ``http_inject`` kwargs (requires the fork wheel —
PyPI 0.8.6 rejects those kwargs).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any


class NetworkConfigError(ValueError):
    """Raised when a network configuration is invalid or unsupported."""


_CREATE_FIELDS = {
    "allowOut",
    "denyOut",
    "allowPublicTraffic",
    "rules",
    "maskRequestHost",
    "egressProxy",
}

_UPDATE_FIELDS = {
    "allowOut",
    "denyOut",
    "allowInternetAccess",
    "allow_internet_access",  # the generated SDK sends snake_case here
    "rules",
    "egressProxy",
}


def _check_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise NetworkConfigError(
            f"unknown network field(s) in {where}: {sorted(unknown)}"
        )


def _normalize_egress_proxy(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the user-provided SOCKS5 proxy.

    The address must resolve to a public IPv4 endpoint: private / loopback /
    link-local ranges are rejected before the sandbox exists (SSRF guard,
    matching the official API). The runtime tunnels all egress through it.
    """
    proxy = raw.get("egressProxy")
    if proxy is None:
        return None
    if not isinstance(proxy, dict):
        raise NetworkConfigError("egressProxy must be an object")
    address = proxy.get("address")
    if not isinstance(address, str) or not address:
        raise NetworkConfigError("egressProxy.address is required")
    host, _, port_s = address.rpartition(":")
    if not host or not port_s.isdigit():
        raise NetworkConfigError(f"invalid egressProxy address: {address!r}")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise NetworkConfigError("egressProxy port must be in 1-65535")
    for key in ("username", "password"):
        value = proxy.get(key)
        if value is not None and (
            not isinstance(value, str) or len(value) > 255
        ):
            raise NetworkConfigError(
                f"egressProxy.{key} must be a string of at most 255 bytes"
            )
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise NetworkConfigError(
            f"egressProxy address {address!r} does not resolve"
        ) from None
    ipv4 = [info[4][0] for info in infos if info[0] == socket.AF_INET]
    if not ipv4:
        raise NetworkConfigError(
            f"egressProxy {address!r} must resolve to an IPv4 address"
        )
    for ip in ipv4:
        addr = ipaddress.ip_address(ip)
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise NetworkConfigError(
                f"egressProxy {address!r} resolves to a non-public address "
                f"({ip})"
            )
    out: dict[str, Any] = {"address": address}
    if proxy.get("username") is not None:
        out["username"] = proxy["username"]
    if proxy.get("password") is not None:
        out["password"] = proxy["password"]
    return out


def _check_mask_request_host(raw: dict[str, Any]) -> None:
    mask = raw.get("maskRequestHost")
    if mask is None:
        return
    if not isinstance(mask, str) or not mask.strip():
        raise NetworkConfigError("maskRequestHost must be a non-empty string")
    mask = mask.strip()
    if any(c.isspace() for c in mask):
        raise NetworkConfigError("maskRequestHost must not contain whitespace")
    if "://" in mask or "@" in mask or "/" in mask:
        raise NetworkConfigError(
            "maskRequestHost must be a host[:port] value "
            "(no scheme, userinfo, or path)"
        )


def _normalize_rules(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]] | None:
    rules = raw.get("rules")
    if rules is None:
        return None
    if not isinstance(rules, dict):
        raise NetworkConfigError("rules must be an object keyed by domain")
    out: dict[str, list[dict[str, Any]]] = {}
    for domain, rule_list in rules.items():
        if not isinstance(domain, str) or not domain:
            raise NetworkConfigError("rules keys must be non-empty domains")
        if not isinstance(rule_list, list):
            raise NetworkConfigError(f"rules[{domain}] must be a list")
        normalized: list[dict[str, Any]] = []
        for rule in rule_list:
            if not isinstance(rule, dict):
                raise NetworkConfigError(f"rules[{domain}] entries must be objects")
            transform = rule.get("transform")
            if transform is None:
                continue
            if not isinstance(transform, dict):
                raise NetworkConfigError(
                    f"rules[{domain}].transform must be an object"
                )
            unknown_transform = set(transform) - {"headers"}
            if unknown_transform:
                raise NetworkConfigError(
                    f"rules[{domain}].transform unsupported field(s): "
                    f"{sorted(unknown_transform)}"
                )
            headers = transform.get("headers")
            if headers is None:
                continue
            if not isinstance(headers, dict) or not headers:
                raise NetworkConfigError(
                    f"rules[{domain}].transform.headers must be a non-empty "
                    "object of header name -> value"
                )
            for name, value in headers.items():
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(value, str)
                    or not value
                ):
                    raise NetworkConfigError(
                        f"rules[{domain}].transform.headers entries must be "
                        "non-empty strings"
                    )
            normalized.append({"transform": {"headers": dict(headers)}})
        out[domain] = normalized
    return out


def _normalize_str_list(
    value: Any, name: str, *, allow_wildcard_domain: bool
) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise NetworkConfigError(f"{name} must be a list of strings")
    out: list[str] = []
    for entry in value:
        entry = entry.strip()
        if not entry:
            raise NetworkConfigError(f"{name} entries must not be empty")
        if entry.startswith("*.") and not allow_wildcard_domain:
            raise NetworkConfigError(
                f"{name} wildcard domains ({entry}) are not supported by the "
                "sandlock runtime"
            )
        out.append(entry)
    return out


def _normalize_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise NetworkConfigError(f"{name} must be a boolean")
    return value


def normalize_network_config(raw: Any) -> dict[str, Any] | None:
    """Validate a create-body ``network`` object into its canonical form."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise NetworkConfigError("network must be an object")
    _check_unknown(raw, _CREATE_FIELDS, "network")
    egress_proxy = _normalize_egress_proxy(raw)
    _check_mask_request_host(raw)
    rules = _normalize_rules(raw)
    # Wildcard domains (``*.example.com``) are expressible either through the
    # egress proxy library (in-sandbox filtering) or through the fork
    # sandlock's net_allow + per-sandbox DNS gateway (the unprivileged
    # shared-netns path; the fork tracks the netns-free upstream PR line, so
    # there is no per-sandbox netns mode anymore).
    allow_out = _normalize_str_list(
        raw.get("allowOut"),
        "allowOut",
        allow_wildcard_domain=True,
    )
    deny_out = _normalize_str_list(
        raw.get("denyOut"), "denyOut", allow_wildcard_domain=False
    )
    _validate_deny_ips(deny_out)
    allow_public_traffic = _normalize_bool(
        raw.get("allowPublicTraffic"), "allowPublicTraffic"
    )
    out: dict[str, Any] = {}
    if allow_out is not None:
        out["allowOut"] = allow_out
    if deny_out is not None:
        out["denyOut"] = deny_out
    if allow_public_traffic is not None:
        out["allowPublicTraffic"] = allow_public_traffic
    if egress_proxy is not None:
        out["egressProxy"] = egress_proxy
    if rules is not None:
        out["rules"] = rules
    if raw.get("maskRequestHost") is not None:
        out["maskRequestHost"] = raw["maskRequestHost"].strip()
    return out


def normalize_network_update(raw: Any) -> dict[str, Any] | None:
    """Validate a ``PUT /sandboxes/{id}/network`` body.

    Semantics follow the official API: the update replaces the egress
    configuration atomically, and omitted fields are cleared.
    """
    if raw is None or not isinstance(raw, dict):
        raise NetworkConfigError("network update body must be an object")
    _check_unknown(raw, _UPDATE_FIELDS, "network update")
    egress_proxy = _normalize_egress_proxy(raw)
    allow_out = _normalize_str_list(
        raw.get("allowOut"),
        "allowOut",
        allow_wildcard_domain=True,
    )
    deny_out = _normalize_str_list(
        raw.get("denyOut"), "denyOut", allow_wildcard_domain=False
    )
    _validate_deny_ips(deny_out)
    allow_internet_access = _normalize_bool(
        raw.get("allowInternetAccess", raw.get("allow_internet_access")),
        "allowInternetAccess",
    )
    out: dict[str, Any] = {}
    if allow_out is not None:
        out["allowOut"] = allow_out
    if deny_out is not None:
        out["denyOut"] = deny_out
    if allow_internet_access is not None:
        out["allowInternetAccess"] = allow_internet_access
    if "egressProxy" in raw:
        # Explicit null clears the proxy (atomic-replace semantics).
        out["egressProxy"] = egress_proxy
    rules = _normalize_rules(raw)
    if rules is not None:
        out["rules"] = rules
    return out


def _validate_deny_ips(deny_out: list[str] | None) -> None:
    """denyOut only accepts IP/CIDR entries (the official API rejects
    domain names there; sandlock's ``net_deny`` does the same)."""
    if deny_out is None:
        return
    for entry in deny_out:
        host = entry.split(":", 1)[0]
        try:
            ipaddress.ip_network(host, strict=False)
        except ValueError:
            raise NetworkConfigError(
                f"denyOut only accepts IP/CIDR entries, got {entry!r}"
            ) from None


def sandlock_network_policy(
    network: dict[str, Any] | None,
    *,
    allow_internet_access: bool,
    enable_network: bool,
    private_deny_cidrs: list[str] | None = None,
) -> dict[str, Any]:
    """Map a normalized network config onto sandlock net/http primitives.

    Returns the subset of ``Sandbox`` kwargs this project controls:
    ``net_allow`` / ``net_deny`` / ``http_allow`` / ``http_inject`` /
    ``host_mask`` / ``egress_proxy``. ``net_allow`` and ``net_deny`` are
    mutually exclusive in sandlock, so when both ``allowOut`` and ``denyOut``
    are present the allowlist model wins and allow entries covered by a deny
    CIDR are dropped (deny precedence).

    ``private_deny_cidrs`` only affects the *implicit* full-egress branch
    (no explicit ``allowOut``/``denyOut`` with internet allowed): instead of
    ``net_allow=["*:*"]`` the policy becomes a ``net_deny`` DenyList
    (default-allow for the public internet, private/loopback/link-local
    ranges refused). Explicit ``allowOut`` entries are never filtered here,
    so a caller that deliberately grants a private IP/CIDR keeps it.
    """
    if not enable_network:
        return {
            "net_allow": [],
            "net_deny": [],
            "http_allow": [],
            "http_inject": [],
            "host_mask": None,
            "egress_proxy": None,
        }
    if network is None:
        return {
            "net_allow": [],
            "net_deny": [],
            "http_allow": [],
            "http_inject": [],
            "host_mask": None,
            "egress_proxy": None,
        }

    allow_out = network.get("allowOut")
    deny_out = network.get("denyOut")
    allow_internet = network.get("allowInternetAccess")
    if allow_internet is None:
        allow_internet = allow_internet_access

    http_allow: list[str] = []
    for domain in (network.get("rules") or {}):
        if not domain or domain.startswith("*"):
            continue
        http_allow.append(f"* {domain}/*")

    # Block B (5B.4): rules[domain].transform.headers → http_inject. The
    # literal header values are materialized into supervisor-only secret files
    # by the executor (sandlock refuses inline literals); `${e2b.identity.tokens.*}`
    # placeholders map to E2B_IDENTITY_TOKEN_<NAME> env vars.
    http_inject: list[dict[str, Any]] = []
    for domain, rule_list in (network.get("rules") or {}).items():
        if not domain or domain.startswith("*"):
            continue
        for rule in rule_list:
            headers = (rule.get("transform") or {}).get("headers") or {}
            for name, value in headers.items():
                safe_domain = "".join(
                    c if c.isalnum() else "_" for c in domain
                ).lower()
                safe_name = "".join(c if c.isalnum() else "_" for c in name).lower()
                http_inject.append(
                    {
                        "matcher": domain,
                        "auth": f"header:{name}",
                        "value": value,
                        "name": f"hdr_{safe_domain}_{safe_name}",
                        "on_existing": "replace",
                    }
                )

    egress_proxy = network.get("egressProxy")
    if allow_out is not None:
        return {
            "net_allow": _to_net_allow(allow_out, deny_out),
            "net_deny": [],
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": network.get("maskRequestHost"),
            "egress_proxy": egress_proxy,
        }
    if deny_out is not None:
        return {
            "net_allow": [],
            "net_deny": [_to_net_deny(e) for e in deny_out],
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": network.get("maskRequestHost"),
            "egress_proxy": egress_proxy,
        }
    if not allow_internet:
        return {
            "net_allow": [],
            "net_deny": [],
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": network.get("maskRequestHost"),
            "egress_proxy": egress_proxy,
        }
    if private_deny_cidrs:
        # Sandlock resolves net_deny into a per-protocol DenyList
        # (default-allow + denied CIDRs); http_allow keeps working through
        # the HTTP-ACL transparent proxy independently of the net policy.
        return {
            "net_allow": [],
            "net_deny": list(private_deny_cidrs),
            "http_allow": http_allow,
            "http_inject": http_inject,
            "host_mask": network.get("maskRequestHost"),
            "egress_proxy": egress_proxy,
        }
    return {
        "net_allow": ["*:*"],
        "net_deny": [],
        "http_allow": http_allow,
        "http_inject": http_inject,
        "host_mask": network.get("maskRequestHost"),
        "egress_proxy": egress_proxy,
    }


def _to_net_allow(
    allow_out: list[str], deny_out: list[str] | None
) -> list[str]:
    denied = [
        ipaddress.ip_network(e.split(":", 1)[0], strict=False)
        for e in (deny_out or [])
    ]
    rules: list[str] = []
    for entry in allow_out:
        host = entry.split(":", 1)[0]
        if "/" in host:
            try:
                net = ipaddress.ip_network(host, strict=False)
            except ValueError:
                net = None
            if net is not None and any(net.subnet_of(d) for d in denied):
                continue
        elif ":" not in entry:
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                addr = None
            if addr is not None and any(addr in d for d in denied):
                continue
        rules.append(_net_entry(entry))
    return rules


def _net_entry(entry: str) -> str:
    if entry.startswith(("tcp://", "udp://", "icmp://")):
        return entry
    if ":" in entry.split("/", 1)[0]:
        # Explicit port(s) (host:port or CIDR:port) — pass through.
        return entry
    return f"tcp://{entry}:*"


def _to_net_deny(entry: str) -> str:
    return entry.split(":", 1)[0]
