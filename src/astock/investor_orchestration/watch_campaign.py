"""Bounded campaign checkpoints over the canonical registry/state store.

This is metadata coordination, not a price store, research engine or order path.
Run with `python -B -m astock.investor_orchestration.watch_campaign --help`.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from pydantic import Field

from astock.core.errors import AStockError
from astock.core.object_store import ObjectStore
from astock.core.request_budget import RequestExecutionBudget
from astock.core.state import StateStore
from astock.financial_sources.repository import FinancialSourceReleaseRepository
from astock.investor_orchestration.full_research import FullResearchRecommendationService
from astock.investor_orchestration.host_continuation import HostContinuationPolicy
from astock.investor_orchestration.models import StrictModel
from astock.investor_orchestration.run_ownership import schedule_run_ownership
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

if TYPE_CHECKING:
    from astock.financial_sources import FinancialSourceService

SCOPE = "watch-campaign"
# Compatibility default for owner takeover, independent of any host activation limit.
DEFAULT_OWNER_LEASE_SECONDS = 2100


class CampaignRound(StrictModel):
    round_id: str
    slot: Literal["am", "pm"]
    status: Literal["PENDING", "PARTIAL", "COMPLETE"] = "PENDING"
    owner: str = ""
    lease_generation: int = Field(default=0, ge=0)
    invocation_started_at: datetime
    deadline_at: datetime  # Owner lease expiry; never a host or logical research deadline.
    checkpoint: str = Field(default="", max_length=12000)
    completed_units: int = Field(default=0, ge=0)
    total_units: int = Field(default=1, ge=1)
    child_task_id: str | None = None
    report_file: str | None = None


class CampaignBeginResult(CampaignRound):
    lease_acquired: bool
    lease_disposition: Literal[
        "ACQUIRED", "REUSED", "LIVE_OWNER", "ALREADY_COMPLETE", "EXPIRED_LEASE"
    ]


class CampaignCheckpointResult(CampaignRound):
    checkpoint_applied: bool
    checkpoint_disposition: Literal["APPLIED", "STALE_LEASE"]


class Campaign(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    report_period: Literal["2026-09-30"] = "2026-09-30"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    portfolio_max_names: int = Field(default=5, ge=1, le=5)
    # Member identities are projected from ResearchSubjectRegistry, not copied here.
    bindings: dict[str, str] = Field(default_factory=dict, max_length=8)
    rounds: dict[str, CampaignRound] = Field(default_factory=dict, max_length=2)
    q3_release_ids: dict[str, str] = Field(default_factory=dict, max_length=50)
    final_recommendation_receipt_id: str | None = None
    lifecycle: Literal["OBSERVING", "PORTFOLIO_MONITORING"] = "OBSERVING"


class CampaignReadiness(TypedDict):
    campaign_id: str
    q3_ready: bool
    missing_q3: list[str]
    q3_release_ids: dict[str, str]
    portfolio_ready: bool
    ready: bool
    receipt_id: str | None
    position_count: int
    reason: str


class CampaignRecoveryPlan(TypedDict):
    campaign_id: str
    report_period: str
    members: list[str]
    missing_q3: list[str]
    requests: list[dict[str, str]]
    existing_bindings: dict[str, str]
    final_receipt_id: str | None


class CampaignDeltaReport(TypedDict):
    instrument_id: str
    report: NotRequired[dict[str, object]]
    failure_code: NotRequired[str]


def _pending_recovery_batch(
    missing: list[str], previous_checkpoint: str, maximum_members: int,
) -> tuple[list[str], list[str]]:
    """Advance through eligible members before retrying a failed first tranche."""
    try:
        previous = json.loads(previous_checkpoint)
    except (TypeError, ValueError):
        previous = {}
    history = previous.get("attempted_members", []) if (
        isinstance(previous, dict)
        and previous.get("schema_version") == "campaign-q3-delta-v1"
    ) else []
    valid = set(missing)
    attempted = list(dict.fromkeys(
        member for member in history
        if isinstance(member, str) and member in valid
    )) if isinstance(history, list) else []
    untried = [member for member in missing if member not in set(attempted)]
    if not untried:
        # A completed sweep may retry still-missing sources on the next generation.
        attempted, untried = [], missing
    return untried[:maximum_members], attempted


class WatchCampaignService:
    def __init__(self, database: Path, campaign_id: str) -> None:
        if not database.is_file():
            raise ValueError("existing canonical database required; no implicit initialization")
        self.database = database.resolve()
        self.state = StateStore(self.database)
        self.subjects = ResearchSubjectRegistryService(
            InvestorOrchestrationStore(self.database)
        )
        self.campaign_id = Campaign(campaign_id=campaign_id).campaign_id
        # One configured provider service per invocation, not one per portfolio member.
        self._financial_service_instance: FinancialSourceService | None = None

    def load(self) -> Campaign:
        row = self.state.get_checkpoint(SCOPE, self.campaign_id)
        if row is None:
            raise ValueError("campaign not initialized")
        return Campaign.model_validate(row["cursor"])

    def _save(self, campaign: Campaign) -> None:
        self.state.set_checkpoint(
            scope_type=SCOPE,
            scope_key=self.campaign_id,
            cursor=campaign.model_dump(mode="json"),
            status=campaign.lifecycle,
        )

    def initialize(self, members: list[str]) -> Campaign:
        if not members or len(members) > 50 or len(set(members)) != len(members):
            raise ValueError("require unique bounded instrument identities")
        if any(
            not re.fullmatch(r"(?:XSHG|XSHE):[0-9]{6}", value) for value in members
        ):
            raise ValueError("explicit exchange and six-digit code required")
        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            existing = self.state.get_checkpoint(SCOPE, self.campaign_id)
            if existing is not None:
                if set(self.members()) != set(members):
                    raise ValueError(
                        "campaign membership differs; do not overwrite an existing campaign"
                    )
                return Campaign.model_validate(existing["cursor"])
            with self.subjects.store.transaction():
                for instrument in members:
                    self.subjects.add_watchlist(
                        instrument,
                        reason="用户指定2026三季报观察；仅候选，不是持仓或买入建议",
                        request_id=self.campaign_id,
                        idempotency_key=f"{self.campaign_id}:watch:{instrument}",
                    )
            campaign = Campaign(campaign_id=self.campaign_id)
            self._save(campaign)
            return campaign

    def members(self) -> list[str]:
        events = self.subjects.store.subject_events(event_types=("WATCHLIST_ADDED",))
        return sorted(
            {
                event.instrument_id
                for event in events
                if event.request_id == self.campaign_id
            }
        )

    def bind(self, role: str, task_id: str) -> Campaign:
        valid_roles = {"am", "pm", "implementation", "watch", "continuation"}
        if role not in valid_roles or not task_id.strip():
            raise ValueError("unsupported binding role or blank platform task ID")
        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            campaign = self.load()
            campaign.bindings[role] = task_id
            self._save(campaign)
            return campaign

    def begin(
        self,
        slot: Literal["am", "pm"],
        bucket: str,
        owner: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_OWNER_LEASE_SECONDS,
    ) -> CampaignBeginResult:
        if not owner.strip() or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", bucket):
            raise ValueError("owner and YYYY-MM-DD Shanghai schedule bucket required")
        at = now or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("timezone-aware clock required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("owner lease duration must be a positive integer")
        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            campaign = self.load()
            current = campaign.rounds.get(slot)
            target_id = f"{self.campaign_id}:{bucket}:{slot}"
            if (
                current is not None
                and current.status == "COMPLETE"
                and current.round_id == target_id
            ):
                return CampaignBeginResult.model_validate(
                    {
                        **current.model_dump(),
                        "lease_acquired": False,
                        "lease_disposition": "ALREADY_COMPLETE",
                    }
                )
            if current is not None and current.status != "COMPLETE":
                if current.owner == owner:
                    return CampaignBeginResult.model_validate(
                        {
                            **current.model_dump(),
                            "lease_acquired": current.deadline_at > at,
                            "lease_disposition": (
                                "REUSED" if current.deadline_at > at else "EXPIRED_LEASE"
                            ),
                        }
                    )  # Retry cannot reset this invocation's clock.
                if current.owner and current.deadline_at > at:
                    return CampaignBeginResult.model_validate(
                        {
                            **current.model_dump(),
                            "lease_acquired": False,
                            "lease_disposition": "LIVE_OWNER",
                        }
                    )
                current = current.model_copy(
                    update={
                        "status": "PENDING",
                        "owner": owner,
                        "lease_generation": current.lease_generation + 1,
                        "invocation_started_at": at,
                        "deadline_at": at
                        + timedelta(seconds=lease_seconds),
                    }
                )
            else:
                current = CampaignRound(
                    round_id=target_id,
                    slot=slot,
                    owner=owner,
                    lease_generation=(current.lease_generation + 1 if current else 1),
                    invocation_started_at=at,
                    deadline_at=at + timedelta(seconds=lease_seconds),
                    child_task_id=campaign.bindings.get("continuation"),
                )
            campaign.rounds[slot] = current
            self._save(campaign)
            return CampaignBeginResult.model_validate(
                {
                    **current.model_dump(),
                    "lease_acquired": True,
                    "lease_disposition": "ACQUIRED",
                }
            )

    def checkpoint(
        self,
        slot: str,
        owner: str,
        payload: dict[str, object],
        *,
        round_id: str | None = None,
        lease_generation: int | None = None,
    ) -> CampaignCheckpointResult:
        allowed = {
            "checkpoint",
            "completed_units",
            "total_units",
            "child_task_id",
            "report_file",
            "status",
        }
        if set(payload) - allowed:
            raise ValueError("checkpoint payload contains unsupported fields")
        if (round_id is None) != (lease_generation is None):
            raise ValueError("round id and lease generation must be supplied together")
        if lease_generation is not None and lease_generation < 0:
            raise ValueError("lease generation must be non-negative")
        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            campaign = self.load()
            current = campaign.rounds[slot]
            explicit_lease = round_id is not None
            if explicit_lease:
                lease_matches = (
                    bool(current.owner)
                    and current.owner == owner
                    and current.round_id == round_id
                    and current.lease_generation == lease_generation
                )
            else:
                lease_matches = bool(current.owner) and bool(owner) and current.owner == owner
            if not lease_matches or current.deadline_at <= datetime.now(UTC):
                return CampaignCheckpointResult.model_validate(
                    {
                        **current.model_dump(),
                        "checkpoint_applied": False,
                        "checkpoint_disposition": "STALE_LEASE",
                    }
                )
            revised = CampaignRound.model_validate(
                {**current.model_dump(), **payload}
            )
            if revised.completed_units > revised.total_units:
                raise ValueError("completed work exceeds the frozen denominator")
            if (
                revised.status == "COMPLETE"
                and revised.completed_units != revised.total_units
            ):
                raise ValueError("incomplete work cannot be marked complete")
            # A missing old report projection must not block saving research progress.
            # Newly registered reports still require an existing project-local file.
            if "report_file" in payload and revised.report_file:
                path = Path(revised.report_file).resolve()
                if (
                    not path.is_relative_to(self.database.parent.parent)
                    or not path.is_file()
                ):
                    raise ValueError("report must be an existing project-local file")
            if revised.status in {"PARTIAL", "COMPLETE"}:
                revised = revised.model_copy(update={"owner": ""})
            campaign.rounds[slot] = revised
            self._save(campaign)
            return CampaignCheckpointResult.model_validate(
                {
                    **revised.model_dump(),
                    "checkpoint_applied": True,
                    "checkpoint_disposition": "APPLIED",
                }
            )

    @staticmethod
    def _official_q3_release(
        instrument_id: str,
        row: dict[str, object] | None,
    ) -> bool:
        if row is None or str(row.get("instrument_id") or "") != instrument_id:
            return False
        if str(row.get("status") or "") != "CERTIFIED":
            return False
        if str(row.get("manifest_schema_version") or "") != "financial-source-release-v2":
            return False
        official_kinds = {
            "CNINFO_EXHAUSTIVE_ENUMERATION",
            "OFFICIAL_WEB_EXACT_ITEM_ADMISSION",
        }
        if str(row.get("official_lineage_kind") or "") not in official_kinds:
            return False
        return bool(
            str(row.get("official_document_id") or "").strip()
            and str(row.get("official_snapshot_id") or "").strip()
            and str(row.get("release_id") or "").strip()
        )

    def _financial_service(self) -> FinancialSourceService:
        if self._financial_service_instance is None:
            from astock.financial_sources import FinancialSourceParquetStore, FinancialSourceService

            root = self.database.parent.parent
            self._financial_service_instance = FinancialSourceService(
                self.state, ObjectStore(self.database.parent / "objects" / "sha256"),
                FinancialSourceParquetStore(
                    self.database.parent / "data" / "parquet" / "financial_sources"
                ),
                root,
            )
        return self._financial_service_instance

    def _verified_q3_release(self, instrument_id: str, row: dict[str, object] | None) -> str | None:
        if row is None:
            return None
        from astock.schemas import FinancialPeriodType

        try:
            release = self._financial_service().read_certified_release(
                instrument_id.split(":", 1)[1], date.fromisoformat(self.load().report_period),
                FinancialPeriodType.QUARTERLY, as_of=datetime.now(UTC),
            )
            if release is None:
                return None
            manifest = release.manifest
            bound_row = {**row, "official_lineage_kind": manifest.official_lineage_kind.value}
            if (
                not self._official_q3_release(instrument_id, bound_row)
                or manifest.instrument_id != instrument_id
                or manifest.release_id != row["release_id"]
            ):
                return None
            return manifest.release_id
        except (OSError, ValueError, AStockError):
            return None

    def recovery_plan(self) -> CampaignRecoveryPlan:
        """Projection only: reuse the campaign and current registered member denominator."""
        readiness = self.finalization_readiness()
        missing = list(readiness["missing_q3"])
        campaign = self.load()
        return {
            "campaign_id": self.campaign_id, "report_period": campaign.report_period,
            "members": self.members(), "missing_q3": missing,
            "requests": [
                {"instrument_id": instrument,
                 "request_id": f"{self.campaign_id}:{campaign.report_period}:{instrument}"}
                for instrument in missing
            ],
            "existing_bindings": dict(campaign.bindings),
            "final_receipt_id": campaign.final_recommendation_receipt_id,
        }

    def recover_missing_q3(
        self, *, slot: str, owner: str, round_id: str, lease_generation: int,
        maximum_members: int = 18, elapsed_budget_seconds: int = 600, live: bool = False,
    ) -> dict[str, object]:
        """Acquire unresolved Q3 members once per owned round generation at a time."""
        with schedule_run_ownership(
            self.database, "watch-campaign-q3", f"{round_id}:{lease_generation}"
        ):
            return self._recover_missing_q3(
                slot=slot, owner=owner, round_id=round_id, lease_generation=lease_generation,
                maximum_members=maximum_members,
                elapsed_budget_seconds=elapsed_budget_seconds, live=live,
            )

    def _recover_missing_q3(
        self, *, slot: str, owner: str, round_id: str, lease_generation: int,
        maximum_members: int, elapsed_budget_seconds: int, live: bool,
    ) -> dict[str, object]:
        from astock.schemas import FinancialPeriodType, Market

        if not 1 <= maximum_members <= 50 or not 1 <= elapsed_budget_seconds <= 1800:
            raise ValueError("campaign recovery exceeds the bounded work envelope")

        def require_lease() -> CampaignRound:
            current = self.load().rounds.get(slot)
            if current is None or (
                current.owner != owner or not owner or current.round_id != round_id
                or current.lease_generation != lease_generation
                or current.deadline_at <= datetime.now(UTC) or current.status != "PENDING"
            ):
                raise ValueError("stale campaign recovery lease")
            return current

        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            current = require_lease()
        plan = self.recovery_plan()
        selected, prior_attempted = _pending_recovery_batch(
            list(plan["missing_q3"]), current.checkpoint, maximum_members,
        )
        budget = RequestExecutionBudget(
            self.state, f"{round_id}:{lease_generation}:q3-recovery",
            min(current.deadline_at, datetime.now(UTC) + timedelta(seconds=elapsed_budget_seconds)),
            max(32, maximum_members * 6),
        )
        reports: list[CampaignDeltaReport] = []
        service = self._financial_service() if selected else None
        with budget.bind():
            for instrument in selected:
                require_lease()
                if budget.remaining_seconds() <= 0 or budget.remaining_attempts() <= 0:
                    break
                market, company = instrument.split(":", 1)
                assert service is not None
                try:
                    report = service.sync(
                        company, Market(market), date.fromisoformat(str(plan["report_period"])),
                        FinancialPeriodType.QUARTERLY, live=live,
                    )
                    reports.append({
                        "instrument_id": instrument, "report": report.model_dump(mode="json"),
                    })
                except (OSError, ValueError, TimeoutError, AStockError) as exc:
                    reports.append({
                        "instrument_id": instrument, "failure_code": type(exc).__name__,
                    })
                try:
                    require_lease()
                except ValueError:
                    # Evidence already frozen by sync remains usable; this generation stops.
                    break
        # No recommendation or release is inferred from successful transport.
        readiness = self.finalization_readiness()
        checkpoint = self.checkpoint(
            slot, owner, {
                "checkpoint": json.dumps(
                    {"schema_version": "campaign-q3-delta-v1",
                     "reports": [{
                         "instrument_id": item["instrument_id"],
                         "status": item.get("failure_code") or item.get("report", {}).get("status"),
                         "release_id": item.get("report", {}).get("release_id"),
                     } for item in reports],
                     "missing_q3": readiness["missing_q3"],
                     "attempted_members": list(dict.fromkeys([
                         *prior_attempted,
                         *(item["instrument_id"] for item in reports),
                     ]))}, ensure_ascii=False
                ),
                "completed_units": len(readiness["q3_release_ids"]),
                "total_units": len(self.members()),
                "status": "COMPLETE" if readiness["q3_ready"] else "PARTIAL",
            }, round_id=round_id, lease_generation=lease_generation,
        )
        return {"campaign_id": self.campaign_id, "attempted": len(reports), "reports": reports,
                "readiness": readiness, "checkpoint": checkpoint.model_dump(mode="json")}

    def finalization_readiness(self, receipt_id: str | None = None) -> CampaignReadiness:
        campaign = self.load()
        members = self.members()
        repository = FinancialSourceReleaseRepository(self.state)
        releases: dict[str, str] = {}
        missing_q3: list[str] = []
        for instrument_id in members:
            company_id = instrument_id.split(":", 1)[1]
            row = repository.get(
                company_id,
                campaign.report_period,
                "QUARTERLY",
            )
            release_id = self._verified_q3_release(instrument_id, row)
            if release_id is None:
                missing_q3.append(instrument_id)
                continue
            releases[instrument_id] = release_id

        result: CampaignReadiness = {
            "campaign_id": self.campaign_id,
            "q3_ready": not missing_q3 and len(releases) == len(members),
            "missing_q3": missing_q3,
            "q3_release_ids": releases,
            "portfolio_ready": False,
            "ready": False,
            "receipt_id": receipt_id,
            "position_count": 0,
            "reason": "Q3_RELEASES_INCOMPLETE" if missing_q3 else "FORMAL_PORTFOLIO_REQUIRED",
        }
        if missing_q3 or not receipt_id:
            return result

        try:
            receipt, verification = FullResearchRecommendationService(
                self.subjects.store
            ).load_and_replay(receipt_id)
        except (OSError, ValueError) as exc:
            result["reason"] = f"FORMAL_RECEIPT_INVALID:{type(exc).__name__}"
            return result

        position_ids = [item.instrument_id for item in receipt.portfolio.positions]
        exact_universe = set(receipt.candidate_universe) == set(members)
        unique_positions = len(position_ids) == len(set(position_ids))
        bounded_positions = (
            1 <= len(position_ids) <= campaign.portfolio_max_names
            and set(position_ids) <= set(members)
            and unique_positions
        )
        formal_publication = (
            verification.get("status") == "PASS"
            and receipt.publication.status == "PUBLISH"
            and receipt.publication.formal_recommendation_allowed
        )
        portfolio_ready = exact_universe and bounded_positions and formal_publication
        result.update(
            {
                "portfolio_ready": portfolio_ready,
                "ready": portfolio_ready,
                "position_count": len(position_ids),
                "reason": "READY" if portfolio_ready else "FORMAL_PORTFOLIO_NOT_ADMITTED",
            }
        )
        return result

    def finalize(self, receipt_id: str) -> Campaign:
        if not receipt_id.strip():
            raise ValueError("finalization requires a RecommendationResearchReceipt id")
        with schedule_run_ownership(self.database, SCOPE, self.campaign_id):
            campaign = self.load()
            if campaign.lifecycle == "PORTFOLIO_MONITORING":
                if campaign.final_recommendation_receipt_id == receipt_id:
                    return campaign
                raise ValueError("campaign already finalized with a different receipt")
            readiness = self.finalization_readiness(receipt_id)
            if readiness["ready"] is not True:
                raise ValueError(f"campaign finalization is blocked: {readiness['reason']}")
            release_ids = readiness["q3_release_ids"]
            if not isinstance(release_ids, dict):
                raise ValueError("campaign finalization release binding is invalid")
            q3_release_ids = {
                str(instrument_id): str(release_id)
                for instrument_id, release_id in release_ids.items()
            }
            revised = campaign.model_copy(
                update={
                    "q3_release_ids": q3_release_ids,
                    "final_recommendation_receipt_id": receipt_id,
                    "lifecycle": "PORTFOLIO_MONITORING",
                }
            )
            self._save(revised)
            return revised

    def status(self) -> dict[str, object]:
        campaign = self.load()
        output = campaign.model_dump(mode="json")
        output["members"] = self.members()
        output["budgets"] = {
            slot: HostContinuationPolicy.evaluate(
                host="CHATGPT_CHAT",
                elapsed_seconds=max(
                    0,
                    int(
                        (
                            datetime.now(UTC) - item.invocation_started_at
                        ).total_seconds()
                    ),
                ),
                completed_units=item.completed_units,
                total_units=item.total_units,
                existing_platform_task_id=item.child_task_id,
            ).model_dump(mode="json")
            for slot, item in campaign.rounds.items()
        }
        output["financial_decision_authority"] = False
        output["paper_ledger_write_allowed"] = False
        return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init", "status", "bind", "begin", "checkpoint", "readiness",
            "recovery-plan", "recover-q3", "finalize",
        ],
    )
    parser.add_argument("--database", type=Path, default=Path("runtime/state.sqlite"))
    parser.add_argument("--campaign", default="ten-stock-q3-2026")
    parser.add_argument("--members", nargs="+")
    parser.add_argument("--role")
    parser.add_argument("--task-id")
    parser.add_argument("--slot", choices=["am", "pm"])
    parser.add_argument("--bucket")
    parser.add_argument("--owner")
    parser.add_argument("--round-id")
    parser.add_argument("--lease-generation", type=int)
    parser.add_argument(
        "--lease-seconds", type=int, default=DEFAULT_OWNER_LEASE_SECONDS,
        help="owner takeover lease; does not set a host or research deadline",
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--receipt-id")
    parser.add_argument("--maximum-members", type=int, default=18)
    parser.add_argument("--elapsed-budget-seconds", type=int, default=600)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    service = WatchCampaignService(args.database, args.campaign)
    if args.action == "init":
        result = service.initialize(args.members or []).model_dump(mode="json")
    elif args.action == "bind":
        result = service.bind(
            args.role or "",
            args.task_id or "",
        ).model_dump(mode="json")
    elif args.action == "begin":
        if args.slot is None:
            parser.error("begin requires --slot")
        result = service.begin(
            args.slot,
            args.bucket or "",
            args.owner or "",
            lease_seconds=args.lease_seconds,
        ).model_dump(mode="json")
    elif args.action == "checkpoint":
        if args.input is None or args.input.stat().st_size > 65536:
            parser.error("checkpoint requires a JSON input of at most 64 KiB")
        result = service.checkpoint(
            args.slot or "",
            args.owner or "",
            json.loads(args.input.read_text(encoding="utf-8")),
            round_id=args.round_id,
            lease_generation=args.lease_generation,
        ).model_dump(mode="json")
    elif args.action == "recovery-plan":
        result = service.recovery_plan()
    elif args.action == "recover-q3":
        if not args.round_id or args.lease_generation is None:
            parser.error("recover-q3 requires the existing round and lease generation")
        result = service.recover_missing_q3(
            slot=args.slot or "", owner=args.owner or "", round_id=args.round_id,
            lease_generation=args.lease_generation, maximum_members=args.maximum_members,
            elapsed_budget_seconds=args.elapsed_budget_seconds, live=args.live,
        )
    elif args.action == "readiness":
        result = service.finalization_readiness(args.receipt_id)
    elif args.action == "finalize":
        if not args.receipt_id:
            parser.error("finalize requires --receipt-id")
        result = service.finalize(args.receipt_id).model_dump(mode="json")
    else:
        result = service.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
