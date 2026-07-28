from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.shadow import (
    ShadowEvaluationService,
    load_shadow_evaluation_policy,
    write_phase7_status,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_phase7_status_is_collecting_and_never_invents_events(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    service = ShadowEvaluationService(
        state,
        ObjectStore(tmp_path / "objects"),
        load_shadow_evaluation_policy(
            PROJECT_ROOT / "configs" / "shadow_evaluation.yaml"
        ),
    )

    target = write_phase7_status(
        service,
        tmp_path / "phase7_status.md",
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
    )
    rendered = target.read_text(encoding="utf-8")

    assert "- Evidence status: `COLLECTING`" in rendered
    assert "- Migration integrity: `PASS`" in rendered
    assert "- Independent forward research events: `0 / 100`" in rendered
    assert "| Skill performance | COLLECTING |" in rendered
    assert "| Committee performance | COLLECTING |" in rendered
    assert "| Research quality | COLLECTING |" in rendered
    assert "Historical replay and backtests cannot satisfy" in rendered
    assert "Reinforcement learning is disabled" in rendered
    assert "Automatic Skill modification is disabled" in rendered

    blocked = write_phase7_status(
        service,
        tmp_path / "phase7_status_blocked.md",
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
        migration_integrity_issue=(
            "Migration checksum changed: 0044_phase6_decision_close.sql"
        ),
    ).read_text(encoding="utf-8")
    assert "Migration integrity: `BLOCKED_CHECKSUM_MISMATCH`" in blocked
    assert "Status read mode: `READ_ONLY`" in blocked
