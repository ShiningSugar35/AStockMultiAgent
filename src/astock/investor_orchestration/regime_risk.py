"""Pure risk-cap composition over the canonical PortfolioIntentProfile.

This module does not own an account/profile store and cannot produce trades.
Multipliers adjust a research budget, never the user's absolute risk ceiling.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from astock.investor_orchestration.utils import content_hash
from astock.schemas.portfolio_decision import PortfolioIntentProfile

# Legacy spellings are accepted only when they agree with the canonical field.
_LIMIT_ALIASES = {
    "max_total_equity_weight": (
        "max_total_exposure",
        "max_total_equity_weight",
        "max_gross_exposure",
        "max_equity_weight",
    ),
    "max_single_name_weight": (
        "max_single_position",
        "max_single_name_weight",
        "max_position_weight",
        "single_name_cap",
    ),
    "minimum_cash_weight": ("minimum_cash_weight", "min_cash_weight", "cash_floor"),
    "max_drawdown": ("max_drawdown", "drawdown_limit"),
    "max_turnover": ("maximum_turnover_weight", "max_turnover", "turnover_limit"),
    "max_industry_exposure": ("max_industry_exposure",),
    "max_abs_correlation": ("max_abs_correlation",),
    "target_annualized_volatility": ("target_annualized_volatility",),
    "max_market_beta": ("max_market_beta",),
}


def bind_profile_limits(
    profile: BaseModel | Mapping[str, Any] | None,
    defaults: Mapping[str, Any],
) -> dict[str, float | int | str]:
    if isinstance(profile, PortfolioIntentProfile):
        # model_copy(update=...) is not validation, even for frozen instances.
        profile = PortfolioIntentProfile.model_validate(profile.model_dump())
    elif isinstance(profile, BaseModel):
        raise ValueError("risk limits require the canonical PortfolioIntentProfile")
    raw = profile.model_dump(mode="json") if isinstance(profile, BaseModel) else dict(profile or {})
    result: dict[str, float | int | str] = {}
    for output, aliases in _LIMIT_ALIASES.items():
        values: list[float] = []
        for name in aliases:
            value = raw.get(name)
            if value is None:
                continue
            if isinstance(value, bool):
                raise ValueError(f"risk limit {name} must be numeric, not boolean")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"risk limit {name} must be numeric") from exc
            if not math.isfinite(numeric) or numeric < 0:
                raise ValueError(f"risk limit {name} must be finite and nonnegative")
            if output != "max_market_beta" and numeric > 1:
                raise ValueError(f"risk limit {name} is outside the allowed range")
            values.append(numeric)
        if values:
            if any(
                not math.isclose(values[0], value, rel_tol=0, abs_tol=1e-12) for value in values
            ):
                raise ValueError(f"conflicting aliases for risk limit {output}")
            result[output] = values[0]
    required = {"max_total_equity_weight", "max_single_name_weight", "minimum_cash_weight"}
    missing = required - result.keys()
    if set(defaults) != required:
        raise ValueError("regime conservative defaults must define the three absolute limits")
    for name in missing:
        value = float(defaults[name])
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("regime default risk limit must be finite and within range")
        result[name] = value
    result["profile_status"] = (
        "CONSERVATIVE_DEFAULT"
        if profile is None
        else "CONSERVATIVE_DEFAULT_FROM_INCOMPLETE_PROFILE"
        if missing
        else "BOUND"
    )
    result["profile_hash"] = content_hash(raw)
    if raw.get("portfolio_id") is not None:
        result["portfolio_id"] = str(raw["portfolio_id"])
    return result


def effective_risk_caps(
    limits: Mapping[str, float | int | str],
    policy: Mapping[str, Any],
) -> dict[str, float]:
    absolute_total = min(
        float(limits["max_total_equity_weight"]), 1 - float(limits["minimum_cash_weight"])
    )
    absolute_single = min(float(limits["max_single_name_weight"]), absolute_total)
    total = min(absolute_total, absolute_total * float(policy["total_risk_multiplier"]))
    single = min(absolute_single, total, absolute_single * float(policy["single_name_multiplier"]))
    # No separate user new-position budget exists; it cannot exceed total/cash/turnover constraints.
    new = min(total, absolute_total * float(policy["new_position_multiplier"]))
    if "max_turnover" in limits:
        new = min(new, float(limits["max_turnover"]))
    return {
        "effective_total_equity_cap": max(0.0, total),
        "effective_single_name_cap": max(0.0, single),
        "effective_new_position_cap": max(0.0, new),
    }


def require_snapshot_time(snapshot: BaseModel, now: datetime) -> None:
    values = {name: getattr(snapshot, name) for name in ("as_of", "valid_from", "expires_at")}
    for value in values.values():
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("regime snapshot time must be timezone-aware")
    if values["as_of"] > now or values["valid_from"] > now:
        raise ValueError("regime snapshot cannot be in the future")
    if not values["as_of"] <= values["valid_from"] < values["expires_at"]:
        raise ValueError("regime snapshot validity interval is inconsistent")
