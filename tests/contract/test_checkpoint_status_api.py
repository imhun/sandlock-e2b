"""只读查询：`GET /sandboxes/{id}/checkpoint`（E2B 侧唯一新增的公开面）。

它必须**只读**：pause/resume 的 204 契约一个字都不许动（控制面把 worker 的非 204/404
一律当 502 回滚，`control_plane/api/sandboxes.py:333-338`），所以这条端点是 GET、无副作用、
未接线时给"不知道"而不是报错。
"""

from __future__ import annotations

from gateway_common.paths import sandbox_checkpoint_dir


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


async def test_a_sandbox_without_an_image_says_so(control_client, workspace) -> None:
    sandbox = await _create_sandbox(control_client)
    sid = sandbox["sandboxID"]

    resp = await control_client.get(
        f"/sandboxes/{sid}/checkpoint", headers={"X-API-Key": "local-key"}
    )

    # 整份相等（不做子串判据）：这就是对外承诺的形状。
    assert resp.status_code == 200
    assert resp.json() == {
        "sandboxID": sid,
        "hasImage": False,
        "imageMB": 0,
        "capturedAt": None,
        "lastRestore": None,
    }


async def test_an_image_and_a_restore_are_visible(control_client, workspace) -> None:
    sandbox = await _create_sandbox(control_client)
    sid = sandbox["sandboxID"]
    # 造一张图与一次恢复的结果，形状与 worker 真写的一致（读端点只认这两个位置）
    image = sandbox_checkpoint_dir(workspace, sid) / "latest"
    image.mkdir(parents=True)
    (image / "meta.json").write_text("{}", encoding="utf-8")
    runtime_dir = workspace / "_runtime" / sid
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "last-restore.json").write_text(
        '{"restored": true, "reason": "", "pid": 31337, "unrecoveredFdCount": 2, '
        '"at": "2026-09-26T00:00:00+00:00"}',
        encoding="utf-8",
    )

    resp = await control_client.get(
        f"/sandboxes/{sid}/checkpoint", headers={"X-API-Key": "local-key"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sandboxID"] == sid
    assert body["hasImage"] is True
    assert body["capturedAt"] == int(image.stat().st_mtime)
    assert body["lastRestore"] == {
        "restored": True,
        "reason": "",
        "pid": 31337,
        "unrecoveredFdCount": 2,
        "at": "2026-09-26T00:00:00+00:00",
    }


async def test_an_unknown_sandbox_is_404(control_client) -> None:
    resp = await control_client.get(
        "/sandboxes/sbx_does_not_exist/checkpoint", headers={"X-API-Key": "local-key"}
    )
    assert resp.status_code == 404
