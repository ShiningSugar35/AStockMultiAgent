"""Auditable Universe coverage proof contracts shared by reference and research layers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas.base import AStockModel
from astock.schemas.market import Market

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
UNIVERSE_COVERAGE_PROOF_SCHEMA_VERSION = "universe-coverage-proof-v1"
UNIVERSE_COVERAGE_POLICY_VERSION = "universe-coverage-policy-v1"
UNIVERSE_COVERAGE_ENGINEERING_MIN_RATIO = 0.995
_ENGINEERING_MIN_RATIO = UNIVERSE_COVERAGE_ENGINEERING_MIN_RATIO
_EQUITY_MARKETS = {Market.XSHG, Market.XSHE, Market.BJSE}


class UniverseCoverageLevel(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"
    ENGINEERING_HIGH_COVERAGE = "ENGINEERING_HIGH_COVERAGE"
    OFFICIAL_DENOMINATOR_RECONCILED = "OFFICIAL_DENOMINATOR_RECONCILED"


class UniverseDenominatorAuthority(StrEnum):
    UNKNOWN = "UNKNOWN"
    SECONDARY_SELF_REPORTED = "SECONDARY_SELF_REPORTED"
    PRIMARY_OFFICIAL = "PRIMARY_OFFICIAL"
    QUALIFIED_AUTHORIZED_MASTER = "QUALIFIED_AUTHORIZED_MASTER"


class MarketCoverageReconciliation(AStockModel):
    schema_version: str = "market-coverage-reconciliation-v1"
    market: Market
    coverage_level: UniverseCoverageLevel
    denominator_source_id: str | None = None
    denominator_capability: str | None = None
    denominator_authority: UniverseDenominatorAuthority = UniverseDenominatorAuthority.UNKNOWN
    denominator_count: int | None = Field(default=None, ge=1)
    numerator_count: int = Field(default=0, ge=0)
    missing_count: int | None = Field(default=None, ge=0)
    extra_count: int | None = Field(default=None, ge=0)
    coverage_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    source_version: str | None = None
    observed_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    available_to_system_at: AwareDatetime | None = None
    denominator_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    numerator_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("source_snapshot_ids", "reason_codes")
    @classmethod
    def _sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("coverage reconciliation list fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _reconcile(self) -> MarketCoverageReconciliation:
        if self.market not in _EQUITY_MARKETS:
            raise ValueError("Universe coverage reconciliation requires an A-share equity market")
        if self.available_to_system_at is not None:
            if self.observed_at is not None and self.observed_at > self.available_to_system_at:
                raise ValueError("coverage observation cannot occur after system availability")
            if self.published_at is not None and self.published_at > self.available_to_system_at:
                raise ValueError("coverage publication cannot occur after system availability")
        if self.denominator_count is None:
            if (
                self.coverage_ratio is not None
                or self.missing_count is not None
                or self.extra_count is not None
            ):
                raise ValueError("coverage ratio/difference counts require a denominator")
            if self.coverage_level not in {
                UniverseCoverageLevel.UNAVAILABLE,
                UniverseCoverageLevel.PARTIAL,
            }:
                raise ValueError("high coverage requires an auditable denominator")
        else:
            expected_ratio = min(1.0, self.numerator_count / self.denominator_count)
            if self.coverage_ratio is None or abs(self.coverage_ratio - expected_ratio) > 1e-12:
                raise ValueError("coverage ratio must reconcile with numerator and denominator")
            if self.missing_count is None or self.extra_count is None:
                raise ValueError("known denominator requires explicit missing/extra counts")
            if self.missing_count != max(self.denominator_count - self.numerator_count, 0):
                raise ValueError("missing count does not reconcile")
            if self.extra_count != max(self.numerator_count - self.denominator_count, 0):
                raise ValueError("extra count does not reconcile")
        if self.coverage_level is UniverseCoverageLevel.UNAVAILABLE and self.numerator_count:
            raise ValueError("UNAVAILABLE coverage cannot claim observed numerator rows")
        if self.coverage_level in {
            UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE,
            UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
        }:
            if self.coverage_ratio is None or self.coverage_ratio < _ENGINEERING_MIN_RATIO:
                raise ValueError("high coverage requires >=99.5% reconciliation")
            if self.extra_count:
                raise ValueError("high coverage cannot contain rows outside its denominator")
            if (
                not self.denominator_source_id
                or not self.denominator_capability
                or not self.denominator_object_hash
                or not self.numerator_object_hash
                or not self.source_snapshot_ids
            ):
                raise ValueError(
                    "high coverage requires source/capability, "
                    "numerator/denominator hashes and snapshots"
                )
            if self.available_to_system_at is None or not self.source_version:
                raise ValueError("high coverage requires source version and availability")
        if self.coverage_level is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED:
            if self.denominator_authority not in {
                UniverseDenominatorAuthority.PRIMARY_OFFICIAL,
                UniverseDenominatorAuthority.QUALIFIED_AUTHORIZED_MASTER,
            }:
                raise ValueError(
                    "formal Universe coverage requires official/authorized denominator"
                )
        elif (
            self.denominator_authority
            in {
                UniverseDenominatorAuthority.PRIMARY_OFFICIAL,
                UniverseDenominatorAuthority.QUALIFIED_AUTHORIZED_MASTER,
            }
            and self.coverage_level is UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
        ):
            raise ValueError("verified official/authorized high coverage must use the formal level")
        return self


def universe_coverage_proof_identity(
    *,
    as_of: datetime,
    coverage_level: UniverseCoverageLevel,
    market_reconciliations: Sequence[MarketCoverageReconciliation],
    reason_codes: Sequence[str],
    policy_version: str = UNIVERSE_COVERAGE_POLICY_VERSION,
    engineering_min_ratio: float = UNIVERSE_COVERAGE_ENGINEERING_MIN_RATIO,
) -> dict[str, object]:
    """Build the canonical PIT-sensitive identity for one Universe proof."""

    return {
        "schema_version": UNIVERSE_COVERAGE_PROOF_SCHEMA_VERSION,
        "policy_version": policy_version,
        "engineering_min_ratio": engineering_min_ratio,
        "as_of": as_of.isoformat(),
        "coverage_level": coverage_level.value,
        "market_reconciliations": [
            item.model_dump(mode="json", exclude={"created_at"})
            for item in market_reconciliations
        ],
        "reason_codes": list(reason_codes),
    }


def universe_coverage_proof_id(
    *,
    as_of: datetime,
    coverage_level: UniverseCoverageLevel,
    market_reconciliations: Sequence[MarketCoverageReconciliation],
    reason_codes: Sequence[str],
    policy_version: str = UNIVERSE_COVERAGE_POLICY_VERSION,
    engineering_min_ratio: float = UNIVERSE_COVERAGE_ENGINEERING_MIN_RATIO,
) -> str:
    """Hash every semantic and PIT field without volatile-key projection."""

    identity = universe_coverage_proof_identity(
        as_of=as_of,
        coverage_level=coverage_level,
        market_reconciliations=market_reconciliations,
        reason_codes=reason_codes,
        policy_version=policy_version,
        engineering_min_ratio=engineering_min_ratio,
    )
    return sha256_bytes(canonical_json_bytes(identity))


class UniverseCoverageProof(AStockModel):
    schema_version: str = UNIVERSE_COVERAGE_PROOF_SCHEMA_VERSION
    proof_id: str = Field(pattern=_SHA256_PATTERN)
    as_of: AwareDatetime
    coverage_level: UniverseCoverageLevel
    market_reconciliations: list[MarketCoverageReconciliation] = Field(min_length=3, max_length=3)
    policy_version: str = Field(default=UNIVERSE_COVERAGE_POLICY_VERSION, min_length=1)
    engineering_min_ratio: float = Field(default=_ENGINEERING_MIN_RATIO, ge=0.9, le=1)
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("reason_codes")
    @classmethod
    def _sorted_unique_reasons(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("Universe coverage reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _aggregate(self) -> UniverseCoverageProof:
        if self.schema_version != UNIVERSE_COVERAGE_PROOF_SCHEMA_VERSION:
            raise ValueError("Universe coverage proof schema version is unsupported")
        if self.policy_version != UNIVERSE_COVERAGE_POLICY_VERSION:
            raise ValueError("Universe coverage policy version is unsupported")
        by_market = {item.market: item for item in self.market_reconciliations}
        if set(by_market) != _EQUITY_MARKETS or len(by_market) != len(self.market_reconciliations):
            raise ValueError("Universe coverage proof must contain exactly XSHG/XSHE/BJSE once")
        if self.market_reconciliations != sorted(
            self.market_reconciliations,
            key=lambda item: item.market.value,
        ):
            raise ValueError("Universe coverage market proofs must use deterministic market order")
        if abs(self.engineering_min_ratio - _ENGINEERING_MIN_RATIO) > 1e-12:
            raise ValueError("Universe coverage policy ratio requires a versioned policy change")
        expected_reasons = sorted(
            {code for item in self.market_reconciliations for code in item.reason_codes}
        )
        if self.reason_codes != expected_reasons:
            raise ValueError("Universe coverage reason codes must equal the market-proof union")
        levels = {item.coverage_level for item in self.market_reconciliations}
        expected = (
            UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
            if levels == {UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED}
            else (
                UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
                if all(
                    item.coverage_level
                    in {
                        UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE,
                        UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
                    }
                    for item in self.market_reconciliations
                )
                else (
                    UniverseCoverageLevel.UNAVAILABLE
                    if levels == {UniverseCoverageLevel.UNAVAILABLE}
                    else UniverseCoverageLevel.PARTIAL
                )
            )
        )
        if self.coverage_level is not expected:
            raise ValueError("aggregate Universe coverage level does not match market proofs")
        if any(
            item.available_to_system_at is not None and item.available_to_system_at > self.as_of
            for item in self.market_reconciliations
        ):
            raise ValueError("Universe coverage proof cannot predate a market proof")
        expected_proof_id = universe_coverage_proof_id(
            as_of=self.as_of,
            coverage_level=self.coverage_level,
            market_reconciliations=self.market_reconciliations,
            reason_codes=self.reason_codes,
            policy_version=self.policy_version,
            engineering_min_ratio=self.engineering_min_ratio,
        )
        if self.proof_id != expected_proof_id:
            raise ValueError("Universe coverage proof id does not match canonical identity")
        return self

    @property
    def formal_full_market_coverage_allowed(self) -> bool:
        return self.coverage_level is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED


__all__ = [
    "MarketCoverageReconciliation",
    "UNIVERSE_COVERAGE_ENGINEERING_MIN_RATIO",
    "UNIVERSE_COVERAGE_POLICY_VERSION",
    "UNIVERSE_COVERAGE_PROOF_SCHEMA_VERSION",
    "UniverseCoverageLevel",
    "UniverseCoverageProof",
    "UniverseDenominatorAuthority",
    "universe_coverage_proof_id",
    "universe_coverage_proof_identity",
]
