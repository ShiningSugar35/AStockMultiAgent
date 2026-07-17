from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_yaml(name: str) -> dict:
    return yaml.safe_load((PROJECT_ROOT / "configs" / name).read_text(encoding="utf-8"))


def test_knowledge_allowlist_is_exact_and_online_identities_are_confirmed() -> None:
    sources = load_yaml("knowledge_sources.yaml")["sources"]
    by_name = {source["display_name"]: source for source in sources}
    assert set(by_name) == {"MR Dang", "黄彦臻", "派大星皮皮", "寒武纪的鳄鱼"}

    mr_dang = by_name["MR Dang"]
    assert mr_dang["url_token"] == "mr-dang-77"
    assert mr_dang["identity_status"] == "CONFIRMED"
    assert mr_dang["enabled"] is True

    expected_tokens = {
        "黄彦臻": "huang-wei-yan-30",
        "派大星皮皮": "xiao-peng-61-47",
    }
    for display_name, token in expected_tokens.items():
        source = by_name[display_name]
        assert source["profile_url"] == f"https://www.zhihu.com/people/{token}"
        assert source["platform_user_id"] is None
        assert source["url_token"] == token
        assert source["identity_status"] == "CONFIRMED"
        assert source["access_status"] == "LOGGED_IN_ACCESS_VERIFIED"
        assert source["enabled"] is True
        scope = source["collection_scope"]
        assert scope["history_mode"] == "FULL_ACCESSIBLE_HISTORY"
        assert scope["content_types"] == ["answers", "thoughts", "articles"]
        assert scope["include_required_comment_pages"] is True
        assert scope["include_nested_replies"] is True

    hanwuji = by_name["寒武纪的鳄鱼"]
    assert hanwuji["source_id"] == "zhihu:hanwujideeyu"
    assert hanwuji["identity_status"] == "LOCAL_EXPORT_USER_CONFIRMED_COMPLETE"
    assert hanwuji["access_status"] == "LOCAL_EXPORT_PARSED_COMPLETE"
    assert hanwuji["online_collection_required"] is False
    assert hanwuji["enabled"] is True


def test_resolved_identity_tasks_leave_no_open_identity_blocker() -> None:
    sources = load_yaml("knowledge_sources.yaml")["sources"]
    pending_ids = {
        source["source_id"]
        for source in sources
        if source["identity_status"] == "PENDING_IDENTITY_CONFIRMATION"
    }
    tasks = load_yaml("manual_investigation_tasks.yaml")["tasks"]
    open_subjects = {task["subject_source_id"] for task in tasks if task["status"] == "OPEN"}
    assert open_subjects == pending_ids
    assert all(task["task_type"] == "IDENTITY_CONFIRMATION" for task in tasks)
    assert {task["status"] for task in tasks} == {"RESOLVED"}


def test_private_book_scope_is_formal_but_raw_pdf_remains_git_excluded() -> None:
    sources = load_yaml("book_sources.yaml")["sources"]
    assert len(sources) == 1
    book = sources[0]
    assert book["source_id"] == "book:mr-dang:value-investing-method"
    assert book["display_name"] == "MR Dang《价值投资功法》"
    assert book["rights_status"] == "LOCAL_PRIVATE_RESEARCH"
    assert book["raw_source_policy"] == {
        "git": "EXCLUDED",
        "external_republication": "PROHIBITED",
        "object_store": "REQUIRED",
        "immutable": True,
    }
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.pdf" in gitignore
    assert "*.docx" in gitignore


def test_private_docx_is_user_confirmed_complete_without_online_collection() -> None:
    sources = load_yaml("knowledge_sources.yaml")["sources"]
    source = next(item for item in sources if item["source_id"] == "zhihu:hanwujideeyu")
    seed = source["local_seed_sources"][0]
    assert seed["source_id"] == "zhihu-export:hanwujideeyu:articles"
    assert seed["source_type"] == "PRIVATE_DOCX_EXPORT"
    assert seed["expected_sha256"] == (
        "197ec18e6fabac4401f6412331e9aa50f919498d4e40cfddb481eeab9788852d"
    )
    assert seed["online_history_coverage"] == "USER_CONFIRMED_COMPLETE_EXPORT"
    assert source["online_collection_required"] is False
    assert source["enabled"] is True


def test_development_plan_names_every_source_and_book_artifact() -> None:
    plan = (PROJECT_ROOT / "开发计划.md").read_text(encoding="utf-8")
    for text in (
        "MR Dang",
        "黄彦臻",
        "派大星皮皮",
        "寒武纪的鳄鱼",
        "《价值投资功法》",
        "BookSourceManifest",
        "BookParseReport",
        "BookCleaningReport",
        "BookMethodCoverageReport",
        "BookViewpointCard",
        "BookSkillCandidate",
        "HumanReviewDecision",
    ):
        assert text in plan
