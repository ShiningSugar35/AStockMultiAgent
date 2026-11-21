"""Filesystem helpers for bounded storage-retention tree cleanup."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    byte_size: int
    file_count: int
    dir_count: int
    latest_mtime_ns: int
    fingerprint: str = ""
    complete: bool = True
    link_count: int = 0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A PID alone is not proof that the original owner is still alive."""

    status: Literal["ALIVE", "DEAD", "UNKNOWN"]
    creation_time: float | None = None


def process_identity(pid: int) -> ProcessIdentity:
    """Read process creation time without dependencies; access failures stay unknown."""

    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid <= 0xFFFFFFFF:
        return ProcessIdentity("UNKNOWN")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4,
        ]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ProcessIdentity("DEAD" if ctypes.get_last_error() == 87 else "UNKNOWN")
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ProcessIdentity("UNKNOWN")
            if exit_code.value != 259:
                return ProcessIdentity("DEAD")
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(system), ctypes.byref(user),
            ):
                return ProcessIdentity("UNKNOWN")
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ProcessIdentity("ALIVE", ticks / 10_000_000 - 11_644_473_600)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessIdentity("DEAD")
    except (PermissionError, OSError, OverflowError):
        return ProcessIdentity("UNKNOWN")
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = int(raw.rsplit(")", 1)[1].split()[19])
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        return ProcessIdentity("ALIVE", boot + start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, StopIteration):
        return ProcessIdentity("UNKNOWN")


def path_has_redirect(root: Path, path: Path) -> bool:
    """Reject redirects at every lexical component before resolving the candidate."""

    try:
        relative = path.absolute().relative_to(root.absolute())
        if ".." in relative.parts:
            return True
        current = root
        for part in ("", *relative.parts):
            current = current / part
            if current.is_symlink() or current.is_junction():
                return True
        return False
    except (OSError, ValueError):
        return True


def snapshot_tree(root: Path, *, max_entries: int = 100_000) -> TreeSnapshot:
    """Measure an owned tree without following redirects; incomplete scans cannot delete."""

    if root.is_symlink() or root.is_junction():
        return TreeSnapshot(0, 0, 0, 0, complete=False, link_count=1)
    if not root.exists():
        return TreeSnapshot(0, 0, 0, 0)
    byte_size = file_count = dir_count = latest_mtime_ns = visited = links = 0
    complete = True
    digest = hashlib.sha256()
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            current_stat = current.lstat()
            latest_mtime_ns = max(latest_mtime_ns, current_stat.st_mtime_ns)
        except OSError:
            complete = False
            continue
        if current.is_symlink() or current.is_junction():
            complete = False
            continue
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
                for entry in children:
                    visited += 1
                    if visited > max_entries:
                        complete = False
                        stack.clear()
                        break
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        complete = False
                        continue
                    latest_mtime_ns = max(latest_mtime_ns, entry_stat.st_mtime_ns)
                    digest.update(json.dumps((
                        Path(entry.path).relative_to(root).as_posix(),
                        entry_stat.st_size, entry_stat.st_mtime_ns,
                        entry_stat.st_mode, entry_stat.st_ino,
                    ), ensure_ascii=True, separators=(",", ":")).encode("ascii"))
                    if stat.S_ISLNK(entry_stat.st_mode) or (
                        getattr(entry_stat, "st_file_attributes", 0) & 0x400
                    ):
                        links += 1
                    elif entry.is_dir(follow_symlinks=False):
                        dir_count += 1
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        file_count += 1
                        byte_size += entry_stat.st_size
        except OSError:
            complete = False
    return TreeSnapshot(
        byte_size, file_count, dir_count, latest_mtime_ns,
        digest.hexdigest(), complete, links,
    )


def iter_matching_tree_roots(root: Path, patterns: list[str], *, max_depth: int) -> list[Path]:
    """Return non-overlapping matching directory roots, stopping descent at a match."""

    if not root.exists() or root.is_symlink() or root.is_junction():
        return []
    normalized = [pattern.casefold() for pattern in patterns]
    matches: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            children = [
                item
                for item in current.iterdir()
                if item.is_dir() and not item.is_symlink() and not item.is_junction()
            ]
        except OSError:
            continue
        for child in children:
            name = child.name.casefold()
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in normalized):
                matches.append(child)
                continue
            stack.append((child, depth + 1))
    return sorted(matches, key=lambda item: item.as_posix().casefold())


def active_linked_worktree_roots(project_root: Path) -> set[Path]:
    """Read Git's registry completely; an unreadable registry is never an empty one."""

    metadata_root = project_root / ".git" / "worktrees"
    if path_has_redirect(project_root, metadata_root):
        raise ValueError("Git worktree inventory is incomplete")
    try:
        mode = metadata_root.stat().st_mode
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise ValueError("Git worktree inventory is incomplete") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError("Git worktree inventory is incomplete")
    roots: set[Path] = set()
    try:
        metadata_dirs = list(metadata_root.iterdir())
        for metadata in metadata_dirs:
            gitdir_file = metadata / "gitdir"
            if path_has_redirect(project_root, gitdir_file):
                raise ValueError("Git worktree inventory is incomplete")
            raw = gitdir_file.read_text(encoding="utf-8").strip()
            if not raw:
                raise ValueError("Git worktree inventory is incomplete")
            gitdir = Path(raw)
            if not gitdir.is_absolute():
                gitdir = metadata / gitdir
            roots.add(gitdir.resolve().parent)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Git worktree inventory is incomplete") from exc
    return roots


def remove_tree(path: Path) -> None:
    """Delete one owned tree; callers are responsible for all safety checks."""

    shutil.rmtree(path)


__all__ = [
    "ProcessIdentity",
    "TreeSnapshot",
    "active_linked_worktree_roots",
    "iter_matching_tree_roots",
    "path_has_redirect",
    "process_identity",
    "remove_tree",
    "snapshot_tree",
]
