"""Atomic, UTF-8-safe local file writes for Windows and POSIX."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from astock.core.errors import FailureClass, StorageError


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes through a same-directory temporary file and atomic replace."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageError(
            f"Atomic write failed for {path}",
            failure_class=FailureClass.STORAGE,
            details={"path": str(path), "error": str(exc)},
        ) from exc


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_create_bytes(path: Path, data: bytes) -> bool:
    """Atomically publish immutable bytes without replacing an existing object."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except PermissionError:
            if path.exists():
                return False
            raise
    except OSError as exc:
        raise StorageError(
            f"Atomic immutable create failed for {path}",
            failure_class=FailureClass.STORAGE,
            details={"path": str(path), "error": str(exc)},
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
