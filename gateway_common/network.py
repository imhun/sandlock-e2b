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


class NetworkUpdateConflictError(ValueError):
    """D4=A: a live-instance network update is not expressible.

    Raised by the executor (and propagated through the runtime context) when
    an already-launched ``SandboxInstance`` cannot represent the proposed
    state. Every layer maps it to an HTTP 409 **before** persisting its own
    record, so neither the control-plane registry nor the worker runtime copy
    changes on rejection.
    """


_INSTANCE_STATIC_FIELDS = (
    "rules",
    "egressProxy",
    "maskRequestHost",
    "allowPublicTraffic",
)


def merged_network_state(
    network: dict[str, Any] | None,
    *,
    allow_internet_access: bool,
    allow_public_traffic: bool = False,
) -> dict[str, Any]:
    """Canonical full network state for D4=A live-update comparisons.

    The merged network dict is folded together with the two record mirrors
    (``allowInternetAccess`` / ``allowPublicTraffic``) into one dictionary so
    a static/proposed pair is compared deterministically regardless of which
    side carried a field in the dict. Explicit nulls (``egressProxy: null``
    after an atomic-replace update) are dropped like the normalization layer
    treats them.
    """
    state = dict(network) if network else {}
    state["allowInternetAccess"] = bool(allow_internet_access)
    state.setdefault("allowPublicTraffic", bool(allow_public_traffic))
    for key in ("rules", "egressProxy", "maskRequestHost"):
        if key in state and state[key] is None:
            del state[key]
    return state


def _egress_model(state: dict[str, Any]) -> str:
    """Classify the outbound model of a canonical network state.

    Presence of ``allowOut`` wins over ``denyOut`` (matching
    :func:`sandlock_network_policy`'s allowlist-precedence); with neither
    list the ``allowInternetAccess`` flag selects the implicit default-allow
    or deny-all model.
    """
    if state.get("allowOut") is not None:
        return "allowOut"
    if state.get("denyOut") is not None:
        return "denyOut"
    if not state.get("allowInternetAccess"):
        return "deny-all implicit"
    return "default-allow implicit"


def _is_bare_ip_literal(entry: Any) -> bool:
    """Whether an ``allowOut`` entry is a bare IP the fork can bind online.

    The fork ``SandboxInstance.update_network(ips)`` parses every entry as
    ``std::net::IpAddr``: domains, CIDRs and host:port forms are not
    expressible through the session verb.
    """
    if not isinstance(entry, str):
        return False
    try:
        ipaddress.ip_address(entry)
    except ValueError:
        return False
    return True


def network_update_conflict_reason(
    static: dict[str, Any],
    proposed: dict[str, Any],
) -> str | None:
    """Return why ``proposed`` cannot be applied to a launched instance.

    ``static``/``proposed`` are canonical states from
    :func:`merged_network_state`. ``None`` means the update is expressible.
    The comparison is monotone against the **currently applied** state (which
    starts as the first-launch snapshot): after a live narrowing, re-widening
    toward the launch ceiling is still rejected (the D4=A contract forbids
    ``allowOut`` ``[]`` -> ``["8.8.8.8"]`` even though the latter equals the
    launch-time ceiling).
    """
    if static == proposed:
        return None
    static_model = _egress_model(static)
    if static_model != _egress_model(proposed):
        return (
            f"network egress model cannot change on a launched sandbox "
            f"({static_model} -> {_egress_model(proposed)})"
        )
    for key in _INSTANCE_STATIC_FIELDS:
        if static.get(key) != proposed.get(key):
            return f"network field {key} cannot change on a launched sandbox"
    if static.get("allowInternetAccess") != proposed.get("allowInternetAccess"):
        return "allowInternetAccess cannot change on a launched sandbox"
    if static_model == "allowOut":
        return _allow_out_conflict(static, proposed)
    if static_model == "denyOut":
        return _deny_out_conflict(static, proposed)
    return "an implicit egress model has no expressible online narrowing"


def _allow_out_conflict(
    static: dict[str, Any],
    proposed: dict[str, Any],
) -> str | None:
    static_list = list(static.get("allowOut") or [])
    proposed_list = list(proposed.get("allowOut") or [])
    if not set(proposed_list) <= set(static_list):
        return "allowOut can only be narrowed on a launched sandbox"
    # The fork binds ip literals only: every entry the update removes or
    # keeps must be a bare IP, or the online state would silently differ from
    # the textual record (domain/CIDR-level narrowing is not expressible).
    changed = set(static_list) ^ set(proposed_list)
    kept = set(proposed_list)
    for entry in sorted(changed | kept):
        if not _is_bare_ip_literal(entry):
            return (
                f"allowOut entry {entry!r} cannot be expressed on a launched "
                "sandbox (ip literals only)"
            )
    return None


def _deny_out_conflict(
    static: dict[str, Any],
    proposed: dict[str, Any],
) -> str | None:
    static_list = list(static.get("denyOut") or [])
    proposed_list = list(proposed.get("denyOut") or [])
    if not set(static_list) <= set(proposed_list):
        return "denyOut can only grow (deny more) on a launched sandbox"
    if set(proposed_list) != set(static_list):
        # The fork verb replaces the outbound allow set for new execs; a
        # DenyList ceiling cannot gain entries at runtime, so accepting the
        # update would leave the instance granting destinations the record
        # claims are denied. D4=A rejects instead of drifting.
        return (
            "adding denyOut entries on a launched sandbox is not expressible "
            "(the fork binds ip allowlists only)"
        )
    return None


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
    # shared-netns path; with E2B_ENABLE_NET_ISOLATION the DNS gateway binds
    # inside the sandbox's own netns — S2.3 — and the same rules apply).
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

    ``private_deny_cidrs`` applies to **both** egress models:

    * implicit full egress (no explicit ``allowOut``/``denyOut`` with internet
      allowed) becomes a ``net_deny`` DenyList instead of ``net_allow=["*:*"]``
      -- default-allow for the public internet, private/loopback/link-local
      ranges refused;
    * an explicit ``allowOut`` list is filtered against the same set before it
      reaches the fork (SEC-K0S-004): a literal IP/CIDR that falls inside a
      protected range is dropped, because the worker performs the connect in
      its own netns and an unfiltered allow entry is a route into the control
      plane. Only literals can be judged here -- a *domain* entry is resolved
      later by the fork, so it is not covered by this filter.
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
        # SEC-K0S-004 (2026-10-01): the explicit allowlist branch is not a way
        # around the private-range protection. Entries covered by the
        # protected set are dropped here -- deny precedence, exactly as an
        # explicit ``denyOut`` entry already drops them -- because the connect
        # is performed by the worker in its *own* network namespace, so a
        # tenant that names the cluster's pod/service CIDR reaches the control
        # plane and the worker's envd from inside the sandbox. Measured on the
        # live cluster before this fix: ``allowOut: ["10.244.0.0/16"]`` fetched
        # ``/openapi.json`` from 10.244.140.28:3000.
        protected = list(deny_out or []) + list(private_deny_cidrs or [])
        return {
            "net_allow": _to_net_allow(allow_out, protected),
            # ... and hand the fork the same protected set as a deny filter, so
            # a destination that only exists *after* resolution is covered too:
            # an allowlist entry may be a hostname, and the address that name
            # finally answers with is chosen at connect time (measured before
            # the fork grew this: ``allowOut: ["10.244.140.26.nip.io:49983"]``
            # reached the worker's envd). The fork applies it with deny
            # precedence; with an empty protected set this stays ``[]`` and the
            # policy is a plain allowlist.
            "net_deny": list(private_deny_cidrs or []),
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
    denied = [net for e in (deny_out or []) if (net := _parse_network(e))]
    rules: list[str] = []
    for entry in allow_out:
        target = _parse_target(_allow_entry_host(entry))
        if target is not None and any(_intersects(target, d) for d in denied):
            continue
        rules.append(_net_entry(entry))
    return rules


_SCHEME_PREFIXES = ("tcp://", "udp://", "icmp://")


def _allow_entry_host(entry: str) -> str:
    """The address/network an ``allowOut`` entry targets, without scheme/port.

    ``10.0.0.0/8``, ``10.0.0.0/8:443``, ``tcp://10.1.2.3:80`` and ``[::1]:80``
    all reduce to the address (or network) itself. Domains are returned as-is:
    they are not literal destinations, so the literal deny filter cannot judge
    them (see :func:`_to_net_allow`).
    """
    value = entry
    for prefix in _SCHEME_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if value.startswith("["):  # bracketed IPv6 literal, optional :port
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if "/" in value:  # CIDR, with an optional trailing :port
        network, _, tail = value.partition("/")
        length, sep, _port = tail.partition(":")
        return f"{network}/{length}" if sep else value
    if value.count(":") == 1:  # host:port
        return value.split(":", 1)[0]
    return value


def _parse_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _parse_target(
    value: str,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """A single address as a /32 or /128 network, or ``None`` for a domain."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _intersects(
    target: ipaddress.IPv4Network | ipaddress.IPv6Network,
    denied: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    """Whether ``target`` overlaps ``denied`` at all (family-exact, like the fork).

    Overlap, not containment: an entry *wider* than a protected range (the
    ``0.0.0.0/0`` case) also reaches it, and the tuple grammar has no way to
    say "everything except the protected set". Dropping the entry fails
    closed; keeping it would silently re-permit the cluster's own network.
    """
    if target.version != denied.version:
        return False
    return target.overlaps(denied)


def _net_entry(entry: str) -> str:
    if entry.startswith(("tcp://", "udp://", "icmp://")):
        return entry
    if ":" in entry.split("/", 1)[0]:
        # Explicit port(s) (host:port or CIDR:port) — pass through.
        return entry
    return f"tcp://{entry}:*"


def _to_net_deny(entry: str) -> str:
    return entry.split(":", 1)[0]
