"""Worker startup pre-warms the base image AND template images."""

from __future__ import annotations

import asyncio

from envd_service.app import _warm_base_image
from envd_service.config import Settings


def test_warm_base_image_warms_base_and_templates(monkeypatch) -> None:  # noqa: ANN001
    import envd_service.runtime.image_resolver as resolver

    calls: list[str] = []

    def fake_resolve(image, cache_dir, **kwargs) -> None:  # noqa: ANN001, ANN003
        calls.append(image)

    monkeypatch.setattr(resolver, "resolve_image_rootfs", fake_resolve)
    settings = Settings(
        base_image="registry.example.com/python:3.14-slim",
        template_images={
            "py311": "registry.example.com/python:3.11-slim",
            "node22": "registry.example.com/node:22-slim",
        },
    )

    asyncio.run(_warm_base_image(settings))

    assert calls == [
        "registry.example.com/python:3.14-slim",
        "registry.example.com/python:3.11-slim",
        "registry.example.com/node:22-slim",
    ]


def test_warm_base_image_deduplicates(monkeypatch) -> None:  # noqa: ANN001
    import envd_service.runtime.image_resolver as resolver

    calls: list[str] = []

    def fake_resolve(image, cache_dir, **kwargs) -> None:  # noqa: ANN001, ANN003
        calls.append(image)

    monkeypatch.setattr(resolver, "resolve_image_rootfs", fake_resolve)
    settings = Settings(
        base_image="img:base",
        template_images={"t1": "img:base", "t2": "img:other"},
    )

    asyncio.run(_warm_base_image(settings))
    assert calls == ["img:base", "img:other"]
