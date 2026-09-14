"""Public-core industry methodology registry.

The registry enriches the core INDUSTRY research task with reusable questions, metrics,
valuation lenses, and red flags. It is intentionally separate from the specialist Skill
router so missing private/blogger Skills or specialist drafts cannot create NEEDS_INFO.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from astock.research.industry_archetypes import IndustryResearchRegistry
from astock.schemas.industry_methodologies import (
    IndustryMethodologyBundle,
    IndustryMethodologySkill,
    IndustryMethodologySource,
)


class IndustryMethodologyRegistry:
    def __init__(
        self,
        *,
        registry_version: str,
        released_at: datetime,
        archetypes: IndustryResearchRegistry,
        sources: list[IndustryMethodologySource],
        methodologies: list[IndustryMethodologySkill],
    ) -> None:
        self.registry_version = registry_version
        if released_at.tzinfo is None or released_at.utcoffset() is None:
            raise ValueError("industry methodology released_at must be timezone-aware")
        self.released_at = released_at
        self.archetypes = archetypes
        self.sources = {item.source_id: item for item in sources}
        self.methodologies = {item.methodology_id: item for item in methodologies}
        if len(self.sources) != len(sources):
            raise ValueError("industry methodology source ids must be unique")
        if len(self.methodologies) != len(methodologies):
            raise ValueError("industry methodology ids must be unique")

        fallback = [item for item in methodologies if item.generic_fallback]
        if len(fallback) != 1:
            raise ValueError("industry methodology registry requires exactly one generic fallback")
        self.generic = fallback[0]

        known_archetypes = {item.archetype_id for item in archetypes.archetypes}
        referenced_archetypes = {
            archetype_id
            for item in methodologies
            if not item.generic_fallback
            for archetype_id in item.archetype_ids
        }
        unknown_archetypes = sorted(referenced_archetypes - known_archetypes)
        if unknown_archetypes:
            raise ValueError(
                "industry methodology registry references unknown archetypes: "
                + ",".join(unknown_archetypes)
            )
        self.uncovered_archetype_ids = tuple(sorted(known_archetypes - referenced_archetypes))

        known_sources = set(self.sources)
        for item in methodologies:
            missing_sources = sorted(set(item.source_ids) - known_sources)
            if missing_sources:
                raise ValueError(
                    f"industry methodology {item.methodology_id} references unknown sources: "
                    + ",".join(missing_sources)
                )

        self.by_archetype: dict[str, list[IndustryMethodologySkill]] = {}
        for item in methodologies:
            if item.generic_fallback:
                continue
            for archetype_id in item.archetype_ids:
                self.by_archetype.setdefault(archetype_id, []).append(item)
        for values in self.by_archetype.values():
            values.sort(key=lambda item: item.methodology_id)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        archetypes: IndustryResearchRegistry,
    ) -> IndustryMethodologyRegistry:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != "industry-methodologies-v1":
            raise ValueError("Unsupported industry methodology registry")
        policy = raw.get("policy")
        if not isinstance(policy, dict):
            raise ValueError("industry methodology policy is missing")
        required_false = (
            "private_skill_required_for_analysis",
            "specialist_draft_required",
            "fact_authority_allowed",
            "recommendation_allowed",
        )
        if any(policy.get(key) is not False for key in required_false):
            raise ValueError("industry methodology authority boundary was weakened")
        if policy.get("allow_generic_fallback_for_uncovered_archetype") is not True:
            raise ValueError("uncovered archetypes must retain generic methodology fallback")

        raw_sources = raw.get("sources")
        raw_methodologies = raw.get("methodologies")
        if not isinstance(raw_sources, list) or not isinstance(raw_methodologies, list):
            raise ValueError("industry methodology sources/methodologies must be lists")
        if any(not isinstance(item, dict) for item in (*raw_sources, *raw_methodologies)):
            raise ValueError("industry methodology rows must be mappings")
        raw_released_at = raw.get("released_at")
        if isinstance(raw_released_at, datetime):
            released_at = raw_released_at
        else:
            released_at = datetime.fromisoformat(str(raw_released_at or "").replace("Z", "+00:00"))
        if released_at.tzinfo is None or released_at.utcoffset() is None:
            raise ValueError("industry methodology released_at must be timezone-aware")
        sources = [
            IndustryMethodologySource.model_validate({**item, "created_at": released_at})
            for item in raw_sources
        ]
        methodologies = [
            IndustryMethodologySkill.model_validate({**item, "created_at": released_at})
            for item in raw_methodologies
        ]
        return cls(
            registry_version=str(raw.get("registry_version") or ""),
            released_at=released_at,
            archetypes=archetypes,
            sources=sources,
            methodologies=methodologies,
        )

    def resolve(self, query: str) -> IndustryMethodologyBundle:
        match = self.archetypes.resolve(query)
        if match.status == "MATCHED":
            assert match.archetype is not None
            archetype_id = match.archetype.archetype_id
            sector_methods = self.by_archetype.get(archetype_id, [])
            methods = [self.generic, *sector_methods]
            status = "MATCHED" if sector_methods else "ARCHETYPE_FALLBACK"
        else:
            archetype_id = None
            methods = [self.generic]
            status = "GENERIC_FALLBACK"
        methods = sorted(methods, key=lambda item: item.methodology_id)
        source_ids = sorted({source_id for item in methods for source_id in item.source_ids})
        return IndustryMethodologyBundle(
            query=query.strip(),
            status=status,
            archetype_id=archetype_id,
            methodology_ids=[item.methodology_id for item in methods],
            methodologies=methods,
            sources=[self.sources[source_id] for source_id in source_ids],
            created_at=self.released_at,
        )

    def inventory(self) -> dict[str, object]:
        known_archetypes = sorted(item.archetype_id for item in self.archetypes.archetypes)
        covered_archetypes = sorted(self.by_archetype)
        return {
            "schema_version": "industry-methodology-inventory-v1",
            "registry_version": self.registry_version,
            "released_at": self.released_at.isoformat(),
            "source_count": len(self.sources),
            "methodology_count": len(self.methodologies),
            "generic_methodology_id": self.generic.methodology_id,
            "known_archetype_count": len(known_archetypes),
            "covered_archetype_count": len(covered_archetypes),
            "uncovered_archetype_ids": list(self.uncovered_archetype_ids),
            "private_skill_required_for_analysis": False,
            "specialist_draft_required": False,
            "fact_authority_allowed": False,
            "recommendation_allowed": False,
        }


__all__ = ["IndustryMethodologyRegistry"]
