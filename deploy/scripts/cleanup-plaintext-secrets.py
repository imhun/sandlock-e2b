#!/usr/bin/env python3
"""O3 (second round): rewrite pre-existing plaintext secret records in place.

`E2B_SECRET_MASTER_KEY` (O3 Task 1, `dd96266`) only changes **future** writes.
Records that were already written while the control plane ran in degraded mode
sit on the shared NAS volume as plaintext
(``<workspace_base>/_secrets/<secret_id>/secret.json`` with
``"encrypted": false`` and the value in the clear), and a worker pod mounts
that whole volume read-write -- so anyone with pod root could read every
tenant's secret. This tool is the one-shot migration the decision of
2026-09-26 asked for (``docs/superpowers/plans/2026-09-26-decisions.md``,
《追加裁定（2026-09-26，O3 第二轮）》).

How the plaintext/encrypted shapes are told apart, and how they are rewritten
-----------------------------------------------------------------------------
It does **not** re-implement any crypto. It hands the directory to
``SecretRegistry`` (the same class ``control_plane/app.py`` builds) with the
master key, and that constructor's ``_scan_disk()`` -> ``_record_from_payload()``
does the rewrite: a payload whose ``encrypted`` flag is not true is loaded as a
plaintext record and, because ``self._fernet is not None``, immediately
re-persisted through ``_persist_record()`` -- which is ``to_storage_dict(
encrypt=self._encrypt_value)``, i.e. the same
``encrypted: true`` + Fernet-token-on-disk shape the API writes today.
Encrypted payloads are decrypted (primary key, then any
``E2B_SECRET_MASTER_KEYS`` window keys) and only re-encrypted when a legacy key
was used. So:

* plaintext record  := canonical file whose JSON has ``encrypted != true``;
* encrypted record  := canonical file whose JSON has ``encrypted == true`` and
  whose token decrypts with the configured keys (proved by re-reading the
  whole directory with a second, fresh registry at the end).

What it also cleans up, and refuses to guess about
--------------------------------------------------
A rewrite can leave plaintext **copies** behind: the same record under a
directory whose name no longer matches the record's ``secret_id`` (the rewrite
goes to the canonical path), a ``*.bak`` of the old plaintext payload, or a
loose file that embeds the value. Every file under ``_secrets/**`` is inspected:

* a copy that is provably preserved -- the value is byte-for-byte the value of a
  record whose canonical file is now encrypted -- is deleted, and its path and
  the secret's ``sha256(前16)`` fingerprint are reported (never the value);
* anything else that still holds plaintext is reported and the run **fails
  closed** (exit 3) without deleting it: a disagreement between two copies of
  the same ``secret_id``, an unparseable ``secret.json``, a file that is not
  part of the registry's shape (add ``--allow-unknown-files`` only after
  looking at it).

No master key means no run
--------------------------
Without ``E2B_SECRET_MASTER_KEY`` (``--master-key``) the "rewrite" would write
plaintext again, i.e. it would look like a cleanup while changing nothing, so
the tool refuses with exit 2 before touching a single file.

Fingerprints only
-----------------
Like ``deploy/k8s-k0s/secrets.sh``, nothing here prints a credential value:
only ``sha256(前16)`` of the value (length-guarded so a *short* value is never
substring-matched against base64 ciphertext, which would be a false positive).

Usage (in the control-plane pod, which is where the master key and the volume
are; ``python3 -`` is how ``deploy/k8s-k0s/apply.sh`` ships in-container
helpers, and stdin keeps the script out of the image):

    kubectl -n sandlock exec -i deploy/control-plane -- \\
        python3 - < deploy/scripts/cleanup-plaintext-secrets.py

Exit codes: 0 = verified clean (idempotent re-runs land here), 2 = refused
(no master key), 3 = not verified / needs a human, 4 = target directory or
arguments wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _add_repo_root_to_path() -> None:
    """Make ``control_plane`` importable however this file is started.

    ``python3 -`` (the in-pod invocation) leaves ``__file__`` undefined and
    puts ``""`` -- the cwd, ``/app`` in the image -- on ``sys.path``;
    ``python3 deploy/scripts/cleanup-plaintext-secrets.py`` puts the script's
    own directory there instead, which is not enough.
    """
    try:
        here = Path(__file__).resolve()
    except NameError:  # pragma: no cover - `python3 -`
        here = None
    candidates = []
    if here is not None:
        candidates.append(here.parent.parent.parent)
    candidates.append(Path.cwd())
    for candidate in candidates:
        if (candidate / "control_plane" / "registry" / "secrets.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


_add_repo_root_to_path()

from gateway_common.env import env_list  # noqa: E402
from control_plane.registry.secrets import SecretRegistry  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NOT_VERIFIED = 3
EXIT_NO_TARGET = 4

#: Values shorter than this are never substring-swept: a two-character secret
#: ("v1") shows up inside base64 ciphertext by chance, and a false "leak"
#: would be worse than the miss. Exact-shape matches (whole file, JSON
#: ``value`` field) are checked for every length.
MIN_SWEEP_BYTES = 12


def fingerprint(value: str) -> str:
    """``sha256(前16)``, the shape ``deploy/k8s-k0s/secrets.sh`` prints."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _is_canonical(path: Path, secrets_dir: Path) -> bool:
    """``<secrets_dir>/<secret_id>/secret.json`` -- the only shape the
    registry reads (``SecretRegistry._record_path``)."""
    return path.name == "secret.json" and path.parent.parent == secrets_dir


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _contains_value(text: str, value: str) -> bool:
    """Exact-shape match of a plaintext value inside a stray file.

    Only two forms are trusted for short values (a raw copy, or a plaintext
    storage payload's ``value`` field); the looser substring sweep needs the
    value to be long enough that base64 ciphertext cannot contain it by
    accident.
    """
    if text.strip() == value:
        return True
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("value") == value:
            return True
    if len(value.encode("utf-8")) >= MIN_SWEEP_BYTES and value in text:
        return True
    return False


def _delete(path: Path, secrets_dir: Path) -> None:
    """Unlink one verified duplicate and prune the directory it left empty."""
    path.unlink()
    parent = path.parent
    while parent != secrets_dir and parent.parent != parent:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _scan(
    secrets_dir: Path, records: list[Any]
) -> dict[str, Any]:
    """Classify every file under ``secrets_dir`` (read-only)."""
    by_id = {record.secret_id: record for record in records}
    by_value = sorted({(record.name, record.value) for record in records})
    encrypted_paths: dict[str, str] = {}
    copies: list[dict[str, Any]] = []
    unresolved: list[str] = []
    unreadable: list[str] = []
    unknown: list[str] = []

    for path in _files(secrets_dir):
        canonical = _is_canonical(path, secrets_dir)
        payload = _read_payload(path)
        if canonical and payload is None:
            unreadable.append(str(path))
            continue
        if canonical and payload.get("encrypted") is True:
            secret_id = payload.get("secret_id")
            if isinstance(secret_id, str) and secret_id in by_id:
                encrypted_paths[secret_id] = str(path)
            else:
                unreadable.append(str(path))
            continue
        # Not a canonical encrypted record: either a leftover plaintext record
        # or some other file. Attribute it to a known secret, or report it.
        matched: tuple[str, str] | None = None
        if canonical and isinstance(payload.get("value"), str):
            value = payload["value"]
            secret_id = payload.get("secret_id")
            record = by_id.get(secret_id) if isinstance(secret_id, str) else None
            if record is not None and record.value == value:
                matched = (record.name, value)
        if matched is None:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            for name, value in by_value:
                if _contains_value(text, value):
                    matched = (name, value)
                    break
        if matched is None:
            if canonical:
                unresolved.append(str(path))
            else:
                unknown.append(str(path))
            continue
        name, value = matched
        copies.append(
            {
                "path": str(path),
                "name": name,
                "value": value,
                "sha256_prefix": fingerprint(value),
            }
        )

    # A copy is only deletable when the value it holds provably survives in an
    # encrypted canonical record -- otherwise removing it could lose a secret.
    preserved = {
        (by_id[secret_id].name, by_id[secret_id].value)
        for secret_id in encrypted_paths
    }
    for copy in copies:
        copy["preserved"] = (copy["name"], copy["value"]) in preserved
    return {
        "plaintext_copies": copies,
        "unresolved_plaintext": sorted(unresolved),
        "unreadable_records": sorted(unreadable),
        "unknown_files": sorted(unknown),
    }


def run(
    secrets_dir: Path | str,
    *,
    master_key: str,
    legacy_master_keys: tuple[str, ...] = (),
    allow_unknown_files: bool = False,
) -> dict[str, Any]:
    """Rewrite plaintext records, remove verified plaintext copies, verify.

    Returns the report; ``report["verified"]`` is the single verdict.
    """
    secrets_dir = Path(secrets_dir).resolve()

    # 1. What was plaintext before we touch anything (this is the honest count
    # of records the master key had not yet reached).
    plaintext_before = []
    for path in _files(secrets_dir):
        if not _is_canonical(path, secrets_dir):
            continue
        payload = _read_payload(path)
        if payload is not None and payload.get("encrypted") is not True:
            plaintext_before.append(path)

    # 2. The rewrite itself: constructing the registry runs its read path over
    # the directory, which re-persists every plaintext record as ciphertext.
    registry = SecretRegistry(
        secrets_dir,
        master_key=master_key,
        legacy_master_keys=legacy_master_keys,
    )
    records = registry.list()

    rewritten = 0
    for path in plaintext_before:
        payload = _read_payload(path)
        if payload is not None and payload.get("encrypted") is True:
            rewritten += 1

    # 3. Classify what is left; delete only copies whose value provably
    # survives in an encrypted record.
    scan = _scan(secrets_dir, records)
    deleted: list[str] = []
    undeletable: list[str] = []
    for copy in scan["plaintext_copies"]:
        if copy["preserved"]:
            _delete(Path(copy["path"]), secrets_dir)
            deleted.append(copy["path"])
        else:
            undeletable.append(copy["path"])
    deleted.sort()
    undeletable.sort()

    # 4. Verify on the final state: nothing plaintext-shaped left, and every
    # record re-reads through a fresh registry with the same keys.
    final = _scan(secrets_dir, registry.list())
    reread_ok = {
        (r.secret_id, r.name, r.value) for r in records
    } == {
        (r.secret_id, r.name, r.value)
        for r in SecretRegistry(
            secrets_dir,
            master_key=master_key,
            legacy_master_keys=legacy_master_keys,
        ).list()
    }
    leftover = [copy["path"] for copy in final["plaintext_copies"]]
    unresolved = sorted(set(undeletable) | set(final["unresolved_plaintext"]))
    residual_unknown = final["unknown_files"]
    verified = bool(
        not leftover
        and not unresolved
        and not final["unreadable_records"]
        and (allow_unknown_files or not residual_unknown)
        and reread_ok
    )

    return {
        "secrets_dir": str(secrets_dir),
        "plaintext_records_before": len(plaintext_before),
        "rewritten_plaintext_records": rewritten,
        "residual_plaintext_copies": [
            {
                "path": copy["path"],
                "name": copy["name"],
                "sha256_prefix": copy["sha256_prefix"],
            }
            for copy in sorted(
                scan["plaintext_copies"], key=lambda item: item["path"]
            )
        ],
        "deleted_files": deleted,
        "unreadable_records": final["unreadable_records"],
        "unresolved_plaintext": unresolved,
        "unknown_files": residual_unknown,
        "verified": verified,
        "secrets": sorted(
            (
                {
                    "name": record.name,
                    "version": record.version,
                    "sha256_prefix": fingerprint(record.value),
                }
                for record in records
            ),
            key=lambda item: item["name"],
        ),
    }


def _report_human(report: dict[str, Any]) -> None:
    """Operator-facing lines on stderr; the JSON report owns stdout.

    Never a credential value: paths, names, ``sha256(前16)`` and counts only.
    """

    def say(line: str) -> None:
        print(line, file=sys.stderr)

    say(f"清理明文 secret：{report['secrets_dir']}")
    say(
        f"  记录 {len(report['secrets'])} 条；清理前明文 "
        f"{report['plaintext_records_before']} 条，本次经 registry 重写 "
        f"{report['rewritten_plaintext_records']} 条"
    )
    for copy in report["residual_plaintext_copies"]:
        mark = "已删除" if copy["path"] in report["deleted_files"] else "保留"
        say(
            f"  残留明文副本（{mark}）：{copy['path']} ← secret={copy['name']} "
            f"sha256:{copy['sha256_prefix']}"
        )
    for path in report["unreadable_records"]:
        say(f"  不可读的记录（原样保留）：{path}")
    for path in report["unresolved_plaintext"]:
        say(f"  无法确认安全的明文文件（原样保留）：{path}")
    for path in report["unknown_files"]:
        say(f"  非 registry 形状的文件（原样保留）：{path}")
    if report["verified"]:
        say("校验通过：目录内不再有明文，且全部记录都能用主 key 回读。")
    else:
        say(
            "校验未通过：上面列出的文件需要人工确认（修复或删除）后重跑；"
            "确认无害可加 --allow-unknown-files。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "把 <workspace_base>/_secrets/** 上已落盘的明文 secret 经 "
            "SecretRegistry 重写为加密态，清理残留明文副本并校验。"
        ),
    )
    parser.add_argument(
        "--workspace-base",
        help="workspace base；目标目录是它下面的 _secrets（默认取 E2B_WORKSPACE_BASE）",
    )
    parser.add_argument(
        "--secrets-dir",
        help="直接指定 _secrets 目录（优先于 --workspace-base）",
    )
    parser.add_argument(
        "--master-key",
        help="secret 主 key（默认取 E2B_SECRET_MASTER_KEY）",
    )
    parser.add_argument(
        "--legacy-master-key",
        action="append",
        default=[],
        help="轮换窗口里的旧主 key（可重复；默认并上 E2B_SECRET_MASTER_KEYS）",
    )
    parser.add_argument(
        "--allow-unknown-files",
        action="store_true",
        help="人工确认过 _secrets 下的非 registry 形状文件之后，允许它们不阻断校验",
    )
    args = parser.parse_args(argv)

    master_key = (args.master_key or os.getenv("E2B_SECRET_MASTER_KEY") or "").strip()
    if not master_key:
        print(
            "拒绝执行：未配置 E2B_SECRET_MASTER_KEY（--master-key 或环境变量）。",
            file=sys.stderr,
        )
        print(
            "没有主 key 时“重写”仍会落明文：先跑 deploy/k8s-k0s/secrets.sh，"
            "并滚动 control-plane 让主 key 生效。",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    if args.secrets_dir:
        secrets_dir = Path(args.secrets_dir)
    else:
        base = args.workspace_base or os.getenv("E2B_WORKSPACE_BASE") or ""
        if not base:
            print(
                "未指定目标：给 --secrets-dir 或 --workspace-base"
                "（默认取 control-plane 的 E2B_WORKSPACE_BASE）。",
                file=sys.stderr,
            )
            return EXIT_NO_TARGET
        secrets_dir = Path(base) / "_secrets"
    secrets_dir = secrets_dir.resolve()
    if not secrets_dir.is_dir():
        print(f"目标目录不存在：{secrets_dir}", file=sys.stderr)
        print(
            "路径可能指错了（control-plane 的 E2B_WORKSPACE_BASE）："
            "拒绝把“空目录”当成校验通过。",
            file=sys.stderr,
        )
        return EXIT_NO_TARGET

    legacy = tuple(args.legacy_master_key) + tuple(
        env_list("E2B_SECRET_MASTER_KEYS", ())
    )
    report = run(
        secrets_dir,
        master_key=master_key,
        legacy_master_keys=legacy,
        allow_unknown_files=args.allow_unknown_files,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    _report_human(report)
    return EXIT_OK if report["verified"] else EXIT_NOT_VERIFIED


if __name__ == "__main__":
    sys.exit(main())
