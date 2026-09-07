from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BUSINESS_QUESTION_IDS = {
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
}


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_root_readme_is_a_bounded_entry_point() -> None:
    readme = _read("README.md")

    assert len(readme.splitlines()) <= 160
    assert readme.count("uv run astock") <= 18
    assert "docs/README.md" in readme
    assert "开发计划.md" in readme
    assert "进度验收.md" in readme
    assert "## 命令目录" not in readme


def test_document_governance_index_and_adr_lifecycle_are_present() -> None:
    index = _read("docs/README.md")
    adr_index = _read("docs/adr/README.md")

    assert "单一事实源优先级" in index
    assert "Markdown" in index and "机器合同" in index
    assert "0001-documentation-as-code-with-machine-contracts.md" in adr_index
    assert "0002-market-regime-as-risk-overlay.md" in adr_index
    documentation_adr = _read(
        "docs/adr/0001-documentation-as-code-with-machine-contracts.md"
    )
    regime_adr = _read("docs/adr/0002-market-regime-as-risk-overlay.md")
    assert "> 状态：ACCEPTED" in documentation_adr
    assert "> 状态：PROPOSED" in regime_adr


def test_current_plan_contains_only_the_eleven_active_work_packages() -> None:
    plan = _read("开发计划.md")
    headings = re.findall(r"^## WP-(\d{2})：", plan, flags=re.MULTILINE)

    assert headings == [f"{number:02d}" for number in range(1, 12)]
    assert "## Phase" not in plan
    assert "只保存尚未完成的工作" in plan
    assert "回滚" in plan


def test_machine_work_package_manifest_matches_the_human_plan() -> None:
    plan = _read("开发计划.md")
    manifest = yaml.safe_load(_read("planning/work_packages_v1.yaml"))
    packages = manifest["work_packages"]

    expected_ids = [f"WP-{number:02d}" for number in range(1, 12)]
    rules = manifest["rules"]
    allowed_statuses = set(rules["allowed_statuses"])
    assert manifest["schema_version"] == "project-work-package-manifest-v1"
    assert manifest["plan_id"] == "investor-orchestration-and-regime-plan-v1"
    assert f"> 版本：{manifest['plan_id']}" in plan
    assert manifest["status"] == "CURRENT"
    assert rules["completed_packages_removed_from_active_manifest"] is True
    assert rules["unique_writer_lane_required"] is True
    assert rules["dependency_ids_must_exist"] is True
    assert rules["markdown_consistency_check_required"] is True
    assert [item["id"] for item in packages] == expected_ids
    assert [item["order"] for item in packages] == list(range(1, 12))
    assert all(item["status"] in allowed_statuses for item in packages)
    assert all(item["status"] == "PLANNED" for item in packages)
    assert all(item["priority"] in {"P0", "P1"} for item in packages)
    assert all(item["owner_lane"] for item in packages)

    plan_headings = re.findall(
        r"^## (WP-\d{2})：(.+)$", plan, flags=re.MULTILINE
    )
    assert plan_headings == [(item["id"], item["title"]) for item in packages]

    owner_rows = re.findall(
        r"^\|\s*((?:WP-\d{2})(?:、WP-\d{2})*)\s*\|\s*`([^`]+)`\s*\|$",
        plan,
        flags=re.MULTILINE,
    )
    owner_by_id = {
        work_package_id: lane
        for grouped_ids, lane in owner_rows
        for work_package_id in re.findall(r"WP-\d{2}", grouped_ids)
    }
    assert owner_by_id == {
        item["id"]: item["owner_lane"] for item in packages
    }

    order_by_id = {item["id"]: item["order"] for item in packages}
    for index, item in enumerate(packages):
        section_start = plan.index(f"## {item['id']}：")
        section_end = (
            plan.index(f"## {packages[index + 1]['id']}：")
            if index + 1 < len(packages)
            else plan.index("## 4. 统一测试与证据")
        )
        status_line = next(
            line
            for line in plan[section_start:section_end].splitlines()
            if line.startswith("> 状态：")
        )
        assert f"状态：{item['status']}" in status_line
        assert f"优先级：{item['priority']}" in status_line
        assert set(re.findall(r"WP-\d{2}", status_line)) == set(item["depends_on"])
        assert all(
            dependency in order_by_id
            and order_by_id[dependency] < item["order"]
            for dependency in item["depends_on"]
        )

    assert "planning/work_packages_v1.yaml" in plan
    assert "机器索引" in _read("docs/README.md")


def test_business_question_matrix_covers_each_requested_id_once() -> None:
    matrix = _read("docs/acceptance/business-question-capability-matrix-v1.md")
    found = [
        int(value)
        for value in re.findall(r"^\|\s*(\d+)\s*\|", matrix, flags=re.MULTILINE)
    ]
    counts = Counter(found)

    assert set(found) == BUSINESS_QUESTION_IDS
    assert all(count == 1 for count in counts.values())
    assert len(found) == 68
    assert found == [
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
    assert "REQUIRED" in matrix
    assert "PROHIBITED" in matrix
    assert "空持仓" in matrix and "静默" in matrix


def test_account_fact_writes_do_not_depend_on_current_regime_and_replay_is_typed() -> None:
    architecture = _read("docs/architecture/investment-request-orchestration-v1.md")
    matrix = _read("docs/acceptance/business-question-capability-matrix-v1.md")

    assert "其缺失不得阻断 append-only 账户事实提交" in architecture
    assert "纯账户事实写入不依赖 current regime" in matrix
    assert "`PT_REPLAY`" in matrix
    order_row = next(line for line in matrix.splitlines() if line.startswith("| 96 |"))
    assert "`PT_REPLAY`" in order_row
    assert "user timezone" in architecture and "market timezone" in architecture
    assert "存在多个账户且无默认时不得猜测" in architecture
    assert "Asia/Shanghai" in architecture and "不把 date-only 伪造成具体成交时刻" in architecture


def test_regime_validation_has_observable_targets_and_a_share_pit_history() -> None:
    blueprint = _read("docs/architecture/market-regime-control-v1.md")

    assert "CORE_LONG_HISTORY" in blueprint and "ENRICHED_HISTORY" in blueprint
    assert "survivorship bias" in blueprint
    assert "事前冻结的可观察 outcome 合同" in blueprint
    assert "NOT_EVALUABLE" in blueprint
    assert "label_change_only_action_count" in blueprint
    assert "重复计数审计" in blueprint
    assert "PortfolioIntentProfile" in blueprint


def test_new_architecture_blueprints_cannot_be_misread_as_implemented() -> None:
    for relative_path in (
        "docs/architecture/investment-request-orchestration-v1.md",
        "docs/architecture/market-regime-control-v1.md",
        "docs/architecture/scheduled-research-orchestration-v1.md",
    ):
        text = _read(relative_path)
        assert "> 状态：PROPOSED" in text
        assert "是否已实现：否" in text


def test_scheduled_research_blueprint_keeps_fact_and_execution_planes_separate() -> None:
    blueprint = _read("docs/architecture/scheduled-research-orchestration-v1.md")
    plan = _read("开发计划.md")

    assert "不能替代" in blueprint and "本地 Continuous Monitor" in blueprint
    assert "Web 任务" in blueprint and "不能直接访问电脑本地目录" in blueprint
    assert "最高每小时一次" in blueprint
    assert "不假设存在亚小时定时能力" in blueprint
    assert "不把一个稳定、可由仓库代码直接调用的 Scheduled Task 公共 API 当作既有能力" in blueprint
    assert "NATIVE_UI_BOUND/OFFICIAL_API_VERIFIED" in blueprint
    assert "默认 `MINIMUM`" in blueprint
    assert "整库 SQLite" in blueprint and "次数为 0" in blueprint
    assert "云端上下文" in blueprint
    assert "分钟级检查" not in blueprint
    assert "EXISTING_CHAT" in blueprint
    assert "CHAT_LOOP" not in blueprint
    assert "经济写入均为 0" in blueprint
    assert "同一 `binding_id + schedule_bucket`" in blueprint
    assert "## WP-10：ChatGPT / Codex Scheduled Research Adapter" in plan
    assert "## WP-11：Controlled live、prospective shadow、启用与回滚" in plan


def test_historical_long_documents_are_explicitly_frozen() -> None:
    overall_design = _read("低成本A股多Agent投研系统方案.md")
    audit_report = _read(
        "docs/architecture/AStockMultiAgent系统体验与投研能力深度研究报告.md"
    )

    assert "HISTORICAL / FROZEN" in overall_design[:1000]
    assert "文档状态：HISTORICAL" in audit_report[:1000]


def test_local_markdown_links_in_new_navigation_docs_exist() -> None:
    for relative_path in ("README.md", "docs/README.md", "docs/adr/README.md"):
        path = PROJECT_ROOT / relative_path
        body = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", body):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean_target = target.split("#", maxsplit=1)[0]
            assert (path.parent / clean_target).resolve().exists(), (
                relative_path,
                target,
            )
