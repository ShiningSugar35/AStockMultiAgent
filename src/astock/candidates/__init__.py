"""Research-only deterministic candidate registry."""

from astock.candidates.config import CandidateScanConfig, load_candidate_scan_config
from astock.candidates.repository import CandidateRepository
from astock.candidates.service import CandidateInterrupted, CandidateScanService
from astock.candidates.storage import CandidateParquetStore
from astock.candidates.verification import (
    CandidateInputVerifier,
    CandidateTestInputVerifier,
    CandidateVerificationResult,
    ProductionCandidateInputVerifier,
)

__all__ = [
    "CandidateInterrupted",
    "CandidateInputVerifier",
    "CandidateParquetStore",
    "CandidateRepository",
    "CandidateScanConfig",
    "CandidateScanService",
    "CandidateTestInputVerifier",
    "CandidateVerificationResult",
    "ProductionCandidateInputVerifier",
    "load_candidate_scan_config",
]
