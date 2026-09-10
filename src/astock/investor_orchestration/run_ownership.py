"""Local process ownership without keeping a database write transaction open.

The OS releases ownership if a worker exits. Lock files must retain their inode;
removing an apparently stale file could split ownership between live processes.
The files contain no account facts and are not a second task/result database.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ScheduledRunInProgress(RuntimeError):
    """Retry the same schedule key after the existing local worker finishes."""


def _lock(fd: int, *, acquire: bool) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        # Windows supports byte-range locks beyond end-of-file, avoiding a
        # truncate/write race when two workers first create the same lock file.
        msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def schedule_run_ownership(database: Path, binding_id: str, schedule_bucket: str) -> Iterator[None]:
    """Acquire a non-blocking, same-host lock before any research or replay.

    Scope is the canonical database plus binding/bucket, not the caller's run ID.
    Completed receipts remain the sole durable result and are read inside this
    lock. An interrupted run may be retried; economic adapters still must honour
    their canonical idempotency keys for crash recovery after external effects.
    """
    if not binding_id.strip() or not schedule_bucket.strip():
        raise ValueError("schedule ownership requires binding and bucket identities")
    path = database.resolve()
    identity = "\0".join((os.path.normcase(str(path)), binding_id, schedule_bucket))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = path.parent / ".scheduled-execution-locks"
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / f"{key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            _lock(fd, acquire=True)
            acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ScheduledRunInProgress("scheduled bucket is already in progress") from exc
            raise
        yield
    finally:
        try:
            if acquired:
                _lock(fd, acquire=False)
        finally:
            os.close(fd)
