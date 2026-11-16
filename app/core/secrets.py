"""Job secrets encrypted at rest (README 10).

The key comes from ``WSS_SECRETS_KEY`` (outside the volume): either 32 bytes in
URL-safe base64 or any passphrase of at least 16 characters (stretched with scrypt and a
fixed, service-specific salt). Blobs written under the earlier
single-SHA-256 stretching of the same passphrase stay readable (their key id differs).
Each stored blob carries a key id (the first 16 hex characters of SHA-256 of the key),
so a changed key is detected without trying to decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.db import Database


class SecretsUnavailable(Exception):
    pass


_SCRYPT_SALT = b"web-scraper-service/job-secrets/v1"


def _raw_key(raw: str) -> bytes | None:
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == 32:
            return decoded
    except (ValueError, base64.binascii.Error):
        pass
    return None


def derive_key(raw: str | None) -> bytes | None:
    if not raw:
        return None
    decoded = _raw_key(raw)
    if decoded is not None:
        return decoded
    if len(raw) < 16:
        return None
    return hashlib.scrypt(raw.encode("utf-8"), salt=_SCRYPT_SALT, n=2**14, r=8, p=1, dklen=32)


def legacy_key(raw: str | None) -> bytes | None:
    """The 0.1.0 stretching (one SHA-256), for reading blobs written before scrypt."""
    if not raw or _raw_key(raw) is not None or len(raw) < 16:
        return None
    return hashlib.sha256(raw.encode("utf-8")).digest()


class SecretStore:
    def __init__(self, db: Database, raw_key: str | None) -> None:
        self.db = db
        self.key = derive_key(raw_key)
        self.key_id = hashlib.sha256(self.key).hexdigest()[:16] if self.key else None
        self._keys: dict[str, bytes] = {}
        if self.key:
            self._keys[self.key_id] = self.key
        old = legacy_key(raw_key)
        if old:
            self._keys[hashlib.sha256(old).hexdigest()[:16]] = old

    @property
    def configured(self) -> bool:
        return self.key is not None

    def put(self, run_id: str, kind: str, value: Any) -> None:
        if not self.key:
            raise SecretsUnavailable("no job-secret key configured")
        nonce = os.urandom(12)
        data = json.dumps(value, separators=(",", ":")).encode("utf-8")
        ct = AESGCM(self.key).encrypt(nonce, data, f"{run_id}:{kind}".encode())
        self.db.execute(
            "INSERT INTO run_secrets(run_id, kind, key_id, nonce, ciphertext) VALUES (?,?,?,?,?) "
            "ON CONFLICT(run_id, kind) DO UPDATE SET key_id=excluded.key_id, nonce=excluded.nonce, ciphertext=excluded.ciphertext",
            (run_id, kind, self.key_id, nonce, ct),
        )

    def present(self, run_id: str, kind: str) -> bool:
        return self.db.one("SELECT 1 FROM run_secrets WHERE run_id=? AND kind=?", (run_id, kind)) is not None

    def readable(self, run_id: str, kind: str) -> bool:
        row = self.db.one("SELECT key_id FROM run_secrets WHERE run_id=? AND kind=?", (run_id, kind))
        return row is not None and row["key_id"] in self._keys

    def get(self, run_id: str, kind: str) -> Any:
        row = self.db.one("SELECT key_id, nonce, ciphertext FROM run_secrets WHERE run_id=? AND kind=?", (run_id, kind))
        if row is None:
            raise SecretsUnavailable(f"no {kind} secret stored")
        key = self._keys.get(row["key_id"])
        if key is None:
            raise SecretsUnavailable("the stored secret was encrypted with another key")
        try:
            data = AESGCM(key).decrypt(row["nonce"], row["ciphertext"], f"{run_id}:{kind}".encode())
        except InvalidTag as exc:
            raise SecretsUnavailable("the stored secret cannot be decrypted") from exc
        return json.loads(data)

    def delete(self, run_id: str, kind: str) -> None:
        self.db.execute("DELETE FROM run_secrets WHERE run_id=? AND kind=?", (run_id, kind))

    def undecryptable_runs(self) -> int:
        if self.key_id is None:
            return int(self.db.scalar("SELECT COUNT(DISTINCT run_id) FROM run_secrets") or 0)
        ids = list(self._keys)
        marks = ",".join("?" * len(ids))
        return int(self.db.scalar(f"SELECT COUNT(DISTINCT run_id) FROM run_secrets WHERE key_id NOT IN ({marks})", tuple(ids)) or 0)
