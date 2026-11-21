"""Staged/CI hygiene reads the selected Git blobs and ratchets Ruff C901."""

from __future__ import annotations

import subprocess

import pytest

from scripts import check_repository


@pytest.mark.parametrize(
    "path,reason",
    [
        (".env", "CREDENTIAL_FILE"),
        ("src/file.py.bak", "TEMPORARY_OR_BACKUP_FILE"),
        ("runtime/state.sqlite", "RUNTIME_OR_PRIVATE_PATH"),
        ("../outside.txt", "INVALID_PATH"),
    ],
)
def test_private_temporary_and_invalid_paths_fail(path: str, reason: str) -> None:
    assert reason in check_repository.inspect_blob(path, b"example")


def test_legitimate_migration_fixture_archive_and_env_example_are_allowed() -> None:
    for path in (
        "migrations/0199_forward.sql",
        "tests/fixtures/recorded.json",
        "docs/archive/release.md",
        ".env.example",
    ):
        assert check_repository.inspect_blob(path, b"public sample") == []


def test_private_key_and_oversized_source_are_detected() -> None:
    key_header = b"-----BEGIN " + b"PRIVATE KEY-----"
    assert "PRIVATE_KEY_MATERIAL" in check_repository.inspect_blob("data.txt", key_header)
    assert "OVERSIZED_BLOB" in check_repository.inspect_blob("src/huge.py", b"x" * 5242881)
    assert check_repository.inspect_blob("tests/fixtures/large.pdf", b"x" * 5242881) == []


def test_staged_inspection_uses_index_blob_without_worktree_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def git(*args):
        calls.append(args)
        if args[0] == "diff":
            return b"src/public.py\0"
        if args == ("show", ":src/public.py"):
            return b"VALUE = 1\n"
        raise AssertionError(args)

    monkeypatch.setattr(check_repository, "_git", git)
    assert check_repository.check_changed(staged=True, base=None) == []
    assert calls == [
        ("diff", "--name-only", "--diff-filter=ACMR", "-z", "--cached"),
        ("show", ":src/public.py"),
    ]


def test_merge_base_inspection_uses_exact_base_and_head_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def git(*args):
        calls.append(args)
        if args[0] in {"rev-parse", "merge-base"}:
            return b"abc123\n"
        if args[0] == "diff":
            return b"src/new.py\0"
        if args == ("show", "HEAD:src/new.py"):
            return b"VALUE = 1\n"
        if args == ("show", "abc123:src/new.py"):
            raise subprocess.CalledProcessError(128, "git")
        raise AssertionError(args)

    monkeypatch.setattr(check_repository, "_git", git)
    assert check_repository.check_changed(staged=False, base="origin/main", complexity=True) == []
    assert ("diff", "--name-only", "--diff-filter=ACMR", "-z", "abc123", "HEAD") in calls


def _branching(decisions: int) -> bytes:
    return (
        "def legacy(value):\n"
        + "".join(f"    if value == {index}: return {index}\n" for index in range(decisions))
        + "    return -1\n"
    ).encode()


def test_ruff_complexity_baseline_accepts_legacy_but_rejects_growth_and_new_over_limit() -> None:
    existing = _branching(22)
    assert check_repository.complexity_regressions("src/example.py", existing, existing) == []
    assert check_repository.complexity_regressions(
        "src/example.py",
        existing,
        _branching(23),
    )
    assert check_repository.complexity_regressions("src/example.py", b"", existing)
    assert check_repository.complexity_regressions("src/example.py", b"", _branching(10)) == []
