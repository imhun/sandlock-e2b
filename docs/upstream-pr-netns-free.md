# Upstream PR: unprivileged domain-wildcard networking + header inject / host mask

> Branch: `imhun/sandlock` → `upstream-pr/netns-free-clean` (base:
> `multikernel/sandlock` `main` @ `f6a3e39`). Purely unprivileged — no
> `CLONE_NEWNET` / veth / `CAP_NET_ADMIN`.

## Summary

Two unprivileged network features, implemented entirely in the supervisor's
seccomp user-notification path (no per-sandbox netns, no privileges beyond what
sandlock already requires):

1. **Domain-suffix wildcards in `net_allow`** — `*.example.com[:port]` matches
   any subdomain at connect/send time. The sandbox resolves wildcard subdomains
   through a per-sandbox loopback DNS gateway (`127.0.1.x:53`) that answers A
   queries with synthetic IPs (`10.250.0.0/16`); the supervisor reverse-looks
   the synthetic address, matches the suffix, resolves the real hostname
   dial-time, and re-verifies the resolved IP (SSRF guard: private/loopback/
   link-local/CGNAT/ULA refused). Direct connects to synthetic addresses are
   refused, so the range cannot be borrowed to bypass literal rules.
2. **HTTP(S) header injection + host masking (Block B)** — `http_auth`
   credential rules (`env:`/`file:`/`fd:` secret sources, bearer/basic/header/
   apikey/query shapes, `replace`/`add-only`) injected by the MITM proxy after
   the ACL check; `host_mask` rewrites the wire `Host` header
   (`${PORT}` substituted). HTTP ACL `*.suffix` host matching (subdomains only,
   not the bare apex).

Both are exercised hermeticly: unit tests for the verdicts/matcher/SOCKS5 wire
protocol, and integration tests that run real sandboxes against local
fixtures (loopback DNS gateway, transparent proxy, no external network).

## Commits (all on `upstream-pr/netns-free-clean`)

- `d3a28cc` Block A (domain wildcards + loopback DNS gateway) + Block B
  (header inject / host mask / HTTP `*.suffix`) — the fork's tree with the
  per-sandbox netns/veth machinery removed.
- `55709f2` Block C — SOCKS5 egress on-behalf (unprivileged).
- `b6ef050` non-root test setup: shared pre-seeded fixtures +
  `stderr_tee` fallback, so the whole suite passes as an unprivileged uid.

The optional per-sandbox netns branch stays on the fork and is deliberately
excluded.

## Verification

All green **as an unprivileged user** (uid 65534; the container entrypoint
does the one-time root prep — sysctl for the `:53` gateway + pre-seeded
`198.18.0.x`/`/etc/hosts` fixtures — then drops privileges):

```text
sandlock-core lib:       770 passed, 0 failed
integration (serial):    432 passed, 0 failed
python:                  414 passed, 16 skipped (/usr/bin/python3 absent)
```

## Notes for review

- The DNS gateway binds a per-sandbox `127.0.1.x` loopback port — no root
  network privileges; each sandbox gets its own address because resolv.conf
  cannot carry a port.
- The netlink view adds one fixed documentation-only interface (`192.0.2.1`,
  RFC 5737) so `getaddrinfo`/`AI_ADDRCONFIG` performs DNS; the address is
  unreachable and leaks nothing.
- Fail closed everywhere: synthetic-IP direct connects, DNS rebinding to
  private ranges, unresolvable wildcard targets, and HTTP-rule matchers with
  malformed wildcards are refused at parse/build/connect time.
- The fork keeps a separate `feature/network-netns` branch (per-sandbox
  veth + loopback isolation, requires `CAP_NET_ADMIN` + `CAP_SYS_ADMIN`) and a
  SOCKS5 egress-proxy branch; both are intentionally not part of this PR.

## Push / create

```bash
cd tmp/sandlock-src
git push origin upstream-pr/netns-free-clean
# then open a PR: multikernel/sandlock main ← imhun/sandlock upstream-pr/netns-free-clean
```

> ⚠️ 本环境无法推送：仓库已备好（`upstream-pr/netns-free-clean`，tip `b6ef050`），
> 但当前 `GITHUB_TOKEN` 是只读 PAT（API 写操作返回 403 "Resource not
> accessible"），git push 同样 403。需要换一个有写权限的 token 或手动推送后
> 再开 PR。
