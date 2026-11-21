"""Research public imports stay compatible without eager numerical initialization."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "statement",
    [
        "import astock.research",
        "from astock.research.process_worker import KillableResearchWorker",
        "import scripts.run_local_quality",
    ],
)
def test_lightweight_imports_do_not_initialize_cli_or_numerical_stacks(statement: str) -> None:
    code = (
        "import json, sys\n"
        + statement
        + "\nprint(json.dumps([name for name in sys.modules "
        "if name.split('.')[0] in {'numpy', 'scipy', 'sklearn'} "
        "or name == 'astock.research.runtime_cli']))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=20,
    )

    assert json.loads(result.stdout) == []


def test_public_exports_preserve_identity_cache_and_introspection() -> None:
    import astock.research as research
    from astock.research.config import load_research_core_config
    from astock.research.repository import ResearchRepository

    assert research.load_research_core_config is load_research_core_config
    assert research.ResearchRepository is ResearchRepository
    assert vars(research)["ResearchRepository"] is ResearchRepository
    assert set(research.__all__).issubset(dir(research))
    missing_export = "missing_research_export"
    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(research, missing_export)
