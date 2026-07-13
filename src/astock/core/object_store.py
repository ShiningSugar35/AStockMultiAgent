"""Immutable SHA-256 content-addressed object storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from astock.core.atomic import atomic_create_bytes
from astock.core.errors import FailureClass, StorageError
from astock.core.hashing import canonical_json_bytes, sha256_bytes


@dataclass(frozen=True, slots=True)
class ObjectRef:
    sha256: str
    byte_size: int
    path: Path


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def put_bytes(self, data: bytes) -> ObjectRef:
        digest = sha256_bytes(data)
        path = self.path_for(digest)
        if not path.exists():
            atomic_create_bytes(path, data)
        stored = path.read_bytes()
        if sha256_bytes(stored) != digest:
            raise StorageError(
                f"Object hash mismatch at {path}",
                failure_class=FailureClass.STORAGE,
                details={"expected": digest, "path": str(path)},
            )
        return ObjectRef(sha256=digest, byte_size=len(stored), path=path)

    def put_json(self, value: object) -> ObjectRef:
        return self.put_bytes(canonical_json_bytes(value))

    def get_bytes(self, sha256: str) -> bytes:
        path = self.path_for(sha256)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StorageError(
                f"Object not found: {sha256}",
                failure_class=FailureClass.STORAGE,
                details={"path": str(path)},
            ) from exc
        if sha256_bytes(data) != sha256:
            raise StorageError(
                f"Object verification failed: {sha256}",
                failure_class=FailureClass.STORAGE,
                details={"path": str(path)},
            )
        return data

    def verify(self, sha256: str) -> bool:
        try:
            self.get_bytes(sha256)
        except StorageError:
            return False
        return True
