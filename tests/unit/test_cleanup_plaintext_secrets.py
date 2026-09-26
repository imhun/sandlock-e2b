"""O3 (second round): cleanup of plaintext ``_secrets/**`` records.

Task 1 (`dd96266`) only changed *future* writes: the records that were already
on the shared volume as plaintext stay plaintext until something rewrites
them through the registry. This suite pins the one-shot tool that does that
rewrite, removes the residual plaintext copies, verifies the directory, is
idempotent, and refuses to run without a master key.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from control_plane.registry.secrets import SecretRegistry, UnknownSecretError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "cleanup-plaintext-secrets.py"

MASTER_KEY = "cleanup-master-key"
API_VALUE = "api-value-0123456789abcdef0123456789abcdef"
TOKEN_VALUE = "token-value-fedcba9876543210fedcba9876543210"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "cleanup_plaintext_secrets", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cleanup = _load_script()


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _tree(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _plaintext_store(workspace: Path) -> dict[str, str]:
    """The pre-Task-1 state: records written by a registry with no master key."""
    registry = SecretRegistry(workspace / "_secrets")
    ids = {}
    for name, value in (("api_key", API_VALUE), ("token", TOKEN_VALUE)):
        ids[name] = registry.create(name, value).secret_id
    for name, value in (("api_key", API_VALUE), ("token", TOKEN_VALUE)):
        payload = json.loads(
            (workspace / "_secrets" / ids[name] / "secret.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["encrypted"] is False
        assert payload["value"] == value
    return ids


def _run(capsys, args: list[str]):
    code = cleanup.main(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured


def test_refuses_without_a_master_key_and_leaves_the_store_untouched(
    workspace, capsys, monkeypatch
):
    monkeypatch.delenv("E2B_SECRET_MASTER_KEY", raising=False)
    monkeypatch.delenv("E2B_SECRET_MASTER_KEYS", raising=False)
    ids = _plaintext_store(workspace)
    record = workspace / "_secrets" / ids["api_key"] / "secret.json"
    before = record.read_bytes()

    code = cleanup.main(["--workspace-base", str(workspace)])
    captured = capsys.readouterr()

    assert code == 2
    assert record.read_bytes() == before
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "拒绝执行：未配置 E2B_SECRET_MASTER_KEY（--master-key 或环境变量）。",
        "没有主 key 时“重写”仍会落明文：先跑 deploy/k8s-k0s/secrets.sh，"
        "并滚动 control-plane 让主 key 生效。",
    ]


def test_rewrites_existing_plaintext_records_through_the_registry(
    workspace, capsys
):
    ids = _plaintext_store(workspace)

    code, report, captured = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert code == 0
    assert report == {
        "secrets_dir": str((workspace / "_secrets").resolve()),
        "plaintext_records_before": 2,
        "rewritten_plaintext_records": 2,
        "residual_plaintext_copies": [],
        "deleted_files": [],
        "unreadable_records": [],
        "unresolved_plaintext": [],
        "unknown_files": [],
        "verified": True,
        "secrets": [
            {
                "name": "api_key",
                "version": 1,
                "sha256_prefix": _fp(API_VALUE),
            },
            {"name": "token", "version": 1, "sha256_prefix": _fp(TOKEN_VALUE)},
        ],
    }

    for name, value in (("api_key", API_VALUE), ("token", TOKEN_VALUE)):
        payload = json.loads(
            (workspace / "_secrets" / ids[name] / "secret.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["encrypted"] is True
        assert payload["value"].startswith("gAAAAA")
        assert payload["value"] != value

    restarted = SecretRegistry(workspace / "_secrets", master_key=MASTER_KEY)
    assert {r.name: r.value for r in restarted.list()} == {
        "api_key": API_VALUE,
        "token": TOKEN_VALUE,
    }
    for value in (API_VALUE, TOKEN_VALUE):
        leaked = [
            str(p)
            for p in _tree(workspace / "_secrets")
            if value.encode("utf-8") in p.read_bytes()
        ]
        assert leaked == []

    assert [chunk for chunk in (captured.out, captured.err) if API_VALUE in chunk] == []


def test_second_run_is_a_byte_for_byte_no_op(workspace, capsys):
    _plaintext_store(workspace)
    first = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )
    snapshots = {p: p.read_bytes() for p in _tree(workspace / "_secrets")}

    second = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert first[0] == 0
    assert first[1]["rewritten_plaintext_records"] == 2
    assert second[0] == 0
    assert second[1]["rewritten_plaintext_records"] == 0
    assert second[1]["residual_plaintext_copies"] == []
    assert second[1]["deleted_files"] == []
    assert second[1]["unresolved_plaintext"] == []
    assert second[1]["verified"] is True
    assert {p: p.read_bytes() for p in _tree(workspace / "_secrets")} == snapshots


def test_deletes_a_plaintext_copy_once_the_encrypted_record_is_in_place(
    workspace, capsys
):
    ids = _plaintext_store(workspace)
    record = workspace / "_secrets" / ids["api_key"] / "secret.json"
    copy = record.with_name("secret.json.plaintext-bak")
    copy.write_bytes(record.read_bytes())

    code, report, _ = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert code == 0
    assert report["plaintext_records_before"] == 2
    assert report["rewritten_plaintext_records"] == 2
    assert report["residual_plaintext_copies"] == [
        {"path": str(copy), "name": "api_key", "sha256_prefix": _fp(API_VALUE)}
    ]
    assert report["deleted_files"] == [str(copy)]
    assert copy.exists() is False
    assert report["verified"] is True
    assert (
        SecretRegistry(workspace / "_secrets", master_key=MASTER_KEY)
        .get(ids["api_key"])
        .value
        == API_VALUE
    )


def test_removes_a_stale_plaintext_record_left_at_a_moved_directory(
    workspace, capsys
):
    ids = _plaintext_store(workspace)
    record_dir = workspace / "_secrets" / ids["api_key"]
    stale_dir = workspace / "_secrets" / f"{ids['api_key']}-moved"
    record_dir.rename(stale_dir)
    stale = stale_dir / "secret.json"
    assert json.loads(stale.read_text(encoding="utf-8"))["value"] == API_VALUE

    code, report, _ = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert code == 0
    assert report["deleted_files"] == [str(stale)]
    assert report["residual_plaintext_copies"] == [
        {"path": str(stale), "name": "api_key", "sha256_prefix": _fp(API_VALUE)}
    ]
    assert stale.exists() is False
    assert report["verified"] is True
    payload = json.loads(
        (record_dir / "secret.json").read_text(encoding="utf-8")
    )
    assert payload["encrypted"] is True
    assert payload["value"].startswith("gAAAAA")


def test_an_unreadable_record_file_fails_closed_without_touching_it(
    workspace, capsys
):
    _plaintext_store(workspace)
    broken = workspace / "_secrets" / "sec_broken" / "secret.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("not json\n", encoding="utf-8")

    code, report, _ = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert code == 3
    assert report["unreadable_records"] == [str(broken)]
    assert report["verified"] is False
    assert broken.read_text(encoding="utf-8") == "not json\n"


def test_an_unknown_extra_file_blocks_verification_unless_explicitly_allowed(
    workspace, capsys
):
    _plaintext_store(workspace)
    extra = workspace / "_secrets" / "notes.txt"
    extra.write_text("no credentials in here\n", encoding="utf-8")

    code, report, _ = _run(
        capsys,
        ["--workspace-base", str(workspace), "--master-key", MASTER_KEY],
    )

    assert code == 3
    assert report["unknown_files"] == [str(extra)]
    assert report["verified"] is False
    assert extra.read_text(encoding="utf-8") == "no credentials in here\n"

    allowed = _run(
        capsys,
        [
            "--workspace-base",
            str(workspace),
            "--master-key",
            MASTER_KEY,
            "--allow-unknown-files",
        ],
    )

    assert allowed[0] == 0
    assert allowed[1]["unknown_files"] == [str(extra)]
    assert allowed[1]["verified"] is True


def test_master_key_may_come_from_the_environment(workspace, capsys, monkeypatch):
    monkeypatch.setenv("E2B_SECRET_MASTER_KEY", MASTER_KEY)
    _plaintext_store(workspace)

    code, report, _ = _run(capsys, ["--workspace-base", str(workspace)])

    assert code == 0
    assert report["rewritten_plaintext_records"] == 2
    assert report["verified"] is True


def test_a_rotation_window_key_re_encrypts_onto_the_primary_key(
    workspace, capsys
):
    registry = SecretRegistry(workspace / "_secrets", master_key="old-key")
    ids = {
        name: registry.create(name, value).secret_id
        for name, value in (("api_key", API_VALUE), ("token", TOKEN_VALUE))
    }

    code, report, _ = _run(
        capsys,
        [
            "--workspace-base",
            str(workspace),
            "--master-key",
            "new-key",
            "--legacy-master-key",
            "old-key",
        ],
    )

    assert code == 0
    assert report["plaintext_records_before"] == 0
    assert report["verified"] is True
    assert (
        SecretRegistry(workspace / "_secrets", master_key="new-key")
        .get(ids["api_key"])
        .value
        == API_VALUE
    )
    with pytest.raises(UnknownSecretError):
        SecretRegistry(workspace / "_secrets", master_key="old-key").get(
            ids["api_key"]
        )


def test_a_missing_secrets_directory_is_not_reported_as_clean(workspace, capsys):
    code = cleanup.main(
        [
            "--workspace-base",
            str(workspace),
            "--master-key",
            MASTER_KEY,
        ]
    )
    captured = capsys.readouterr()

    assert code == 4
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"目标目录不存在：{(workspace / '_secrets').resolve()}",
        "路径可能指错了（control-plane 的 E2B_WORKSPACE_BASE）：拒绝把"
        "“空目录”当成校验通过。",
    ]
