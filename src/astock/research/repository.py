"""Safe SQLite indexes for frozen research artifacts."""

from __future__ import annotations

from datetime import UTC

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import BaseCasePack, FrozenEvidencePack


class ResearchRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def get_evidence_pack(self, pack_id: str) -> FrozenEvidencePack | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM frozen_evidence_pack_index WHERE pack_id=?",
                (pack_id,),
            ).fetchone()
        if row is None:
            return None
        return FrozenEvidencePack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def evidence_pack_object_hash(self, pack_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM frozen_evidence_pack_index WHERE pack_id=?",
                (pack_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_evidence_pack(
        self,
        pack: FrozenEvidencePack,
        *,
        object_hash: str,
        request_hash: str,
    ) -> FrozenEvidencePack:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash FROM frozen_evidence_pack_index "
                "WHERE pack_id=?",
                (pack.pack_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ValueError(f"frozen evidence pack collision: {pack.pack_id}")
                return FrozenEvidencePack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO frozen_evidence_pack_index("
                "pack_id,company_id,as_of,formal_historical,allow_approximated,"
                "coverage_status,claim_count,evidence_count,open_conflict_count,object_hash,"
                "request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pack.pack_id,
                    pack.company_id,
                    pack.as_of.astimezone(UTC).isoformat(),
                    int(pack.formal_historical),
                    int(pack.allow_approximated),
                    pack.coverage_status.value,
                    len(pack.claim_ids),
                    len(pack.evidence_ids),
                    len(pack.open_conflict_ids),
                    object_hash,
                    request_hash,
                    pack.created_at.isoformat(),
                ),
            )
        return pack

    def latest_evidence_pack_summary(self, company_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT pack_id,company_id,as_of,formal_historical,allow_approximated,"
                "coverage_status,claim_count,evidence_count,open_conflict_count,object_hash,"
                "created_at FROM frozen_evidence_pack_index WHERE company_id=? "
                "ORDER BY as_of DESC,created_at DESC,pack_id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_base_case(self, base_case_id: str) -> BaseCasePack | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM base_case_pack_index WHERE base_case_id=?",
                (base_case_id,),
            ).fetchone()
        if row is None:
            return None
        return BaseCasePack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def base_case_object_hash(self, base_case_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM base_case_pack_index WHERE base_case_id=?",
                (base_case_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_base_case(
        self,
        pack: BaseCasePack,
        *,
        object_hash: str,
        draft_hash: str,
    ) -> BaseCasePack:
        finding_count = sum(len(values) for values in pack.findings_by_section.values())
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,draft_hash FROM base_case_pack_index WHERE base_case_id=?",
                (pack.base_case_id,),
            ).fetchone()
            if row is not None:
                if str(row["draft_hash"]) != draft_hash:
                    raise ValueError(f"BaseCase identity collision: {pack.base_case_id}")
                return BaseCasePack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO base_case_pack_index("
                "base_case_id,evidence_pack_id,company_id,as_of,kernel_version,coverage_status,"
                "finding_count,evidence_count,gap_count,object_hash,draft_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pack.base_case_id,
                    pack.evidence_pack_id,
                    pack.company_id,
                    pack.as_of.astimezone(UTC).isoformat(),
                    pack.kernel_version,
                    pack.coverage_status.value,
                    finding_count,
                    len(pack.evidence_ids),
                    len(pack.evidence_gaps),
                    object_hash,
                    draft_hash,
                    pack.created_at.isoformat(),
                ),
            )
        return pack

    def latest_base_case_summary(self, company_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT base_case_id,evidence_pack_id,company_id,as_of,kernel_version,"
                "coverage_status,finding_count,evidence_count,gap_count,object_hash,created_at "
                "FROM base_case_pack_index WHERE company_id=? "
                "ORDER BY as_of DESC,created_at DESC,base_case_id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return dict(row) if row else None


__all__ = ["ResearchRepository"]
