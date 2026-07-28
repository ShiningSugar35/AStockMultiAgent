"""Private-safe immutable Parquet facts for shadow execution observations."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.schemas import ShadowExecutionObservation, ShadowOutcomeDataSource

_PHASE7_FORWARD_COLUMNS = {
    "outcome_data_source",
    "data_available_at",
    "market_snapshot_ids",
    "thesis_status",
    "invalidation_reason_codes",
}

_OBSERVATION_SCHEMA = pa.schema(
    [
        ("observation_id", pa.string()),
        ("observation_version", pa.string()),
        ("supersedes_observation_id", pa.string()),
        ("object_sha256", pa.string()),
        ("observation_sha256", pa.string()),
        ("study_id", pa.string()),
        ("assignment_id", pa.string()),
        ("arm_id", pa.string()),
        ("regime_id", pa.string()),
        ("independence_key", pa.string()),
        ("company_id", pa.string()),
        ("symbol", pa.string()),
        ("market", pa.string()),
        ("horizon_days", pa.int32()),
        ("trading_days_elapsed", pa.int32()),
        ("action", pa.string()),
        ("observation_status", pa.string()),
        ("formal_eligible", pa.bool_()),
        ("signal_time", pa.timestamp("us", tz="UTC")),
        ("entry_time", pa.timestamp("us", tz="UTC")),
        ("valuation_time", pa.timestamp("us", tz="UTC")),
        ("fill_status", pa.string()),
        ("requested_quantity", pa.int64()),
        ("quantity", pa.int64()),
        ("entry_price_fen", pa.int64()),
        ("valuation_price_fen", pa.int64()),
        ("highest_price_fen", pa.int64()),
        ("lowest_price_fen", pa.int64()),
        ("corporate_action_cash_fen", pa.int64()),
        ("gross_pnl_fen", pa.int64()),
        ("commission_fen", pa.int64()),
        ("tax_fen", pa.int64()),
        ("transfer_fee_fen", pa.int64()),
        ("slippage_fen", pa.int64()),
        ("net_pnl_fen", pa.int64()),
        ("capital_at_risk_fen", pa.int64()),
        ("normalization_notional_fen", pa.int64()),
        ("net_return_text", pa.string()),
        ("nav_before_fen", pa.int64()),
        ("nav_after_fen", pa.int64()),
        ("mfe_text", pa.string()),
        ("mae_text", pa.string()),
        ("turnover_fen", pa.int64()),
        ("liquidity_score_text", pa.string()),
        ("market_volume_shares", pa.int64()),
        ("participation_rate_text", pa.string()),
        ("replay_quality", pa.string()),
        ("cost_model_version", pa.string()),
        ("fill_model_version", pa.string()),
        ("corporate_action_version", pa.string()),
        ("market_manifest_sha256", pa.string()),
        ("trading_calendar_snapshot_sha256", pa.string()),
        ("candidate_set_snapshot_sha256", pa.string()),
        ("corporate_action_snapshot_sha256", pa.string()),
        ("delisting_snapshot_sha256", pa.string()),
        ("outcome_data_source", pa.string()),
        ("data_available_at", pa.timestamp("us", tz="UTC")),
        ("market_snapshot_ids", pa.list_(pa.string())),
        ("market_observation_ids", pa.list_(pa.string())),
        ("thesis_status", pa.string()),
        ("invalidation_reason_codes", pa.list_(pa.string())),
        ("pit_statuses", pa.list_(pa.string())),
        ("candidate_membership_pit_safe", pa.bool_()),
        ("corporate_action_coverage_complete", pa.bool_()),
        ("delisting_coverage_complete", pa.bool_()),
        ("t_plus_one_compliant", pa.bool_()),
        ("price_limit_compliant", pa.bool_()),
        ("suspension_compliant", pa.bool_()),
        ("ambiguous_intrabar_path", pa.bool_()),
        ("conservative_path_used", pa.bool_()),
        ("optimistic_net_pnl_fen", pa.int64()),
        ("exclusion_codes", pa.list_(pa.string())),
        ("created_at", pa.timestamp("us", tz="UTC")),
    ]
)


class ParquetShadowStore:
    """Materialize one immutable, audit-safe row per observation version."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, observation: ShadowExecutionObservation) -> Path:
        return (
            self.root
            / "shadow_observations"
            / f"study={quote(observation.study_id, safe='-_.')}"
            / f"year={observation.signal_time.year}"
            / f"{quote(observation.observation_id, safe='-_.')}.parquet"
        )

    def write(
        self,
        observation: ShadowExecutionObservation,
        *,
        object_sha256: str,
    ) -> Path:
        path = self.path_for(observation)
        if path.exists():
            if not self.verify(observation, object_sha256=object_sha256):
                raise ValueError(
                    f"shadow observation Parquet collision: {observation.observation_id}"
                )
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [self._row(observation, object_sha256=object_sha256)],
            schema=_OBSERVATION_SCHEMA,
        )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            pq.write_table(table, temporary, compression="zstd")
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def verify(
        self,
        observation: ShadowExecutionObservation,
        *,
        object_sha256: str,
    ) -> bool:
        path = self.path_for(observation)
        if not path.is_file():
            return False
        try:
            rows = pq.ParquetFile(path).read().to_pylist()
        except (OSError, pa.ArrowException):
            return False
        if len(rows) != 1:
            return False
        expected = self._row(observation, object_sha256=object_sha256)
        actual = rows[0]
        if actual == expected:
            return True
        missing = set(expected) - set(actual)
        return bool(
            observation.outcome_data_source
            is ShadowOutcomeDataSource.LEGACY_UNVERIFIED
            and missing == _PHASE7_FORWARD_COLUMNS
            and not (set(actual) - set(expected))
            and actual == {key: expected[key] for key in actual}
        )

    @staticmethod
    def _row(
        observation: ShadowExecutionObservation,
        *,
        object_sha256: str,
    ) -> dict[str, object]:
        return {
            "observation_id": observation.observation_id,
            "observation_version": observation.observation_version,
            "supersedes_observation_id": observation.supersedes_observation_id,
            "object_sha256": object_sha256,
            "observation_sha256": observation.observation_sha256,
            "study_id": observation.study_id,
            "assignment_id": observation.assignment_id,
            "arm_id": observation.arm_id,
            "regime_id": observation.regime_id,
            "independence_key": observation.independence_key,
            "company_id": observation.company_id,
            "symbol": observation.symbol,
            "market": observation.market.value,
            "horizon_days": observation.horizon_days,
            "trading_days_elapsed": observation.trading_days_elapsed,
            "action": observation.action.value,
            "observation_status": observation.status.value,
            "formal_eligible": observation.formal_eligible,
            "signal_time": observation.signal_time,
            "entry_time": observation.entry_time,
            "valuation_time": observation.valuation_time,
            "fill_status": observation.fill_status.value,
            "requested_quantity": observation.requested_quantity,
            "quantity": observation.quantity,
            "entry_price_fen": observation.entry_price_fen,
            "valuation_price_fen": observation.valuation_price_fen,
            "highest_price_fen": observation.highest_price_fen,
            "lowest_price_fen": observation.lowest_price_fen,
            "corporate_action_cash_fen": observation.corporate_action_cash_fen,
            "gross_pnl_fen": observation.gross_pnl_fen,
            "commission_fen": observation.commission_fen,
            "tax_fen": observation.tax_fen,
            "transfer_fee_fen": observation.transfer_fee_fen,
            "slippage_fen": observation.slippage_fen,
            "net_pnl_fen": observation.net_pnl_fen,
            "capital_at_risk_fen": observation.capital_at_risk_fen,
            "normalization_notional_fen": observation.normalization_notional_fen,
            "net_return_text": str(observation.net_return),
            "nav_before_fen": observation.nav_before_fen,
            "nav_after_fen": observation.nav_after_fen,
            "mfe_text": str(observation.mfe),
            "mae_text": str(observation.mae),
            "turnover_fen": observation.turnover_fen,
            "liquidity_score_text": str(observation.liquidity_score),
            "market_volume_shares": observation.market_volume_shares,
            "participation_rate_text": str(observation.participation_rate),
            "replay_quality": observation.replay_quality.value,
            "cost_model_version": observation.cost_model_version,
            "fill_model_version": observation.fill_model_version,
            "corporate_action_version": observation.corporate_action_version,
            "market_manifest_sha256": observation.market_manifest_sha256,
            "trading_calendar_snapshot_sha256": (
                observation.trading_calendar_snapshot_sha256
            ),
            "candidate_set_snapshot_sha256": (
                observation.candidate_set_snapshot_sha256
            ),
            "corporate_action_snapshot_sha256": (
                observation.corporate_action_snapshot_sha256
            ),
            "delisting_snapshot_sha256": observation.delisting_snapshot_sha256,
            "outcome_data_source": observation.outcome_data_source.value,
            "data_available_at": observation.data_available_at,
            "market_snapshot_ids": observation.market_snapshot_ids,
            "market_observation_ids": observation.market_observation_ids,
            "thesis_status": observation.thesis_status.value,
            "invalidation_reason_codes": observation.invalidation_reason_codes,
            "pit_statuses": [item.value for item in observation.pit_statuses],
            "candidate_membership_pit_safe": (
                observation.candidate_membership_pit_safe
            ),
            "corporate_action_coverage_complete": (
                observation.corporate_action_coverage_complete
            ),
            "delisting_coverage_complete": observation.delisting_coverage_complete,
            "t_plus_one_compliant": observation.t_plus_one_compliant,
            "price_limit_compliant": observation.price_limit_compliant,
            "suspension_compliant": observation.suspension_compliant,
            "ambiguous_intrabar_path": observation.ambiguous_intrabar_path,
            "conservative_path_used": observation.conservative_path_used,
            "optimistic_net_pnl_fen": observation.optimistic_net_pnl_fen,
            "exclusion_codes": observation.exclusion_codes,
            "created_at": observation.created_at,
        }


__all__ = ["ParquetShadowStore"]
