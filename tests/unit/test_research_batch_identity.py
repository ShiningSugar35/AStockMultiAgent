"""Legacy batch resumption preserves the one canonical request graph and budget."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.sla_runtime import CurrentResearchSlaService
from astock.schemas.research_sla import ResearchTaskCategory as Category

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 18, tzinfo=UTC)


@pytest.fixture
def environment(tmp_path):
    state = StateStore(tmp_path / "state.sqlite", ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    now = [START]
    return state, objects, now, CurrentResearchSlaService(state, objects, clock=lambda: now[0])


def specs(*, legacy=False, different=False):
    companies = ["XSHE:000001", "XSHG:600000"]
    if different:
        companies[0] = "XSHE:000002"
    return [(f"company-run-{i}" if legacy else f"company-run:{company}", company,
             Category.NETWORK, (), f"input:{company}") for i, company in enumerate(companies)]


def count(state):
    with state.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM research_scheduler_run").fetchone()[0]


def test_legacy_run_reordered_request_keeps_original_nodes_and_deadline(environment):
    state, _, now, service = environment
    old = service.create_run(request_id="caller:" + "a" * 64,
                             task_specs=specs(legacy=True), budget_seconds=30)
    old_tasks = service.tasks(old.run_id)
    now[0] += timedelta(seconds=7)
    resumed = service.create_run(request_id="batch-v2-caller", task_specs=list(reversed(specs())),
                                 budget_seconds=30, legacy_batch_caller="caller")
    assert resumed.run_id == old.run_id
    assert resumed.deadline_at == old.deadline_at
    assert service.tasks(old.run_id) == old_tasks
    assert count(state) == 1


@pytest.mark.parametrize("change", ["company", "input", "budget"])
def test_legacy_explicit_identity_mismatch_never_creates_fresh_graph(environment, change):
    state, _, _, service = environment
    service.create_run(request_id="caller:" + "a" * 64,
                       task_specs=specs(legacy=True), budget_seconds=30)
    new_specs = specs(different=change == "company")
    if change == "input":
        item = new_specs[0]
        new_specs[0] = (*item[:4], "new-as-of-or-input")
    with pytest.raises(ValueError, match="contract"):
        service.create_run(request_id="batch-v2-caller", task_specs=new_specs,
                           budget_seconds=31 if change == "budget" else 30,
                           legacy_batch_caller="caller")
    assert count(state) == 1


def test_two_legacy_forks_are_ambiguous_even_when_economically_identical(environment):
    state, _, now, service = environment
    service.create_run(request_id="caller:" + "a" * 64,
                       task_specs=specs(legacy=True), budget_seconds=30)
    now[0] += timedelta(seconds=10)
    service.create_run(request_id="caller:" + "b" * 64,
                       task_specs=specs(legacy=True), budget_seconds=30)
    with pytest.raises(ValueError, match="ambiguous"):
        service.create_run(request_id="batch-v2-caller", task_specs=specs(),
                           budget_seconds=30, legacy_batch_caller="caller")
    assert count(state) == 2


def test_legacy_prefix_is_literal_not_sql_like_pattern(environment):
    state, _, _, service = environment
    service.create_run(request_id="callerX:" + "a" * 64,
                       task_specs=specs(legacy=True), budget_seconds=30)
    run = service.create_run(request_id="batch-v2-percent", task_specs=specs(),
                             budget_seconds=30, legacy_batch_caller="caller%")
    assert run.request_id == "batch-v2-percent"
    assert count(state) == 2


def test_implicit_legacy_family_can_contain_different_requests(environment):
    state, _, _, service = environment
    service.create_run(request_id="current-research-batch:" + "a" * 64,
                       task_specs=specs(legacy=True, different=True), budget_seconds=30)
    match = service.create_run(request_id="current-research-batch:" + "b" * 64,
                               task_specs=specs(legacy=True), budget_seconds=30)
    resumed = service.create_run(request_id="batch-v2-implicit", task_specs=specs(),
                                 budget_seconds=30, legacy_batch_caller="current-research-batch",
                                 legacy_batch_explicit=False)
    assert resumed.run_id == match.run_id
    assert count(state) == 2


def test_legacy_lookup_refuses_to_flatten_real_dependency_graphs(environment):
    _, _, _, service = environment
    task_specs = [
        ("company-run-0", "XSHE:000001", Category.NETWORK, (), "input"),
        ("company-run-1", "XSHE:000001", Category.NETWORK, ("company-run-0",), "input2"),
    ]
    with pytest.raises(ValueError, match="batch"):
        service.create_run(request_id="not-a-flat-batch", task_specs=task_specs,
                           budget_seconds=30, legacy_batch_caller="caller")


def test_concurrent_same_request_creates_only_one_graph(environment):
    state, objects, _, _ = environment
    barrier = Barrier(2)

    def create():
        service = CurrentResearchSlaService(state, objects, clock=lambda: START)
        barrier.wait(timeout=10)
        return service.create_run(request_id="v2-single-flight", task_specs=specs(),
                                  budget_seconds=30, legacy_batch_caller="caller")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results[0].run_id == results[1].run_id
    assert count(state) == 1


@pytest.mark.parametrize("invalid", [True, 30.5, "30"])
def test_budget_must_be_integer_without_coercion(environment, invalid):
    _, _, _, service = environment
    with pytest.raises(ValueError, match="budget"):
        service.create_run(request_id="strict-budget", task_specs=specs(),
                           budget_seconds=cast(int, invalid))
