from __future__ import annotations

from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "状态.sqlite", PROJECT_ROOT / "migrations")
    store.migrate()
    return store


@pytest.fixture
def object_store(tmp_path: Path) -> ObjectStore:
    return ObjectStore(tmp_path / "对象" / "sha256")
