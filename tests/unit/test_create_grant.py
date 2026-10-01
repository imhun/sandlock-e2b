"""建箱授权（`materialize-tree`）的载荷与签名。

这张授权的形状决定了"谁能让 agent 动特权文件操作"，所以它自己的契约要钉死：
签名覆盖规范化的载荷（CP 签、面 B 验，同一份代码）；载荷里逐字写着**控制面推导出来的**
路径与 uid（worker 不能自己填）；并且它的寿命有上限 —— 面 B 拒绝任何 `exp - iat > 60 s`
的授权，这样"铸权端写错一个数"不会变成一张长期有效的特权凭证。

每条拒绝都要有**具名**的 reason：面 B 的日志与 worker 的重试规则都按它分支
（`already used` ⇒ 重新领一张；其余 ⇒ 建箱失败并说明原因）。
"""

from __future__ import annotations

import base64
import json

import pytest

from gateway_common.create_grant import (
    MAX_TTL_S,
    OP,
    GrantRefusal,
    mint,
    verify,
)

SECRET = "sekret"
HOST = "node-a"


def _payload(**overrides) -> dict:
    payload = {
        "v": 1,
        "host": HOST,
        "sandbox_id": "sbx_0123456789abcdef",
        "op": OP,
        "tree": {
            "path": "/var/lib/e2b-sandboxes/workspaces/sbx_0123456789abcdef",
            "subdir": "workspace",
            "mode": "0770",
            "uid": 10000,
            "gid": 65534,
        },
        "slices": [],
        "jti": "0123456789abcdef",
        "iat": 1000,
        "exp": 1010,
    }
    payload.update(overrides)
    return payload


def _token(**overrides) -> str:
    return mint(_payload(**overrides), secret=SECRET)


def _refusal(token: str, *, host: str = HOST, now: float = 1005) -> str:
    with pytest.raises(GrantRefusal) as excinfo:
        verify(token, secret=SECRET, host=host, now=now)
    return excinfo.value.reason


def test_round_trip_returns_the_payload() -> None:
    assert verify(_token(), secret=SECRET, host=HOST, now=1005) == _payload()


def test_the_signature_is_over_the_canonical_form() -> None:
    """Same payload, different insertion order ⇒ same token."""
    first = mint(_payload(), secret=SECRET)
    shuffled = {key: _payload()[key] for key in reversed(list(_payload()))}
    assert mint(shuffled, secret=SECRET) == first


def test_a_tampered_payload_is_refused() -> None:
    head, signature = _token().split(".")
    payload = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    payload["tree"]["uid"] = 10001
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    assert _refusal(f"{forged}.{signature}") == "bad-signature"


@pytest.mark.parametrize("token", ["no-dot", "a.b.c", ".", "!!!.???"])
def test_a_malformed_token_is_refused(token: str) -> None:
    assert _refusal(token) == "bad-format"


def test_a_wrong_version_is_refused() -> None:
    assert _refusal(_token(v=2)) == "bad-format"


def test_another_hosts_grant_is_refused() -> None:
    assert _refusal(_token(), host="node-b") == "wrong-host"


def test_an_expired_grant_is_refused() -> None:
    assert _refusal(_token(), now=1011) == "expired"


def test_a_grant_from_the_future_is_refused() -> None:
    assert _refusal(_token(), now=990) == "not-yet-valid"


def test_a_long_lived_grant_is_refused() -> None:
    assert _refusal(_token(exp=1000 + MAX_TTL_S + 1)) == "ttl-too-long"


def test_another_op_is_refused() -> None:
    assert _refusal(_token(op="chown-workspace")) == "op-not-allowed"
