from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from astock.core.atomic import atomic_write_text
from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore


def test_object_store_hash_idempotency_and_chinese_path(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "研究对象")
    payload = "贵州茅台证据".encode()
    first = store.put_bytes(payload)
    second = store.put_bytes(payload)
    assert first.sha256 == sha256_bytes(payload)
    assert first == second
    assert store.get_bytes(first.sha256) == payload


def test_concurrent_put_returns_one_content_identity(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    payload = b"same immutable response"
    with ThreadPoolExecutor(max_workers=8) as executor:
        refs = list(executor.map(store.put_bytes, [payload] * 32))
    assert {ref.sha256 for ref in refs} == {sha256_bytes(payload)}
    assert len(list((tmp_path / "objects").rglob(refs[0].sha256))) == 1


def test_atomic_utf8_write_replaces_complete_file(tmp_path: Path) -> None:
    path = tmp_path / "中文目录" / "报告.json"
    atomic_write_text(path, "第一版")
    atomic_write_text(path, "第二版")
    assert path.read_text(encoding="utf-8") == "第二版"
    assert not list(path.parent.glob("*.tmp"))
