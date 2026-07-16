from __future__ import annotations

import json
from pathlib import Path

from astock.core.codex_runs import CodexRunService
from astock.financial_integrity import FinancialIntegrityService
from tests.helpers import make_financial_request

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_financial_pack_is_a_validated_codex_artifact(
    tmp_path: Path, state, object_store
) -> None:
    financial = FinancialIntegrityService(
        state,
        object_store,
        rule_config_path=PROJECT_ROOT / "configs" / "financial_rules.yaml",
        industry_profile_path=PROJECT_ROOT / "configs" / "financial_industry_profiles.yaml",
    ).run(make_financial_request(state, object_store))
    evidence_ids = sorted(
        {
            evidence_id
            for number in financial.pack.verified_numbers
            for evidence_id in number.evidence_ids
        }
    )
    codex = CodexRunService(tmp_path / "codex-runtime", object_store, state)
    manifest = codex.initialize({"request": "validate a frozen financial pack"})
    draft = tmp_path / "financial-draft.json"
    draft.write_text(
        json.dumps(
            {
                "artifact_type": "FinancialIntegrityEvidencePack",
                "payload": financial.pack.model_dump(mode="json"),
                "citations": {
                    evidence_id: "recorded official annual report page 1"
                    for evidence_id in evidence_ids
                },
                "requested_commands": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    codex.stage_draft(manifest.run_id, draft)
    report = codex.import_draft(manifest.run_id)
    assert report.valid
    assert report.artifact_hash is not None
