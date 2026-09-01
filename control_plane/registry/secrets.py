"""Secret registry: write-only values with versioning (E5.4).

Values are encrypted at rest with AES-128/256 (Fernet) whenever
``E2B_SECRET_MASTER_KEY`` is configured: the disk records and the optional
Redis mirror store ciphertext, and decryption happens only in memory when a
value is resolved. Without a master key the registry degrades to the
previous in-memory + plaintext-disk behavior and logs a startup warning
(``E2B_SECRET_MASTER_KEY`` unset); nothing is written to Redis in that mode.

Key rotation uses the same two-window model as the internal key: put the
current key in ``E2B_SECRET_MASTER_KEYS``, set ``E2B_SECRET_MASTER_KEY`` to
the new key, let every replica roll, then remove the old key. Records
decrypted with a legacy key are transparently re-encrypted with the primary
key on the next load.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway_common.ids import sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

#: Max UTF-8 bytes of a secret value (E5.4 / E5.3: keeps sandbox.json and
#: the registry bounded even when an env var expands a secret reference).
MAX_SECRET_VALUE_BYTES = 64 * 1024

_REDIS_NAMESPACE = "e2b:secret"
_KDF_SALT = b"e2b-secret-master-v1"
_KDF_INFO = b"secret-registry"


def _check_value_size(value: str) -> None:
    if len(value.encode("utf-8")) > MAX_SECRET_VALUE_BYTES:
        raise ValueError(
            f"secret value exceeds {MAX_SECRET_VALUE_BYTES}-byte limit"
        )


try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:  # pragma: no cover - cryptography is pinned in requirements
    Fernet = None  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]
    HKDF = None  # type: ignore[assignment]


def _fernet(master_key: str):
    """A Fernet cipher derived from the configured master key (HKDF-SHA256)."""
    if Fernet is None:
        raise RuntimeError(
            "cryptography is required for secret encryption "
            "(pip install -r requirements.txt)"
        )
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        info=_KDF_INFO,
    )
    return Fernet(base64.urlsafe_b64encode(hkdf.derive(master_key.encode("utf-8"))))


class UnknownSecretError(KeyError):
    pass


class SecretTenantMismatchError(ValueError):
    """Raised when a sandbox references a secret owned by another tenant."""


class SecretDecryptionError(ValueError):
    """Raised when a stored secret cannot be decrypted with any known key."""


@dataclass
class SecretRecord:
    secret_id: str
    name: str
    value: str
    version: int = 1
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    tenant_id: str | None = None

    def as_model(self) -> dict:
        return {
            "secretID": self.secret_id,
            "name": self.name,
            "currentVersion": self.version,
            "metadata": self.metadata,
            "createdAt": to_iso_z(self.created_at),
            "updatedAt": to_iso_z(self.updated_at),
        }

    def to_storage_dict(
        self, encrypt: Any | None = None
    ) -> dict[str, Any]:
        """Storage payload; ``encrypt`` (callable) encrypts the value."""
        value = self.value
        encrypted = False
        if encrypt is not None:
            value = encrypt(self.value)
            encrypted = True
        return {
            "secret_id": self.secret_id,
            "name": self.name,
            "value": value,
            "encrypted": encrypted,
            "version": self.version,
            "metadata": dict(self.metadata),
            "created_at": to_iso_z(self.created_at),
            "updated_at": to_iso_z(self.updated_at),
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_storage_dict(
        cls, data: dict[str, Any], decrypt: Any | None = None
    ) -> "SecretRecord":
        from datetime import datetime as _dt

        def _parse(value: str) -> datetime:
            try:
                return _dt.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return utcnow()

        value = data["value"]
        if data.get("encrypted"):
            if decrypt is None:
                raise SecretDecryptionError(
                    "stored secret is encrypted but no master key is configured"
                )
            value = decrypt(value)
        return cls(
            secret_id=data["secret_id"],
            name=data["name"],
            value=value,
            version=int(data.get("version", 1)),
            metadata=dict(data.get("metadata", {})),
            created_at=_parse(data.get("created_at")),
            updated_at=_parse(data.get("updated_at")),
            tenant_id=data.get("tenant_id"),
        )


class SecretRegistry:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        redis_client: Any | None = None,
        master_key: str | None = None,
        legacy_master_keys: tuple[str, ...] = (),
    ) -> None:
        self._secrets: dict[str, SecretRecord] = {}
        self._by_name: dict[str, str] = {}
        self._lock = threading.Lock()
        self._base = Path(base_dir).resolve() if base_dir is not None else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)
        self._redis = redis_client
        self._master_key = master_key
        self._legacy_keys = tuple(
            k for k in legacy_master_keys if k and k != master_key
        )
        self._fernet = _fernet(master_key) if master_key else None
        self._legacy_fernets = [_fernet(k) for k in self._legacy_keys]
        if master_key is None:
            logger.warning(
                "E2B_SECRET_MASTER_KEY is not configured: secrets are stored "
                "without at-rest encryption and are not persisted to Redis "
                "(degraded mode; configure the key in production)"
            )
        if self._redis is not None and self._fernet is None:
            logger.warning(
                "E2B_REDIS_URL is set but E2B_SECRET_MASTER_KEY is not: "
                "secrets will not be mirrored to Redis"
            )
        self._scan_redis()
        self._scan_disk()

    def create(
        self,
        name: str,
        value: str,
        metadata: dict[str, str] | None = None,
        tenant_id: str | None = None,
    ) -> SecretRecord:
        if not name or not isinstance(name, str):
            raise ValueError("name must be a non-empty string")
        if value is None:
            raise ValueError("value is required")
        _check_value_size(str(value))
        with self._lock:
            if name in self._by_name:
                raise ValueError(f"Secret {name} already exists")
            secret_id = sandbox_id().replace("sbx_", "sec_")
            record = SecretRecord(
                secret_id=secret_id,
                name=name,
                value=str(value),
                metadata=dict(metadata or {}),
                tenant_id=tenant_id,
            )
            self._secrets[secret_id] = record
            self._by_name[name] = secret_id
            self._persist_record(record)
            return record

    def _record_path(self, secret_id: str) -> Path | None:
        if self._base is None:
            return None
        return self._base / secret_id / "secret.json"

    def _redis_key(self, secret_id: str) -> str:
        return f"{_REDIS_NAMESPACE}:{secret_id}"

    def _encrypt_value(self, value: str) -> str:
        assert self._fernet is not None
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt_value(self, token: str) -> tuple[str, bool]:
        """Return ``(plaintext, used_legacy_key)`` for an encrypted token."""
        candidates = [(self._fernet, False)]
        candidates += [(f, True) for f in self._legacy_fernets]
        for fernet, legacy in candidates:
            if fernet is None:
                continue
            try:
                plain = fernet.decrypt(token.encode("ascii"))
                return plain.decode("utf-8"), legacy
            except Exception:
                continue
        raise SecretDecryptionError(
            "stored secret cannot be decrypted (wrong or missing "
            "E2B_SECRET_MASTER_KEY?)"
        )

    def _persist_record(self, record: SecretRecord) -> None:
        """Write one record to disk (and Redis when encryption is enabled)."""
        encrypt = self._encrypt_value if self._fernet is not None else None
        payload = record.to_storage_dict(encrypt=encrypt)
        path = self._record_path(record.secret_id)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
        if self._redis is not None and self._fernet is not None:
            self._redis.set(
                self._redis_key(record.secret_id),
                json.dumps(payload, separators=(",", ":")),
            )

    def _write_record(self, record: SecretRecord) -> None:
        """Deprecated alias kept for callers outside the lock; prefer
        ``_persist_record`` (which is lock-agnostic)."""
        self._persist_record(record)

    def _record_from_payload(
        self, payload: dict[str, Any]
    ) -> SecretRecord | None:
        """Load a storage payload into a record, decrypting as needed."""
        try:
            if payload.get("encrypted"):
                if self._fernet is None:
                    raise SecretDecryptionError(
                        "stored secret is encrypted but no master key "
                        "is configured"
                    )
                plain, used_legacy = self._decrypt_value(payload["value"])
                record = SecretRecord.from_storage_dict(
                    {**payload, "value": plain, "encrypted": False}
                )
                if used_legacy:
                    # Re-encrypt with the primary key so old keys can be
                    # removed once every replica has loaded the record.
                    self._persist_record(record)
            else:
                record = SecretRecord.from_storage_dict(payload)
                if self._fernet is not None:
                    # Upgrade a legacy plaintext record to encrypted storage.
                    self._persist_record(record)
            return record
        except SecretDecryptionError as e:
            logger.warning("secret record skipped: %s", e)
            return None
        except (KeyError, ValueError, TypeError, AttributeError):
            return None

    def _scan_disk(self) -> None:
        if self._base is None or not self._base.is_dir():
            return
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / "secret.json"
            if not path.is_file():
                continue
            secret_id = entry.name
            if secret_id in self._secrets:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = self._record_from_payload(payload)
            if record is None:
                continue
            self._secrets[record.secret_id] = record
            self._by_name[record.name] = record.secret_id

    def _scan_redis(self) -> None:
        if self._redis is None:
            return
        try:
            raw_keys = self._redis.keys(f"{_REDIS_NAMESPACE}:*")
        except Exception:  # pragma: no cover - redis unavailable at startup
            logger.warning("secret registry could not read Redis", exc_info=True)
            return
        for raw_key in raw_keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            secret_id = key.split(":", 2)[-1]
            if secret_id in self._secrets:
                continue
            try:
                raw = self._redis.get(key)
            except Exception:  # pragma: no cover
                continue
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            record = self._record_from_payload(payload)
            if record is None:
                continue
            self._secrets[record.secret_id] = record
            self._by_name[record.name] = record.secret_id

    def get(self, secret_id: str) -> SecretRecord:
        with self._lock:
            record = self._secrets.get(secret_id)
            if record is None:
                self._scan_disk()
                self._scan_redis()
                record = self._secrets.get(secret_id)
        if record is None:
            raise UnknownSecretError(secret_id)
        return record

    def get_by_name(self, name: str) -> SecretRecord:
        with self._lock:
            self._scan_disk()
            self._scan_redis()
            secret_id = self._by_name.get(name)
        if secret_id is None:
            raise UnknownSecretError(name)
        return self.get(secret_id)

    def update(
        self, secret_id: str, value: str | None, metadata: dict[str, str] | None = None
    ) -> SecretRecord:
        record = self.get(secret_id)
        if value is not None:
            _check_value_size(str(value))
        with self._lock:
            if value is not None:
                record.value = str(value)
                record.version += 1
            if metadata is not None:
                record.metadata = dict(metadata)
            record.updated_at = utcnow()
            self._persist_record(record)
        return record

    def delete(self, secret_id: str) -> SecretRecord:
        record = self.get(secret_id)
        with self._lock:
            self._secrets.pop(secret_id, None)
            self._by_name.pop(record.name, None)
            if self._redis is not None:
                self._redis.delete(self._redis_key(secret_id))
        path = self._record_path(secret_id)
        if path is not None:
            import shutil

            shutil.rmtree(path.parent, ignore_errors=True)
        return record

    def rotate_master_key(self, new_key: str) -> int:
        """Re-encrypt every stored secret with ``new_key`` (key rotation).

        Returns the number of records re-encrypted. In-memory plaintext is
        unchanged; only the at-rest payloads are rewritten.
        """
        if not new_key or not isinstance(new_key, str):
            raise ValueError("new master key must be a non-empty string")
        new_fernet = _fernet(new_key)
        with self._lock:
            self._scan_disk()
            self._scan_redis()
            self._master_key = new_key
            self._fernet = new_fernet
            self._legacy_keys = ()
            self._legacy_fernets = []
            count = 0
            for record in list(self._secrets.values()):
                self._persist_record(record)
                count += 1
            return count

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        tenant_id: str | None = None,
    ) -> list[SecretRecord]:
        with self._lock:
            self._scan_disk()
            values = list(self._secrets.values())
        records = sorted(values, key=lambda r: r.created_at, reverse=True)
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        if limit is not None:
            records = records[offset : offset + limit]
        return records

    def resolve_env_refs(
        self,
        env_vars: dict[str, str],
        tenant_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, str]:
        """Resolve ``${secret_name}`` references in env var values.

        When tenant isolation is active (``tenant_id`` set and caller not an
        admin), referencing a secret owned by another tenant raises
        :class:`SecretTenantMismatchError` (the API maps it to 403).
        """
        resolved: dict[str, str] = {}
        for key, value in env_vars.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                name = value[2:-1]
                try:
                    secret = self.get_by_name(name)
                    if (
                        tenant_id is not None
                        and not is_admin
                        and secret.tenant_id != tenant_id
                    ):
                        raise SecretTenantMismatchError(
                            f"Secret {name} does not belong to this tenant"
                        )
                    resolved[key] = secret.value
                    continue
                except UnknownSecretError:
                    pass
            resolved[key] = value
        return resolved
