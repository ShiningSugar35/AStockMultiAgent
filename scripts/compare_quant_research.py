"""Reproducible offline method comparison; optional packages never enter production.

Run export/report with the project's Python and each external part with the
isolated comparison Python. All data are synthetic. This script has no account,
order, network-data, or production-admission capability.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import sys
import warnings
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".scratchpad" / "quant-comparison-v1"
AUDIT = ROOT / "third_party" / "audits" / "quant-method-comparison-v1.json"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
    os.environ[_name] = "1"
os.environ["NUMBA_CACHE_DIR"] = str(WORK / "numba-cache")
os.environ["MPLCONFIGDIR"] = str(WORK / "matplotlib-cache")
RSS_REVIEW_THRESHOLD = 1_073_741_824
SOURCE_PATHS = (
    "scripts/compare_quant_research.py",
    "src/astock/experiments/cross_validation.py",
    "src/astock/experiments/market_evaluator.py",
    "src/astock/experiments/validation.py",
    "src/astock/shadow/statistics.py",
    "src/astock/portfolio/allocation.py",
    "src/astock/portfolio/allocators.py",
    "src/astock/portfolio/analytics.py",
    "tests/unit/test_strategy_validation.py",
)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def write(name, value):
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / name).write_bytes(encode(value))


def read(name):
    return json.loads((WORK / name).read_bytes())


def source_hashes():
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SOURCE_PATHS}


def environment():
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        record = distribution.read_text("RECORD")
        packages[name] = {
            "version": distribution.version,
            "installed_record_sha256": (
                hashlib.sha256(record.encode()).hexdigest() if record else None
            ),
        }
    return {"python": sys.version, "packages": dict(sorted(packages.items()))}


def peak_rss():
    if sys.platform != "win32":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong,
    ]
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(
        process, ctypes.byref(counters), ctypes.sizeof(counters)
    ):
        raise RuntimeError("could not observe process peak working set")
    return int(counters.PeakWorkingSetSize)


def resources(start):
    rss = peak_rss()
    return {
        "elapsed_seconds": perf_counter() - start, "peak_working_set_bytes": rss,
        "rss_review_threshold_bytes": RSS_REVIEW_THRESHOLD,
        "within_observed_rss_threshold": rss <= RSS_REVIEW_THRESHOLD,
        "hard_rss_limit_applied": False, "threads_requested": 1,
    }


def allocation_context(case):
    import numpy as np

    from astock.portfolio.allocation import AllocationContext
    from astock.portfolio.analytics import AlignedPortfolioData

    matrix = np.asarray(case["train_returns"])
    return AllocationContext(
        aligned=AlignedPortfolioData(
            company_ids=case["company_ids"], asset_returns=matrix,
            benchmark_returns=np.zeros(len(matrix)), common_session_count=len(matrix),
            covariance=np.asarray(case["covariance"]), correlation=np.asarray(case["correlation"]),
        ),
        current_weights=tuple(case["current_weights"]),
        groups=tuple(tuple(values) for values in case["groups"]), group_caps=case["group_caps"],
        target_exposure=case["target_exposure"], max_single=case["max_single"],
        can_buy=tuple(case["can_buy"]), can_sell=tuple(case["can_sell"]),
        one_way_costs=tuple(case["one_way_costs"]), min_cash=case["min_cash"],
        max_turnover=case["max_turnover"], return_basis="PRICE_ONLY",
    )


def portfolio_cases():
    import numpy as np
    from sklearn.covariance import LedoitWolf

    rng = np.random.default_rng(20261121)
    shocks = rng.normal(size=(256, 9))
    normal = np.column_stack([
        0.0002 + shocks[:, 0] * 0.004 + shocks[:, 1 + index // 3] * 0.003
        + shocks[:, 3 + index] * (0.005 + index * 0.001)
        for index in range(6)
    ])
    collinear = normal.copy()
    collinear[:, 1] = collinear[:, 0] + shocks[:, 7] * 0.0000001
    tails = normal.copy()
    tails[[17, 49, 89, 151, 221], :3] -= 0.055
    analytic = np.tile([[0.01, 0.03], [-0.01, -0.03],
                        [0.01, -0.03], [-0.01, 0.03]], (40, 1))
    matrices = {"analytic_two": analytic, "normal": normal, "near_collinear": collinear,
                "left_tail": tails, "normal_constraints": normal}
    cases = []
    for name, matrix in matrices.items():
        train_count = 128 if matrix.shape[1] == 2 else 192
        train, holdout = matrix[:train_count], matrix[train_count:]
        n = train.shape[1]
        case = {
            "name": name, "company_ids": [f"SYNTHETIC:{index}" for index in range(n)],
            "train_returns": train.tolist(), "synthetic_holdout_returns": holdout.tolist(),
            "covariance": LedoitWolf().fit(train).covariance_.tolist(),
            "correlation": np.corrcoef(train, rowvar=False).tolist(),
            "current_weights": [0.0] * n, "groups": [["all"]] * n, "group_caps": {"all": 1.0},
            "target_exposure": 1.0, "max_single": 1.0, "can_buy": [True] * n,
            "can_sell": [True] * n, "one_way_costs": [0.0] * n,
            "min_cash": 0.0, "max_turnover": None,
        }
        if name.endswith("constraints"):
            case.update({
                "current_weights": [0.18, 0.12, 0.15, 0.10, 0.10, 0.15],
                "groups": [["sector_a", "theme"], ["sector_a"], ["sector_a"],
                           ["sector_b", "theme"], ["sector_b"], ["sector_b"]],
                "group_caps": {"sector_a": 0.45, "sector_b": 0.45, "theme": 0.35},
                "target_exposure": 0.8, "max_single": 0.3,
                "can_buy": [False, True, True, True, True, True],
                "can_sell": [True, False, True, True, True, True],
                "one_way_costs": [0.001] * n, "min_cash": 0.15, "max_turnover": 0.3,
            })
        cases.append(case)
    return cases


def cpcv_inputs():
    from astock.experiments.cross_validation import CPCVObservation, CPCVRequest

    start = datetime(2024, 1, 1, 1, tzinfo=UTC)
    rows = tuple(CPCVObservation(
        instrument_id=f"SYNTHETIC:{asset}", prediction_at=start + timedelta(days=day),
        feature_available_at=start + timedelta(days=day, hours=-1),
        label_end_at=start + timedelta(days=day, hours=1),
        label_available_at=start + timedelta(days=day, hours=2),
        responses={"a": 0.02 if day % 3 else -0.01, "b": 0.01 if day % 4 else -0.03},
    ) for day in range(16) for asset in range(3))
    base = CPCVRequest(
        as_of=start + timedelta(days=40), candidate_ids=("a", "b"), observations=rows,
        source_references=("SYNTHETIC_FIXED_RESPONSE_PANEL_NO_MARKET_AUTHORITY",),
    )
    changed = list(rows)
    changed[10] = changed[10].model_copy(update={
        "label_end_at": rows[18].prediction_at,
        "label_available_at": rows[18].label_available_at,
    })
    variable = base.model_copy(update={"observations": tuple(changed), "embargo_seconds": 86400})
    return {
        "regular": base.model_dump(mode="json"),
        "variable_grouped": variable.model_dump(mode="json"),
    }


def canonical_numeric_reference():
    """Use the existing actual evaluator on an explicitly synthetic, zero-cost train window."""
    import numpy as np

    from astock.core.object_store import ObjectStore
    from astock.core.state import StateStore
    from astock.experiments.market_evaluator import CanonicalMarketEvaluator
    from astock.experiments.validation import Budget, Candidate, make_plan
    from astock.experiments.validation import digest as model_digest
    from astock.investor_orchestration.store import InvestorOrchestrationStore
    from tests.unit.test_strategy_validation import (
        canonical_fixture,
        canonical_query,
        register_canonical,
    )

    release, old_plan = canonical_fixture()
    release = release.model_copy(update={
        "fees": (release.fees[0].model_copy(update={
            "commission_rate": Decimal(0), "minimum_commission_fen": 0,
            "stamp_tax_sell_rate": Decimal(0), "transfer_fee_rate": Decimal(0),
        }),), "slippage_bps": Decimal(0),
    })
    candidates = tuple(Candidate(
        candidate_id=f"lookback-{lookback}-shares-{shares}", strategy_version="lagged-momentum-v1",
        parameters={"lookback": lookback, "shares": shares},
    ) for lookback in (1, 2, 3) for shares in (100, 200))
    plan = make_plan(old_plan.request.model_copy(update={
        "candidates": candidates, "input_release_hash": model_digest(release),
    }))
    location = WORK / "canonical-numeric"
    location.mkdir(parents=True, exist_ok=True)
    metadata = InvestorOrchestrationStore(location / "state.sqlite")
    metadata.initialize()
    state, objects = StateStore(metadata.path), ObjectStore(location / "objects")
    artifact_id = register_canonical(state, objects, release)
    evaluator = CanonicalMarketEvaluator(state, objects, artifact_id, plan)
    closes = [float(session.bar.close) for session in release.sessions[:plan.folds[0].train.stop]]
    results = {}
    for candidate in candidates:
        evidence = evaluator.evaluate(canonical_query(plan, candidate=candidate), Budget(5))
        values = np.cumprod([1 + float(row.gross_return - row.fee_return - row.slippage_return)
                            for row in evidence.rows]) * (release.initial_cash_fen / 100)
        results[candidate.candidate_id] = {
            "parameters": candidate.parameters, "value": values.tolist(),
            "evidence_hash": model_digest(evidence),
            "execution_artifact_ids": list(evidence.execution_artifact_ids),
        }
    return {
        "basis": "SYNTHETIC_TRAIN_WINDOW_ZERO_FEES_SLIPPAGE_NO_A_SHARE_EQUIVALENCE",
        "closes": closes, "initial_cash": release.initial_cash_fen / 100,
        "results": results, "release": release.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
    }


def export():
    import numpy as np

    from astock.experiments.cross_validation import CPCVRequest, build_grouped_cpcv_report
    from astock.portfolio.allocators import default_portfolio_allocator_registry
    from astock.schemas.portfolio import PortfolioAllocationMethod
    from astock.shadow.statistics import (
        cscv_probability_of_backtest_overfitting,
        deflated_sharpe_probability,
        expected_maximum_sharpe,
        sample_sharpe,
    )

    rng = np.random.default_rng(630076)
    panel = rng.normal(loc=[0.001, 0.0003, -0.0002, 0.0007], scale=0.01, size=(128, 4))
    sharpes = [sample_sharpe(column.tolist()) for column in panel.T]
    winner = int(np.argmax(sharpes))
    tied = [[3.0] * 4 + [0.0] * 4, [0.0] * 4 + [3.0] * 4]
    cases = portfolio_cases()
    cpcv = cpcv_inputs()
    data = {
        "schema_version": "quant-method-comparison-input-v1",
        "data_basis": "SYNTHETIC_NOT_HISTORICAL_PIT_OR_MARKET_EFFECTIVENESS",
        "portfolio_cases": cases, "trial_returns_observations_by_candidate": panel.tolist(),
        "trial_sharpes": sharpes, "selected_candidate_index": winner, "tied_cscv_panel": tied,
        "cpcv": cpcv, "canonical_numeric": canonical_numeric_reference(),
    }
    registry = default_portfolio_allocator_registry()
    local = {
        "input_hash": digest(data), "source_hashes": source_hashes(), "environment": environment(),
        "allocations": {
            case["name"]: {
                method.value: asdict(registry.allocate(method, allocation_context(case)))
                for method in PortfolioAllocationMethod
            } for case in cases
        },
        "dsr": deflated_sharpe_probability(
            panel[:, winner].tolist(), selection_candidate_count=4,
            trial_sharpes=sharpes, effective_trial_count=4.0,
        ),
        "expected_max_sharpe": expected_maximum_sharpe(sharpes, effective_trial_count=4.0),
        "paper_expected_max_z_100": expected_maximum_sharpe(
            [(-1.0) ** i * np.sqrt(0.99) for i in range(100)], effective_trial_count=100.0,
        ),
        "cscv_no_ties": cscv_probability_of_backtest_overfitting(
            panel.T.tolist(), block_count=4, metric="MEAN",
        ),
        "cscv_neutral_ties": cscv_probability_of_backtest_overfitting(
            tied, block_count=4, metric="MEAN",
        ),
        "cpcv": {
            name: build_grouped_cpcv_report(CPCVRequest.model_validate(request))
            for name, request in cpcv.items()
        },
    }
    write("inputs.json", data)
    write("local.json", local)
    return {"input_hash": local["input_hash"], "local_source_hashes": local["source_hashes"]}

def solver_constraints(case):
    import cvxpy as cp
    import numpy as np

    held, costs = np.asarray(case["current_weights"]), np.asarray(case["one_way_costs"])
    min_cash, turnover = case["min_cash"], case["max_turnover"]

    def constraints(weights):
        values = [cp.sum(weights) + costs @ cp.abs(weights - held) <= 1 - min_cash]
        if turnover is not None:
            values.append(cp.norm1(weights - held) <= turnover)
        return values

    return constraints


def external_portfolio(data):
    import cvxpy as cp
    import numpy as np
    from skfolio import RiskMeasure
    from skfolio.cluster import HierarchicalClustering, LinkageMethod
    from skfolio.distance import PearsonDistance
    from skfolio.moments import LedoitWolf
    from skfolio.optimization import HierarchicalRiskParity, MeanRisk
    from skfolio.prior import EmpiricalPrior
    from skfolio.seriation import HierarchicalSeriation

    results = {}
    for case in data["portfolio_cases"]:
        matrix = np.asarray(case["train_returns"])
        held = np.asarray(case["current_weights"])
        lower = np.where(case["can_sell"], 0.0, held)
        upper = np.where(case["can_buy"], case["max_single"],
                         np.minimum(held, case["max_single"]))
        prior = EmpiricalPrior(covariance_estimator=LedoitWolf(nearest=False))
        covariance = LedoitWolf(nearest=False).fit(matrix).covariance_
        covariance_error = float(np.max(np.abs(covariance - np.asarray(case["covariance"]))))
        if covariance_error > 1e-12:
            raise ValueError("comparison covariance differs from the frozen local estimator")
        bounds = np.asarray([
            [float(group in memberships) for memberships in case["groups"]]
            for group in case["group_caps"]
        ])
        caps = np.asarray(list(case["group_caps"].values()))

        additional = solver_constraints(case)

        models = {
            "SKFOLIO_HRP_SINGLE": HierarchicalRiskParity(
                risk_measure=RiskMeasure.VARIANCE, prior_estimator=prior,
                distance_estimator=PearsonDistance(), distance_from_prior=False,
                seriation_estimator=HierarchicalSeriation(
                    hierarchical_clustering_estimator=HierarchicalClustering(
                        linkage_method=LinkageMethod.SINGLE,
                    ), optimal_ordering=False,
                ),
            ),
        }
        for name, risk in (("SKFOLIO_MIN_VARIANCE", RiskMeasure.VARIANCE),
                           ("SKFOLIO_CVAR_95", RiskMeasure.CVAR)):
            models[name] = MeanRisk(
                risk_measure=risk, prior_estimator=prior,
                min_weights=lower, max_weights=upper, budget=case["target_exposure"],
                transaction_costs=0.0, left_inequality=bounds, right_inequality=caps,
                add_constraints=additional, cvar_beta=0.95, solver="CLARABEL",
                save_problem=True,
                solver_params={"max_threads": 1, "time_limit": 2.0, "max_iter": 200,
                               "tol_gap_abs": 1e-10, "tol_feas": 1e-10},
            )
        results[case["name"]] = {}
        for name, model in models.items():
            start = perf_counter()
            try:
                with warnings.catch_warnings(record=True) as observed_warnings:
                    warnings.simplefilter("always")
                    model.fit(matrix)
                weights = np.asarray(model.weights_)
                if weights.shape != held.shape or not np.isfinite(weights).all():
                    raise ValueError("non-finite or misaligned native weights")
                results[case["name"]][name] = {
                    "status": "COMPUTED", "weights": weights.tolist(),
                    "native_solver_status": getattr(
                        getattr(model, "problem_", None), "status", None,
                    ),
                    "native_warnings": [str(item.message) for item in observed_warnings],
                    "elapsed_seconds": perf_counter() - start,
                    "covariance_max_abs_difference": covariance_error,
                    "native_constraint_scope": "WEIGHT_BOUNDS_ONLY_BEFORE_SHARED_PROJECTION"
                    if "HRP" in name else "EXACT_EXPOSURE_GROUPS_ELIGIBILITY_L1_COST_CASH",
                }
            except (ValueError, RuntimeError, cp.error.SolverError) as exc:
                results[case["name"]][name] = {
                    "status": "UNAVAILABLE", "error_type": type(exc).__name__,
                    "reason": str(exc)[:1000], "elapsed_seconds": perf_counter() - start,
                }
    return results


def external_vectorbt(data):
    import numpy as np
    import pandas as pd
    import vectorbt as vbt

    reference = data["canonical_numeric"]
    closes = np.asarray(reference["closes"])
    results = {}
    for name, result in reference["results"].items():
        lookback = result["parameters"]["lookback"]
        size = result["parameters"]["shares"]
        entries = np.asarray([
            index > lookback and closes[index - 1] > closes[index - 1 - lookback]
            for index in range(len(closes))
        ])
        start = perf_counter()
        portfolio = vbt.Portfolio.from_signals(
            pd.Series(closes), entries=entries, exits=~entries, size=size,
            init_cash=reference["initial_cash"], direction="longonly",
            fees=0.0, fixed_fees=0.0, slippage=0.0, accumulate=False, freq="1D",
        )
        values = np.asarray(portfolio.value())
        results[name] = {
            "value": values.tolist(), "elapsed_seconds": perf_counter() - start,
            "max_abs_value_difference": float(np.max(np.abs(values - np.asarray(result["value"])))),
            "within_absolute_tolerance_1e_8": bool(np.allclose(
                values, result["value"], atol=1e-8, rtol=0.0,
            )),
        }
    return results


def cvar95(matrix, weights):
    import numpy as np
    losses = -np.asarray(matrix) @ np.asarray(weights)
    index = max(0, int(np.ceil(0.95 * len(losses))) - 1)
    threshold = np.sort(losses)[index]
    return float(threshold + np.maximum(losses - threshold, 0).sum() / (0.05 * len(losses)))


def allocation_report(case, result):
    import numpy as np

    from astock.portfolio.allocation import AllocationResult, revalidate_implementation

    context = allocation_context(case)
    allocation = AllocationResult(**result)
    if not allocation.weights:
        return {"allocation": result, "account_implementation_claim": False}
    weights = np.asarray(allocation.weights)
    covariance = np.asarray(case["covariance"])
    holdout = np.asarray(case["synthetic_holdout_returns"])
    price_relatives = np.cumprod(1 + holdout, axis=0)
    cost = allocation.one_off_cost_fraction
    gross = 1 - weights.sum() + price_relatives @ weights
    net = gross - cost
    lower, _ = context.bounds()
    rounded = tuple(np.maximum(lower, np.floor(weights / 0.005 + 1e-10) * 0.005).tolist())
    checked = revalidate_implementation(
        context, allocation, rounded, observed_current_weights=context.current_weights,
    )
    return {
        "allocation": result,
        "same_exposure_as_solver_budget": bool(
            abs(weights.sum() - context.target_exposure) <= 1e-8
        ),
        "training_variance": float(weights @ covariance @ weights),
        "training_empirical_cvar95": cvar95(case["train_returns"], weights),
        "synthetic_holdout_gross_buy_hold_total_return": float(gross[-1] - 1),
        "synthetic_holdout_net_buy_hold_total_return": float(net[-1] - 1),
        "synthetic_holdout_cost_debit_once": cost,
        "projection_stress": {
            "basis": "NUMERICAL_0_5_PERCENT_WEIGHT_GRID_NOT_ACTUAL_ACCOUNT_OR_LOTS",
            "checked_result": asdict(checked),
            "post_projection_variance": float(np.asarray(rounded) @ covariance @ rounded),
            "post_projection_empirical_cvar95": cvar95(case["train_returns"], rounded),
        },
        "account_implementation_claim": False,
    }


def plain(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def native_cpcv(data, local):
    import numpy as np
    import pandas as pd
    from ml4t.diagnostic.splitters import CombinatorialCV
    from purgedcv import CombinatorialPurgedCV

    result = {}
    for name, request in data["cpcv"].items():
        dates = local["cpcv"][name]["date_groups"]
        starts = pd.to_datetime([group["prediction_at"] for group in dates])
        closed_ends = pd.to_datetime([group["label_end_at"] for group in dates])
        ends = closed_ends + pd.Timedelta(1, unit="ns")
        cv = CombinatorialPurgedCV(
            n_splits=request["n_groups"], n_test_groups=request["n_test_groups"],
            prediction_times=starts, evaluation_times=ends,
            purge_horizon=pd.Timedelta(seconds=request["purge_seconds"]),
            embargo=pd.Timedelta(seconds=request["embargo_seconds"]) or None,
        )
        native = []
        for train, test in cv.split(np.zeros((len(dates), 1))):
            native.append({
                "train_indices": sorted(row for i in train for row in dates[i]["row_indices"]),
                "test_indices": sorted(row for i in test for row in dates[i]["row_indices"]),
            })
        local_pairs = [
            {key: fold[key] for key in ("train_indices", "test_indices")}
            for fold in local["cpcv"][name]["folds"]
        ]
        horizon = max(closed_ends - starts)
        fixed = CombinatorialCV(
            n_groups=request["n_groups"], n_test_groups=request["n_test_groups"],
            label_horizon=horizon, embargo_size=pd.Timedelta(
                seconds=request["embargo_seconds"],
            ) or None,
            max_combinations=request["max_combinations"], calendar=None, isolate_groups=False,
        )
        frame = pd.DataFrame({"feature": np.arange(len(dates))}, index=starts)
        ml4t = [{
            "train_indices": sorted(row for i in train for row in dates[i]["row_indices"]),
            "test_indices": sorted(row for i in test for row in dates[i]["row_indices"]),
        } for train, test in fixed.split(frame)]
        result[name] = {
            "purgedcv_pairs": native, "purgedcv_matches_public_local_report": native == local_pairs,
            "interval_conversion": "LOCAL_CLOSED_END_TO_PURGEDCV_EXCLUSIVE_END_PLUS_1_NS",
            "ml4t_pairs": ml4t, "ml4t_matches_public_local_report": ml4t == local_pairs,
            "ml4t_horizon": str(horizon),
            "ml4t_contract": "FIXED_MAX_HORIZON_CONSERVATIVE_NOT_ROW_SPECIFIC_INTERVALS",
        }
    return result


def external_statistics(data, local):
    from itertools import combinations

    import numpy as np
    import purgedcv
    from ml4t.diagnostic.evaluation.stats import (
        compute_pbo,
        deflated_sharpe_ratio,
        deflated_sharpe_ratio_from_statistics,
    )

    panel = np.asarray(data["trial_returns_observations_by_candidate"])
    chosen = panel[:, data["selected_candidate_index"]]
    centered = chosen - chosen.mean()
    m2 = np.mean(centered**2)
    skew = float(np.mean(centered**3) / m2**1.5)
    kurt = float(np.mean(centered**4) / m2**2)
    variance = float(np.var(data["trial_sharpes"], ddof=1))
    native_variance = float(np.var(panel.mean(axis=0) / panel.std(axis=0, ddof=0), ddof=1))
    purged = purgedcv.deflated_sharpe_ratio_full(chosen, 4, native_variance)
    ml4t_fixed = deflated_sharpe_ratio_from_statistics(
        observed_sharpe=data["trial_sharpes"][data["selected_candidate_index"]],
        n_samples=len(chosen), n_trials=4, variance_trials=variance,
        skewness=skew, excess_kurtosis=kurt - 3, autocorrelation=0.0,
        frequency="daily", periods_per_year=252,
    )
    ml4t_raw = deflated_sharpe_ratio(
        panel, frequency="daily", periods_per_year=252, correlation_method=None,
    )
    pbo = {}
    for name, matrix in (
        ("no_ties", panel.T), ("neutral_ties", np.asarray(data["tied_cscv_panel"]))
    ):
        native = purgedcv.probability_of_backtest_overfitting(matrix, n_splits=4, metric=np.mean)
        blocks = np.arange(matrix.shape[1]).reshape(4, -1)
        pairs = []
        for selected in combinations(range(4), 2):
            other = [i for i in range(4) if i not in selected]
            pairs.append((matrix[:, blocks[list(selected)].ravel()].mean(axis=1),
                          matrix[:, blocks[other].ravel()].mean(axis=1)))
        ml4t = compute_pbo(np.asarray([a for a, _ in pairs]), np.asarray([b for _, b in pairs]))
        pbo[name] = {"purgedcv": plain(asdict(native)), "ml4t": plain(asdict(ml4t))}
    return {
        "purgedcv_dsr": asdict(purged),
        "purgedcv_moment_convention": "STD_DDOF_0_UNBIASED_SKEW_PEARSON_KURTOSIS",
        "ml4t_controlled_statistics_dsr": asdict(ml4t_fixed),
        "ml4t_raw_returns_dsr": asdict(ml4t_raw),
        "ml4t_variance_convention": "NULL_THRESHOLD_SR_AND_TRIAL_VARIANCE_RESCALING",
        "local_moment_convention": "STD_DDOF_1_POPULATION_CENTRAL_MOMENTS_OBSERVED_SR",
        "paper_expected_max_z_100": (
            purgedcv.deflated_sharpe_ratio_full(chosen, 100, 1).expected_max_z
        ),
        "pbo": pbo, "cpcv": native_cpcv(data, local),
    }


def experiment_receipts(run_ids):
    """Preserve actual owned-worker outcomes without calling an experiment a quality pass."""
    receipts = []
    for run_id in run_ids:
        if len(run_id) != 25 or run_id[8] != "T" or run_id[15:17] != "Z-":
            raise ValueError("an exact project quality-run id is required")
        if not all(character.isalnum() or character == "-" for character in run_id):
            raise ValueError("experiment run id must not contain path components")
        location = ROOT / ".ai-bridge" / "quality-runs" / run_id
        result_bytes = (location / "result.json").read_bytes()
        result = json.loads(result_bytes)
        output = (location / "output.log").read_bytes()
        output_hash = hashlib.sha256(output).hexdigest()
        if result["run_id"] != run_id or result["output_sha256"] != output_hash:
            raise ValueError("experiment receipt/output integrity mismatch")
        if not result.get("owned_worker_reaped") or result.get("temporary_cleanup") != "REMOVED":
            raise ValueError("experiment worker is not reaped and terminal")
        summaries = []
        for line in output.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "input_hash" in value:
                summaries.append(value)
        receipts.append({
            "result": result, "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "stage_summaries": summaries, "formal_quality_pass_claim": False,
        })
    return receipts


def report(run_ids):
    import numpy as np

    from astock.portfolio.allocation import project_scores, validate_allocation

    data, local = read("inputs.json"), read("local.json")
    external = {name: read(name + ".json") for name in ("portfolio", "statistics", "vectorbt")}
    if local["source_hashes"] != source_hashes() or any(
        item["input_hash"] != digest(data) for item in external.values()
    ):
        raise ValueError("comparison sources or frozen inputs changed between stages")
    allocations = {}
    for case in data["portfolio_cases"]:
        name = case["name"]
        allocations[name] = {
            method: allocation_report(case, result)
            for method, result in local["allocations"][name].items()
        }
        for method, native in external["portfolio"]["results"][name].items():
            if native["status"] != "COMPUTED":
                allocations[name][method] = {"native": native}
                continue
            context = allocation_context(case)
            weights = np.asarray(native["weights"])
            if "HRP" in method:
                result = project_scores(context, weights, method=method)
            else:
                objective = float(weights @ context.aligned.covariance @ weights) if (
                    "VARIANCE" in method
                ) else cvar95(case["train_returns"], weights)
                result = validate_allocation(
                    context, weights, method=method, status="SOLVED_OFFLINE_SYNTHETIC",
                    optimality_claim=native["native_solver_status"] == "optimal",
                    objective_value=objective,
                )
            allocations[name][method] = {
                **allocation_report(case, asdict(result)), "native": native,
            }
    stats = external["statistics"]["results"]
    audit = {
        "schema_version": "quant-method-comparison-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "OFFLINE_SYNTHETIC_SOFTWARE_METHOD_COMPARISON",
        "market_effectiveness_claim": False, "historical_pit_claim": False,
        "real_account_or_trading_actions": False,
        "input_hash": digest(data), "frozen_inputs": data,
        "source_hashes": source_hashes(), "local": local, "external": external,
        "allocations": allocations,
        "numerical_checks": {
            "paper_expected_max_z_difference": abs(
                local["paper_expected_max_z_100"] - stats["paper_expected_max_z_100"]
            ),
            "cpcv_exact_interval_parity": all(
                result["purgedcv_matches_public_local_report"] for result in stats["cpcv"].values()
            ),
            "vectorbt_zero_cost_numeric_parity": all(
                result["within_absolute_tolerance_1e_8"]
                for result in external["vectorbt"]["results"].values()
            ),
        },
        "dependency_lock_sha256": hashlib.sha256(
            (WORK / "requirements.lock").read_bytes()
        ).hexdigest(),
        "installation": {
            "requirements": (WORK / "requirements.in").read_text(encoding="utf-8"),
            "primary_environment_modified": False,
            "reproduction_requirements": [
                f"{name}=={record['version']}"
                for name, record in external["portfolio"]["environment"]["packages"].items()
            ],
        },
        "experiment_runs": experiment_receipts(run_ids),
        "references": [
            "https://pypi.org/project/skfolio/1.14.4/",
            "https://skfolio.org/generated/skfolio.optimization.MeanRisk.html",
            "https://skfolio.org/generated/skfolio.optimization.HierarchicalRiskParity.html",
            "https://skfolio.org/generated/skfolio.seriation.HierarchicalSeriation.html",
            "https://clarabel.org/stable/api_settings/",
            "https://pypi.org/project/purgedcv/1.0.0/",
            "https://eslazarev.github.io/purged-cross-validation/api/splitters/",
            "https://eslazarev.github.io/purged-cross-validation/api/metrics/",
            "https://eslazarev.github.io/purged-cross-validation/api/overfitting/",
            "https://pypi.org/project/ml4t-diagnostic/0.2.1/",
            "https://github.com/ml4t/diagnostic",
            "https://pypi.org/project/vectorbt/1.2.1/",
            "https://vectorbt.dev/api/portfolio/base/",
        ],
        "decision": {
            "status": "NOT_ADOPTED_IN_PRODUCTION_AFTER_MEASURED_COMPARISON",
            "production_dependencies_added": [],
            "production_allocator_adapter_added": False,
            "skfolio": [
                "Four equal-exposure unencumbered minimum-variance cases reproduce the local "
                "variance objective within 1e-12 absolute; no replacement is needed "
                "for that scope.",
                "Single-link HRP differs from local average-link recursion; neither is declared "
                "superior from these synthetic paths.",
                "CVaR improves its own synthetic empirical tail-loss objective, but changes "
                "the objective and can lose feasibility after the shared weight-grid projection.",
                "In the constrained case local minimum-variance projection has exposure "
                "0.73736537129714 versus solver 0.8; lower variance is not an equal-budget win.",
                "Retain current deterministic allocators and shared exits. Reconsider a bounded "
                "optional CVaR consumer only with a qualified portfolio use case, feasible actual "
                "implementation and frozen real validation; this is no market-superiority verdict.",
            ],
            "cross_validation_and_statistics": [
                "Use the public local bounded grouped-label CPCV diagnostic: purgedcv reproduces "
                "both exact interval cases after explicit closed-to-half-open conversion.",
                "Do not add runtime libraries for already covered splitting/statistics. Fixed "
                "maximum label horizons overpurge variable labels; DSR variance/moment and "
                "PBO tie conventions differ and remain explicit instead of silently "
                "replacing them.",
            ],
            "vectorbt": [
                "All six zero-cost canonical numeric parameter cases match exactly. Keep this "
                "external comparison reproducible but omit a production dependency: the narrow "
                "numeric equivalence does not establish A-share fees, suspension, limits or T+1.",
            ],
            "continued_local_authority": [
                "CANONICAL_MARKET_EXECUTOR_AND_SELECTION_SEAL",
                "SHARED_ALLOCATION_CONSTRAINT_AND_IMPLEMENTATION_EXIT",
                "EXPLICIT_LOCAL_DSR_MOMENTS_AND_NEUTRAL_TIE_CSCV",
                "PUBLIC_BOUNDED_GROUPED_LABEL_CPCV_REPORT",
            ],
        },
        "limits": [
            "Synthetic held-out periods establish no real market effectiveness.",
            "CPCV reports fixed-trial response selection, not new model fitting or deployment.",
            "Library-specific DSR and tie conventions are reported without forced equality.",
            "HRP uses single-link seriation; the local baseline uses average-tree recursion.",
            "Projected HRP and lot-grid stress have no risk-optimality guarantee.",
            "The weight grid is a numerical stress, not an account lot/fee/T+1 execution claim.",
            "RSS is observed against a review threshold; no hard RSS cap was applied.",
        ],
    }
    audit = plain(audit)
    audit["content_sha256"] = digest(audit)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_bytes(encode(audit))
    return {"audit": str(AUDIT.relative_to(ROOT)), "content_sha256": audit["content_sha256"],
            "checks": audit["numerical_checks"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("export", "external", "report"))
    parser.add_argument("--part", choices=("portfolio", "statistics", "vectorbt"))
    parser.add_argument("--run-id", action="append", default=[], help="Terminal experiment receipt")
    args = parser.parse_args()
    start = perf_counter()
    if args.stage == "export":
        result = export()
        local = read("local.json")
        local["resources"] = resources(start)
        write("local.json", local)
    elif args.stage == "external":
        if args.part is None:
            parser.error("--part is required for external comparisons")
        data, local = read("inputs.json"), read("local.json")
        functions = {
            "portfolio": lambda: external_portfolio(data),
            "statistics": lambda: external_statistics(data, local),
            "vectorbt": lambda: external_vectorbt(data),
        }
        values = functions[args.part]()
        result = {
            "input_hash": digest(data), "environment": environment(),
            "resources": resources(start), "results": plain(values),
        }
        write(args.part + ".json", result)
        result = {"part": args.part, "input_hash": result["input_hash"],
                  "resources": result["resources"]}
    else:
        result = report(args.run_id)
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
