"""建箱授权：一张签名的 `materialize-tree` 计划（CP 签、面 B 验）。

为什么有这个东西（`docs/superpowers/specs/2026-10-01-create-path-grant-design.md`）：
建箱期的材料化（建树 / 快照拷贝 / 改属主 / 卷切片）原先由 worker 自己做、改属主再经控制面
逐次转发给 agent —— 那一次转发在生产上要 ~71 ms（worker→CP→agent→CP→worker 整圈）。
改成"控制面签一张 10 s 的单次授权、worker 直连本节点 agent"之后，控制面仍在**授权**，
只是不再**转发并等待执行**。

三条不变量写在这里，因为它们是这条通道的全部安全性：

* **载荷里的路径与 uid 是控制面推导出来的**（§14.4 硬规则二：worker 只报
  "哪个沙箱、什么动作"，绝不报路径）。worker 拿到的是签名后的结果，改一个字都会破签名。
* **寿命有上限**：面 B 拒绝 `exp - iat > MAX_TTL_S`，所以铸权端把 TTL 配错不会变成
  一张长期有效的特权凭证。默认 10 s 的依据是"只要够 worker 把请求发到 agent"
  （实测集群内一次控制面往返 1.29 ms，两节点钟差 ~16 ms）。
* **拒绝是具名的**：`GrantRefusal.reason` 是 worker 与面 B 分支的依据
  （重放 ⇒ 重新领一张；其余 ⇒ 建箱失败并说明原因）。

单次消费**不在**这里：那需要状态（`jti` 表），由面 B 的入口持有。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

#: 这条通道上唯一的操作：一次把这次建箱要做的材料化全做完。
OP = "materialize-tree"

#: 载荷版本。
VERSION = 1

#: 面 B 接受的最长寿命（秒）。铸权端配得再长也会被这里拒掉。
MAX_TTL_S = 60

#: 允许的时钟偏差（秒）。CP 与 agent 是两台机器，实测两节点钟差 ~16 ms；
#: 留 5 s 是为了让"iat 略在未来"这种正常抖动不被误判。
CLOCK_SKEW_S = 5.0


class GrantRefusal(Exception):
    """一次授权被拒绝，`reason` 是机器可读的具名原因。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    # ``validate=True``: the lenient default silently drops characters outside
    # the alphabet, so ``"!!!"`` would decode to ``b""`` and be reported as a
    # *signature* mismatch. A token that is not base64 is a format problem, and
    # the two reasons send the caller down different paths.
    return base64.b64decode(
        text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
    )


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """The exact bytes the signature covers.

    Sorted keys and no whitespace: CP and agent are different processes (and
    different languages could verify this tomorrow), so "the same payload"
    has to mean the same bytes, not the same dict.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def mint(payload: dict[str, Any], *, secret: str) -> str:
    """Sign one plan. Returns ``<payload>.<signature>`` (both base64url, unpadded)."""
    raw = canonical_payload(payload)
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(signature)}"


def verify(
    token: str,
    *,
    secret: str,
    host: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a token for ``host``; returns the payload or raises `GrantRefusal`."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise GrantRefusal("bad-format", "a token is <payload>.<signature>")
    head, signature = token.split(".")
    if not head or not signature:
        raise GrantRefusal("bad-format", "empty payload or signature")
    try:
        raw = _b64d(head)
        provided = _b64d(signature)
    except (ValueError, base64.binascii.Error) as exc:
        raise GrantRefusal("bad-format", str(exc)) from exc
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided):
        # Signature first: everything below reads the payload, and a payload
        # nobody signed must not influence which reason is reported.
        raise GrantRefusal("bad-signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GrantRefusal("bad-format", str(exc)) from exc
    if not isinstance(payload, dict) or payload.get("v") != VERSION:
        raise GrantRefusal("bad-format", "unknown payload version")
    if payload.get("op") != OP:
        raise GrantRefusal("op-not-allowed", str(payload.get("op")))
    if payload.get("host") != host:
        raise GrantRefusal("wrong-host", str(payload.get("host")))
    iat = payload.get("iat")
    exp = payload.get("exp")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise GrantRefusal("bad-format", "iat/exp must be numbers")
    moment = time.time() if now is None else float(now)
    if iat > moment + CLOCK_SKEW_S:
        raise GrantRefusal("not-yet-valid", f"iat={iat} now={moment}")
    if moment > exp:
        raise GrantRefusal("expired", f"exp={exp} now={moment}")
    lifetime = exp - iat
    if lifetime <= 0 or lifetime > MAX_TTL_S:
        raise GrantRefusal("ttl-too-long", f"exp-iat={lifetime}")
    return payload
