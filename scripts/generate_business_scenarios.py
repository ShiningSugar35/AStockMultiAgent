from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

CAPABILITY_MAP = {
    "CR": "COMPANY_RESEARCH",
    "MQ": "CURRENT_MARKET",
    "MA": "MARKET_REGIME",
    "IN": "INDUSTRY",
    "FI": "FINANCIAL_INTEGRITY",
    "GV": "GOVERNANCE",
    "EV": "EVENT_RESEARCH",
    "FV": "FORECAST_VALUATION",
    "RR": "RED_TEAM",
    "CO": "COMMITTEE",
    "PO": "PORTFOLIO",
    "HR": "HOLDING_REVIEW",
    "FM": "FULL_MARKET",
    "SR": "SUBJECT_REGISTRY",
    "EA": "EXTERNAL_ACCOUNT",
    "ET": "ETF",
    "PT": "PAPER",
}
SIDE_EFFECT_TOKENS = (
    "EA_PROVISIONAL",
    "EA_WRITE",
    "PT_PREPARE",
    "PT_CONFIRM",
    "PT_REPLAY",
    "READ",
    "META",
    "NONE",
)

BASE_CAPABILITIES = {
    "REQUEST_TIME",
    "ENTITY_IDENTITY",
    "SESSION_PREFLIGHT",
    "SUBJECT_REGISTRY",
    "RESPONSE_GATEWAY",
}
EXPECTED_IDS = [
    1,
    2,
    3,
    *range(11, 15),
    *range(22, 30),
    *range(31, 39),
    *range(41, 49),
    *range(51, 58),
    *range(61, 68),
    *range(71, 78),
    *range(81, 91),
    *range(91, 97),
]


def _intent(scenario_id: int) -> str:
    if scenario_id in {22, 23, 24, 25, 26, 27, 28, 29}:
        return "ACCOUNT_FACT_WRITE"
    if scenario_id == 95:
        return "PAPER_PREPARE"
    if scenario_id == 96:
        return "PAPER_STATUS"
    if 81 <= scenario_id <= 90:
        return "MONITOR"
    if 11 <= scenario_id <= 14 or scenario_id in {66, 75, 76}:
        return "HOLDING_DECISION"
    if 31 <= scenario_id <= 38 or scenario_id == 48 or 91 <= scenario_id <= 94:
        return "PORTFOLIO_DECISION"
    if 41 <= scenario_id <= 47:
        return "RECOMMENDATION"
    if scenario_id in {1, 2, 3}:
        return "BUY_DECISION"
    return "RESEARCH"


def parse_matrix(path: Path) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*`([^`]*)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$",
            line,
        )
        if match is None:
            continue
        scenario_id = int(match.group(1))
        title = match.group(2).strip()
        chain = match.group(3).strip()
        side_effect_description = re.sub(r"`", "", match.group(4)).strip()
        side_effects = [token for token in SIDE_EFFECT_TOKENS if token in side_effect_description]
        if not side_effects:
            raise ValueError(f"scenario {scenario_id} has no recognized side effect")
        acceptance = match.group(5).strip()
        tokens = set(re.findall(r"\b[A-Z]{1,2}\b", chain))
        required = set(BASE_CAPABILITIES)
        required.update(CAPABILITY_MAP[token] for token in tokens if token in CAPABILITY_MAP)
        prohibited: set[str] = set()
        if set(side_effects).issubset({"READ", "META", "NONE"}):
            prohibited.update({"EXTERNAL_ACCOUNT", "PAPER"})
        elif any(token.startswith("EA_") for token in side_effects):
            required.add("EXTERNAL_ACCOUNT")
            prohibited.add("PAPER")
        elif any(token.startswith("PT_") for token in side_effects):
            required.add("PAPER")
            prohibited.add("EXTERNAL_ACCOUNT")
        required -= prohibited
        scenarios.append(
            {
                "id": scenario_id,
                "title": title,
                "intent": _intent(scenario_id),
                "required": sorted(required),
                "conditional": [],
                "prohibited": sorted(prohibited),
                "allowed_side_effects": side_effects,
                "side_effect_description": side_effect_description,
                "acceptance": acceptance,
                "source_chain": chain,
            }
        )
    ids = [item["id"] for item in scenarios]
    if ids != EXPECTED_IDS:
        raise ValueError(f"business scenario ids differ from the frozen contract: {ids}")
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("docs/acceptance/business-question-capability-matrix-v1.md"),
    )
    parser.add_argument("--output", type=Path, default=Path("configs/business_scenarios_v1.yaml"))
    args = parser.parse_args()
    payload = {
        "schema_version": "business-scenario-manifest-v1",
        "policy_version": "investor-capability-policy-v1",
        "source_matrix": str(args.matrix).replace("\\", "/"),
        "scenario_count": len(EXPECTED_IDS),
        "scenarios": parse_matrix(args.matrix),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
