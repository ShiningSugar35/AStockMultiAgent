"""Internal industry research archetype registry.

The registry supplies research questions/KPIs/valuation lenses without claiming to be a
certified external industry taxonomy. Private Skills are optional edge, never a prerequisite.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.schemas.research_team import IndustryResearchArchetype, IndustryResearchMatch


class IndustryResearchRegistry:
    def __init__(
        self, archetypes: list[IndustryResearchArchetype], *, registry_version: str
    ) -> None:
        if len(archetypes) < 18:
            raise ValueError("industry research registry must cover at least 18 archetypes")
        ids = [item.archetype_id for item in archetypes]
        if len(ids) != len(set(ids)):
            raise ValueError("industry archetype ids must be unique")
        self.archetypes = archetypes
        self.registry_version = registry_version

    @classmethod
    def load(cls, path: Path) -> IndustryResearchRegistry:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != "industry-research-archetypes-v1"
        ):
            raise ValueError("Unsupported industry research archetype registry")
        if raw.get("taxonomy_kind") != "INTERNAL_RESEARCH_ARCHETYPE":
            raise ValueError("industry archetype registry cannot claim an external taxonomy")
        if raw.get("certified_external_taxonomy") is not False:
            raise ValueError("internal archetype registry must not claim certification")
        rows = raw.get("archetypes")
        if not isinstance(rows, list):
            raise ValueError("industry archetypes must be a list")
        archetypes = [IndustryResearchArchetype.model_validate(item) for item in rows]
        return cls(archetypes, registry_version=str(raw.get("registry_version") or ""))

    def resolve(self, query: str) -> IndustryResearchMatch:
        normalized = query.strip()
        if not normalized:
            raise ValueError("industry query must be non-empty")
        matches: list[tuple[int, str, IndustryResearchArchetype]] = []
        for archetype in self.archetypes:
            for alias in archetype.aliases:
                if alias.casefold() in normalized.casefold():
                    matches.append((len(alias), alias, archetype))
        if not matches:
            return IndustryResearchMatch(
                query=normalized,
                status="UNCLASSIFIED",
                archetype=None,
                matched_alias=None,
            )
        _, alias, archetype = max(
            matches,
            key=lambda item: (item[0], item[2].archetype_id, item[1]),
        )
        return IndustryResearchMatch(
            query=normalized,
            status="MATCHED",
            archetype=archetype,
            matched_alias=alias,
        )

    def inventory(self) -> dict[str, object]:
        return {
            "schema_version": "industry-research-inventory-v1",
            "registry_version": self.registry_version,
            "taxonomy_kind": "INTERNAL_RESEARCH_ARCHETYPE",
            "certified_external_taxonomy": False,
            "archetype_count": len(self.archetypes),
            "archetypes": [item.model_dump(mode="json") for item in self.archetypes],
            "private_skill_required_for_analysis": False,
        }


__all__ = ["IndustryResearchRegistry"]
