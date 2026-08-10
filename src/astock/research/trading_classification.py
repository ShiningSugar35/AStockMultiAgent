"""Immutable point-in-time trading classification releases for research runtime."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from astock.core.errors import PolicyError
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.paper_trading.operation import (
    MarketReferencePaperVerifier,
    PaperTradingRuleBook,
)
from astock.schemas.market import Market
from astock.schemas.paper import PaperTradingClassification
from astock.schemas.reference_data import ReferenceDatasetKind, ReferenceSyncReport
from astock.schemas.research_runtime import (
    TradingClassificationCorporateActionBaseline,
    TradingClassificationDraft,
    TradingClassificationRecord,
    TradingClassificationRelease,
    TradingClassificationResolution,
    TradingClassificationStatus,
    TradingPriceLimitRegime,
    TradingSpecialRegime,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RESOLVER_VERSION = "market-reference-trading-classification-v1"


class TradingClassificationStatusReport(TypedDict):
    status: Literal["READY", "NEEDS_INFO"]
    reason_codes: list[str]
    artifact_id: str | None
    release_id: str | None
    broker_execution_allowed: bool


class TradingClassificationAuditReport(TypedDict):
    status: Literal["PASS", "FAIL"]
    finding_codes: list[str]
    artifact_id: str | None
    release_id: str | None
    broker_execution_allowed: bool


class TradingClassificationService:
    """Freeze, resolve, and verify trading classifications without order authorization."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        reference: MarketReferenceService | None = None,
        trading_rules: PaperTradingRuleBook | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.reference = reference
        self.trading_rules = trading_rules

    @staticmethod
    def checkpoint_key(company_id: str, as_of: datetime) -> str:
        return f"{company_id}:{as_of.isoformat()}"

    def latest_for(self, company_id: str, as_of: datetime) -> TradingClassificationRecord | None:
        checkpoint = self.state.get_checkpoint(
            "trading-classification",
            self.checkpoint_key(company_id, as_of),
        )
        if checkpoint is None:
            return None
        artifact_id = checkpoint["cursor"].get("artifact_id")
        if not artifact_id:
            return None
        try:
            record = self.load(str(artifact_id))
        except ValueError:
            return None
        if record.release.company_id != company_id or record.release.as_of != as_of:
            return None
        return record

    def plan_resolution(self, company_id: str, as_of: datetime) -> TradingClassificationResolution:
        existing = self.latest_for(company_id, as_of)
        if (
            existing is not None
            and self.status(existing.artifact_id, as_of=as_of)["status"] == "READY"
        ):
            return TradingClassificationResolution(
                company_id=company_id,
                as_of=as_of,
                status=TradingClassificationStatus.READY,
                artifact_id=existing.artifact_id,
                object_hash=existing.object_hash,
                source_artifact_ids=existing.release.source_artifact_ids,
                created_at=as_of,
            )
        if self.reference is None or self.trading_rules is None:
            reasons = ["TRADING_CLASSIFICATION_RESOLVER_NOT_CONFIGURED"]
        else:
            reasons = self._reference_missing_codes(company_id, as_of)
            if not reasons:
                reasons = ["TRADING_CLASSIFICATION_RESOLUTION_REQUIRED"]
        return TradingClassificationResolution(
            company_id=company_id,
            as_of=as_of,
            status=TradingClassificationStatus.NEEDS_INFO,
            reason_codes=sorted(set(reasons)),
            created_at=as_of,
        )

    def resolve(
        self,
        company_id: str,
        as_of: datetime,
        *,
        live: bool,
        sync_reference_inputs: bool = True,
    ) -> TradingClassificationResolution:
        existing = self.latest_for(company_id, as_of)
        if (
            existing is not None
            and self.status(existing.artifact_id, as_of=as_of)["status"] == "READY"
        ):
            return TradingClassificationResolution(
                company_id=company_id,
                as_of=as_of,
                status=TradingClassificationStatus.READY,
                artifact_id=existing.artifact_id,
                object_hash=existing.object_hash,
                source_artifact_ids=existing.release.source_artifact_ids,
                live_sync_attempted=False,
                created_at=as_of,
            )
        if self.reference is None or self.trading_rules is None:
            return TradingClassificationResolution(
                company_id=company_id,
                as_of=as_of,
                status=TradingClassificationStatus.NEEDS_INFO,
                reason_codes=["TRADING_CLASSIFICATION_RESOLVER_NOT_CONFIGURED"],
                live_sync_attempted=False,
                created_at=as_of,
            )

        verifier = MarketReferencePaperVerifier(self.reference, self.trading_rules)
        sync_attempted = False
        source_artifacts: set[str] = set()
        try:
            if sync_reference_inputs:
                sync_attempted = True
                instrument_report = self.reference.sync_instruments(live=live)
                self._collect_release_artifact(instrument_report, source_artifacts)
            instrument, instrument_release_id = verifier.resolve_instrument(
                company_id,
                visible_at=as_of,
            )
            source_artifacts.add(f"market-reference:{instrument_release_id}")

            local_date = as_of.astimezone(_SHANGHAI).date()
            lookback_start = local_date - timedelta(days=60)
            if (
                instrument.listing_date is not None
                and (local_date - instrument.listing_date).days <= 45
            ):
                lookback_start = instrument.listing_date
            if sync_reference_inputs:
                sync_attempted = True
                calendar_report = self.reference.sync_calendar(
                    instrument.market,
                    lookback_start,
                    local_date,
                    live=live,
                )
                daily_report = self.reference.sync_daily(
                    instrument.symbol,
                    instrument.market,
                    lookback_start,
                    local_date,
                    live=live,
                )
                self._collect_release_artifact(calendar_report, source_artifacts)
                self._collect_release_artifact(daily_report, source_artifacts)

            facts = verifier.trading_classification(instrument, visible_at=as_of)
            if (
                not facts.instrument_release_id
                or not facts.calendar_release_id
                or not facts.daily_release_id
            ):
                raise ValueError("resolved trading facts lack reference release lineage")
            source_artifacts.update(
                {
                    f"market-reference:{facts.instrument_release_id}",
                    f"market-reference:{facts.calendar_release_id}",
                    f"market-reference:{facts.daily_release_id}",
                }
            )
            rulebook_artifact_id = self._freeze_rulebook(as_of)
            source_artifacts.add(rulebook_artifact_id)

            corporate_report = self._corporate_action_report(
                instrument.symbol,
                instrument.market,
                local_date,
                live=live,
                sync=sync_reference_inputs,
            )
            baseline_artifact_id = self._freeze_corporate_action_baseline(
                company_id=company_id,
                market=instrument.market,
                symbol=instrument.symbol,
                as_of=as_of,
                report=corporate_report,
            )
            source_artifacts.add(baseline_artifact_id)
            if corporate_report is None:
                return TradingClassificationResolution(
                    company_id=company_id,
                    as_of=as_of,
                    status=TradingClassificationStatus.NEEDS_INFO,
                    source_artifact_ids=sorted(source_artifacts),
                    reason_codes=["CORPORATE_ACTION_BASELINE_REQUIRED"],
                    live_sync_attempted=sync_attempted,
                    created_at=as_of,
                )
            if corporate_report.coverage.record_count > 0:
                return TradingClassificationResolution(
                    company_id=company_id,
                    as_of=as_of,
                    status=TradingClassificationStatus.NEEDS_INFO,
                    source_artifact_ids=sorted(source_artifacts),
                    reason_codes=[
                        "CORPORATE_ACTION_NEAR_CLASSIFICATION_DATE_REQUIRES_TERMS_VERIFICATION"
                    ],
                    live_sync_attempted=sync_attempted,
                    created_at=as_of,
                )
            baseline_record = self.state.artifact_record(baseline_artifact_id)
            if baseline_record is None:
                raise ValueError("corporate-action baseline artifact is unavailable")
            baseline = TradingClassificationCorporateActionBaseline.model_validate_json(
                self.objects.get_bytes(str(baseline_record["object_hash"]))
            )
            if not baseline.absence_is_officially_certified:
                return TradingClassificationResolution(
                    company_id=company_id,
                    as_of=as_of,
                    status=TradingClassificationStatus.NEEDS_INFO,
                    source_artifact_ids=sorted(source_artifacts),
                    reason_codes=["CORPORATE_ACTION_BASELINE_NOT_CERTIFIED"],
                    live_sync_attempted=sync_attempted,
                    created_at=as_of,
                )

            special_regime = TradingSpecialRegime(facts.special_regime)
            if special_regime is TradingSpecialRegime.SUSPENDED:
                price_regime = TradingPriceLimitRegime.SUSPENDED
            elif special_regime is TradingSpecialRegime.IPO_INITIAL_NO_FIXED_PRICE_LIMIT:
                price_regime = TradingPriceLimitRegime.NO_FIXED
            else:
                price_regime = TradingPriceLimitRegime.FIXED
            draft = TradingClassificationDraft(
                company_id=company_id,
                market=instrument.market,
                symbol=instrument.symbol,
                as_of=as_of,
                effective_from=as_of,
                valid_until=self._valid_until(as_of),
                classification=PaperTradingClassification.model_validate(
                    {
                        "instrument_id": instrument.instrument_id,
                        "board": facts.board,
                        "risk_status": facts.risk_status,
                        "fixed_price_limit_eligible": facts.fixed_price_limit_eligible,
                        "suspension_status_verified": facts.suspension_status_verified,
                        "suspended": facts.suspended,
                        "evidence_id": facts.evidence_id,
                        "created_at": as_of,
                    }
                ),
                special_no_price_limit=price_regime is TradingPriceLimitRegime.NO_FIXED,
                special_regime=special_regime,
                price_limit_regime=price_regime,
                price_limit_rate_bps=facts.price_limit_rate_bps,
                rulebook_artifact_id=rulebook_artifact_id,
                instrument_release_id=facts.instrument_release_id,
                calendar_release_id=facts.calendar_release_id,
                daily_release_id=facts.daily_release_id,
                resolver_version=_RESOLVER_VERSION,
                corporate_action_baseline_artifact_id=baseline_artifact_id,
                source_artifact_ids=sorted(source_artifacts),
                created_at=as_of,
            )
            record = self.freeze(draft)
            return TradingClassificationResolution(
                company_id=company_id,
                as_of=as_of,
                status=TradingClassificationStatus.READY,
                artifact_id=record.artifact_id,
                object_hash=record.object_hash,
                source_artifact_ids=record.release.source_artifact_ids,
                live_sync_attempted=sync_attempted,
                created_at=as_of,
            )
        except (PolicyError, ValueError, OSError) as exc:
            return TradingClassificationResolution(
                company_id=company_id,
                as_of=as_of,
                status=TradingClassificationStatus.NEEDS_INFO,
                source_artifact_ids=sorted(source_artifacts),
                reason_codes=[self._reason_code(exc)],
                live_sync_attempted=sync_attempted,
                created_at=as_of,
            )

    def freeze(self, draft: TradingClassificationDraft) -> TradingClassificationRecord:
        source_hashes: list[str] = []
        for artifact_id in draft.source_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(f"unknown classification source artifact: {artifact_id}")
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                raise ValueError(f"classification source object unavailable: {artifact_id}")
            source_hashes.append(object_hash)
        if draft.corporate_action_baseline_artifact_id is not None:
            baseline = self.state.artifact_record(draft.corporate_action_baseline_artifact_id)
            if baseline is None:
                raise ValueError("corporate-action baseline artifact is unknown")
            baseline_hash = str(baseline["object_hash"])
            if not self.objects.verify(baseline_hash):
                raise ValueError("corporate-action baseline object is unavailable")
            if draft.corporate_action_baseline_artifact_id not in draft.source_artifact_ids:
                raise ValueError("corporate-action baseline must be one of the frozen sources")
            if draft.resolver_version is not None:
                if str(baseline["type"]) != "TradingClassificationCorporateActionBaseline":
                    raise ValueError(
                        "resolved classification requires the exact baseline artifact type"
                    )
                baseline_value = TradingClassificationCorporateActionBaseline.model_validate_json(
                    self.objects.get_bytes(baseline_hash)
                )
                if not baseline_value.absence_is_officially_certified:
                    raise ValueError(
                        "resolved classification requires certified corporate-action absence"
                    )
        source_hashes = sorted(set(source_hashes))

        identity_payload = {
            "schema_version": "trading-classification-release-v2",
            "company_id": draft.company_id,
            "market": draft.market.value,
            "symbol": draft.symbol,
            "as_of": draft.as_of.isoformat(),
            "effective_from": draft.effective_from.isoformat(),
            "valid_until": draft.valid_until.isoformat(),
            "classification": draft.classification.model_dump(mode="json"),
            "special_no_price_limit": draft.special_no_price_limit,
            "special_regime": draft.special_regime.value,
            "price_limit_regime": draft.price_limit_regime.value,
            "price_limit_rate_bps": draft.price_limit_rate_bps,
            "rulebook_artifact_id": draft.rulebook_artifact_id,
            "instrument_release_id": draft.instrument_release_id,
            "calendar_release_id": draft.calendar_release_id,
            "daily_release_id": draft.daily_release_id,
            "resolver_version": draft.resolver_version,
            "corporate_action_baseline_artifact_id": draft.corporate_action_baseline_artifact_id,
            "source_artifact_ids": draft.source_artifact_ids,
            "source_object_hashes": source_hashes,
            "status": draft.status.value,
            "reason_codes": sorted(set(draft.reason_codes)),
            "broker_execution_allowed": False,
        }
        identity = sha256_bytes(canonical_json_bytes(identity_payload))
        release = TradingClassificationRelease(
            release_id=f"trading-classification:{identity}",
            company_id=draft.company_id,
            market=draft.market,
            symbol=draft.symbol,
            as_of=draft.as_of,
            effective_from=draft.effective_from,
            valid_until=draft.valid_until,
            classification=draft.classification,
            special_no_price_limit=draft.special_no_price_limit,
            special_regime=draft.special_regime,
            price_limit_regime=draft.price_limit_regime,
            price_limit_rate_bps=draft.price_limit_rate_bps,
            rulebook_artifact_id=draft.rulebook_artifact_id,
            instrument_release_id=draft.instrument_release_id,
            calendar_release_id=draft.calendar_release_id,
            daily_release_id=draft.daily_release_id,
            resolver_version=draft.resolver_version,
            corporate_action_baseline_artifact_id=draft.corporate_action_baseline_artifact_id,
            source_artifact_ids=draft.source_artifact_ids,
            source_object_hashes=source_hashes,
            status=draft.status,
            reason_codes=sorted(set(draft.reason_codes)),
            created_at=draft.created_at,
        )
        object_ref = self.objects.put_json(release.model_dump(mode="json"))
        artifact_id = f"TradingClassificationRelease:{release.release_id}"
        existing = self.state.artifact_record(artifact_id)
        replay = existing is not None
        if existing is not None:
            if (
                str(existing["type"]) != "TradingClassificationRelease"
                or str(existing["schema_version"]) != release.schema_version
                or str(existing["object_hash"]) != object_ref.sha256
                or sorted(existing["input_hashes"]) != source_hashes
            ):
                raise ValueError("trading classification release identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="TradingClassificationRelease",
                schema_version=release.schema_version,
                object_hash=object_ref.sha256,
                input_hashes=source_hashes,
            )
        self.state.set_checkpoint(
            scope_type="trading-classification",
            scope_key=self.checkpoint_key(draft.company_id, draft.as_of),
            cursor={
                "artifact_id": artifact_id,
                "release_id": release.release_id,
                "status": release.status.value,
            },
            status="SUCCEEDED"
            if release.status is TradingClassificationStatus.READY
            else "NEEDS_INFO",
            object_hash=object_ref.sha256,
        )
        return TradingClassificationRecord(
            release=release,
            artifact_id=artifact_id,
            object_hash=object_ref.sha256,
            idempotent_replay=replay,
        )

    def load(self, artifact_id: str) -> TradingClassificationRecord:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "TradingClassificationRelease":
            raise ValueError("unknown trading classification release artifact")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("trading classification release object is unavailable")
        release = TradingClassificationRelease.model_validate_json(
            self.objects.get_bytes(object_hash)
        )
        return TradingClassificationRecord(
            release=release,
            artifact_id=artifact_id,
            object_hash=object_hash,
            idempotent_replay=True,
        )

    def status(
        self,
        artifact_id: str,
        *,
        as_of: datetime | None = None,
    ) -> TradingClassificationStatusReport:
        try:
            record = self.load(artifact_id)
        except ValueError as exc:
            return {
                "status": "NEEDS_INFO",
                "reason_codes": [str(exc)],
                "artifact_id": None,
                "release_id": None,
                "broker_execution_allowed": False,
            }
        release = record.release
        current = as_of or release.as_of
        reasons = list(release.reason_codes)
        if current < release.effective_from or current > release.valid_until:
            reasons.append("TRADING_CLASSIFICATION_OUTSIDE_VALIDITY")
        if release.status is not TradingClassificationStatus.READY:
            reasons.append("TRADING_CLASSIFICATION_NOT_READY")
        if not release.classification.suspension_status_verified:
            reasons.append("SUSPENSION_STATUS_NOT_VERIFIED")
        return {
            "status": "READY" if not reasons else "NEEDS_INFO",
            "artifact_id": artifact_id,
            "release_id": release.release_id,
            "reason_codes": sorted(set(reasons)),
            "broker_execution_allowed": False,
        }

    def audit(self, artifact_id: str) -> TradingClassificationAuditReport:
        findings: list[str] = []
        try:
            record = self.load(artifact_id)
        except ValueError as exc:
            return {
                "status": "FAIL",
                "finding_codes": [str(exc)],
                "artifact_id": None,
                "release_id": None,
                "broker_execution_allowed": False,
            }
        release = record.release
        registry = self.state.artifact_record(artifact_id)
        assert registry is not None
        if sorted(registry["input_hashes"]) != release.source_object_hashes:
            findings.append("CLASSIFICATION_SOURCE_HASH_DRIFT")
        actual_source_hashes: list[str] = []
        for source_id in release.source_artifact_ids:
            source = self.state.artifact_record(source_id)
            if source is None:
                findings.append("CLASSIFICATION_SOURCE_ARTIFACT_DRIFT")
                continue
            source_hash = str(source["object_hash"])
            actual_source_hashes.append(source_hash)
            if not self.objects.verify(source_hash):
                findings.append("CLASSIFICATION_SOURCE_OBJECT_MISSING")
        if sorted(set(actual_source_hashes)) != release.source_object_hashes:
            findings.append("CLASSIFICATION_SOURCE_ARTIFACT_DRIFT")
        if self.status(artifact_id, as_of=release.as_of)["status"] != "READY":
            findings.append("CLASSIFICATION_NOT_READY_AT_AS_OF")
        if release.resolver_version is not None:
            for required in (
                release.rulebook_artifact_id,
                release.corporate_action_baseline_artifact_id,
            ):
                if required is None or required not in release.source_artifact_ids:
                    findings.append("CLASSIFICATION_RESOLVER_LINEAGE_DRIFT")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "release_id": release.release_id,
            "finding_codes": sorted(set(findings)),
            "broker_execution_allowed": False,
        }

    def _freeze_rulebook(self, as_of: datetime) -> str:
        assert self.trading_rules is not None
        payload = {
            "schema_version": "trading-classification-rulebook-v1",
            "rule_version": self.trading_rules.rule_version,
            "board_rules": [asdict(item) for item in self.trading_rules.board_rules],
            "price_limit_rules": [asdict(item) for item in self.trading_rules.price_limit_rules],
            "broker_execution_allowed": False,
        }
        object_ref = self.objects.put_json(payload)
        artifact_id = f"TradingClassificationRuleBook:{self.trading_rules.rule_version}"
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if str(existing["object_hash"]) != object_ref.sha256:
                raise ValueError("trading classification rulebook identity collision")
            return artifact_id
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="TradingClassificationRuleBook",
            schema_version="trading-classification-rulebook-v1",
            object_hash=object_ref.sha256,
            input_hashes=[],
        )
        return artifact_id

    def _corporate_action_report(
        self,
        symbol: str,
        market: Market,
        local_date: date,
        *,
        live: bool,
        sync: bool,
    ) -> ReferenceSyncReport | None:
        assert self.reference is not None
        if not sync:
            return None
        return self.reference.sync_corporate_actions(
            symbol,
            market,
            local_date - timedelta(days=3),
            local_date + timedelta(days=3),
            live=live,
        )

    def _freeze_corporate_action_baseline(
        self,
        *,
        company_id: str,
        market: Market,
        symbol: str,
        as_of: datetime,
        report: ReferenceSyncReport | None,
    ) -> str:
        local_date = as_of.astimezone(_SHANGHAI).date()
        payload = {
            "company_id": company_id,
            "market": market.value,
            "symbol": symbol,
            "as_of": as_of.isoformat(),
            "window_start": (local_date - timedelta(days=3)).isoformat(),
            "window_end": (local_date + timedelta(days=3)).isoformat(),
            "reference_status": report.status.value if report is not None else "NOT_SYNCED",
            "release_id": report.release_id if report is not None else None,
            "manifest_object_hash": report.manifest_object_hash if report is not None else None,
            "raw_snapshot_ids": report.raw_snapshot_ids if report is not None else [],
            "observed_record_count": report.coverage.record_count if report is not None else 0,
            "reason_codes": report.reason_codes
            if report is not None
            else ["CORPORATE_ACTION_NOT_SYNCED"],
        }
        digest = sha256_bytes(canonical_json_bytes(payload))
        baseline = TradingClassificationCorporateActionBaseline(
            baseline_id=f"trading-corporate-action-baseline:{digest}",
            company_id=company_id,
            market=market,
            symbol=symbol,
            as_of=as_of,
            window_start=payload["window_start"],
            window_end=payload["window_end"],
            reference_status=payload["reference_status"],
            release_id=payload["release_id"],
            manifest_object_hash=payload["manifest_object_hash"],
            raw_snapshot_ids=sorted(set(payload["raw_snapshot_ids"])),
            observed_record_count=payload["observed_record_count"],
            reason_codes=sorted(set(payload["reason_codes"])),
            created_at=as_of,
        )
        object_ref = self.objects.put_json(baseline.model_dump(mode="json"))
        artifact_id = f"TradingClassificationCorporateActionBaseline:{baseline.baseline_id}"
        existing = self.state.artifact_record(artifact_id)
        inputs: list[str] = []
        if report is not None and report.release_id is not None:
            release_artifact = self.state.artifact_record(f"market-reference:{report.release_id}")
            if release_artifact is not None:
                inputs.append(str(release_artifact["object_hash"]))
        if existing is not None:
            if str(existing["object_hash"]) != object_ref.sha256 or sorted(
                existing["input_hashes"]
            ) != sorted(inputs):
                raise ValueError("corporate-action classification baseline collision")
            return artifact_id
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="TradingClassificationCorporateActionBaseline",
            schema_version=baseline.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=inputs,
        )
        return artifact_id

    def _reference_missing_codes(self, company_id: str, as_of: datetime) -> list[str]:
        if self.reference is None:
            return ["REFERENCE_SERVICE_NOT_CONFIGURED"]
        verifier = MarketReferencePaperVerifier(self.reference, self.trading_rules)
        try:
            instrument, _ = verifier.resolve_instrument(company_id, visible_at=as_of)
        except (PolicyError, ValueError):
            return ["INSTRUMENT_REFERENCE_REQUIRED"]
        reasons: list[str] = []
        for kind, scope, code in (
            (
                ReferenceDatasetKind.TRADING_CALENDAR,
                instrument.market.value,
                "CALENDAR_REFERENCE_REQUIRED",
            ),
            (
                ReferenceDatasetKind.DAILY_UNADJUSTED,
                f"{instrument.market.value}:{instrument.symbol}",
                "DAILY_SUSPENSION_REFERENCE_REQUIRED",
            ),
        ):
            status = self.reference.status(kind, scope, as_of=as_of)
            if status.get("status") != "AVAILABLE":
                reasons.append(code)
        return reasons

    @staticmethod
    def _collect_release_artifact(report: ReferenceSyncReport, result: set[str]) -> None:
        if report.release_id is not None:
            result.add(f"market-reference:{report.release_id}")

    @staticmethod
    def _valid_until(as_of: datetime) -> datetime:
        local = as_of.astimezone(_SHANGHAI)
        return datetime.combine(local.date(), time(23, 59, 59), tzinfo=_SHANGHAI).astimezone(UTC)

    @staticmethod
    def _reason_code(exc: Exception) -> str:
        message = str(exc).casefold()
        if "instrument" in message or "listing" in message:
            return "INSTRUMENT_REFERENCE_REQUIRED"
        if "calendar" in message or "open session" in message:
            return "CALENDAR_REFERENCE_REQUIRED"
        if "daily" in message or "suspension" in message:
            return "DAILY_SUSPENSION_REFERENCE_REQUIRED"
        if "board" in message or "price-limit" in message:
            return "TRADING_RULE_REFERENCE_REQUIRED"
        if "special" in message or "delisting" in message:
            return "SPECIAL_REGIME_REFERENCE_REQUIRED"
        return "TRADING_CLASSIFICATION_RESOLUTION_FAILED"


__all__ = ["TradingClassificationService"]
