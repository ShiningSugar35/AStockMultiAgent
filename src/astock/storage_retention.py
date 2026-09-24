"""Filesystem helpers for bounded storage-retention tree cleanup."""

from __future__ import annotations

import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    byte_size: int
    file_count: int
    dir_count: int
    latest_mtime_ns: int


def snapshot_tree(root: Path) -> TreeSnapshot:
    """Measure one owned directory tree without following symlinks."""

    if not root.exists():
        return TreeSnapshot(0, 0, 0, 0)
    byte_size = 0
    file_count = 0
    dir_count = 0
    latest_mtime_ns = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            current_stat = current.stat()
            latest_mtime_ns = max(latest_mtime_ns, current_stat.st_mtime_ns)
        except OSError:
            pass
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
                    if entry.is_dir(follow_symlinks=False):
                        dir_count += 1
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        file_count += 1
                        byte_size += stat.st_size
        except OSError:
            continue
    return TreeSnapshot(byte_size, file_count, dir_count, latest_mtime_ns)


def iter_matching_tree_roots(root: Path, patterns: list[str], *, max_depth: int) -> list[Path]:
    """Return non-overlapping matching directory roots, stopping descent at a match."""

    if not root.exists():
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
                item for item in current.iterdir() if item.is_dir() and not item.is_symlink()
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
    """Read Git's own worktree metadata without shelling out to Git."""

    metadata_root = project_root / ".git" / "worktrees"
    if not metadata_root.is_dir():
        return set()
    roots: set[Path] = set()
    try:
        metadata_dirs = list(metadata_root.iterdir())
    except OSError:
        return roots
    for metadata in metadata_dirs:
        gitdir_file = metadata / "gitdir"
        try:
            raw = gitdir_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw:
            continue
        gitdir = Path(raw)
        if not gitdir.is_absolute():
            gitdir = (metadata / gitdir).resolve()
        try:
            roots.add(gitdir.resolve().parent)
        except OSError:
            continue
    return roots


def remove_tree(path: Path) -> None:
    """Delete one owned tree; callers are responsible for all safety checks."""

    shutil.rmtree(path)


__all__ = [
    "TreeSnapshot",
    "active_linked_worktree_roots",
    "iter_matching_tree_roots",
    "remove_tree",
    "snapshot_tree",
]
