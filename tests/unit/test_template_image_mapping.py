"""templateID -> base image resolution and unknown-template rejection."""

from __future__ import annotations

import pytest

from control_plane.config import Settings


def test_base_maps_to_base_image():
    settings = Settings(base_image="python:3.11-slim")
    assert settings.resolve_template_image("base") == "python:3.11-slim"


def test_unknown_template_returns_none():
    settings = Settings(base_image="python:3.11-slim")
    assert settings.resolve_template_image("nope") is None


def test_template_images_take_precedence():
    settings = Settings(
        base_image="python:3.11-slim",
        template_images={"node22": "node:22-slim", "base": "custom:base"},
    )
    assert settings.resolve_template_image("node22") == "node:22-slim"
    assert settings.resolve_template_image("base") == "custom:base"


def test_template_images_parsed_from_env(monkeypatch):
    import json

    monkeypatch.setenv("E2B_TEMPLATE_IMAGES", json.dumps({"py312": "python:3.12-slim"}))
    settings = Settings()
    assert settings.template_images == {"py312": "python:3.12-slim"}

