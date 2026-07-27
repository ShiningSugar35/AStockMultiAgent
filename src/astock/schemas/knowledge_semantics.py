"""Argument-aware semantic filtering and offline distillation contracts."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.books import (
    BookApprovalStatus,
    BookEvaluationStatus,
    BookMethodCategory,
    HumanReviewStatus,
)


class RhetoricalRole(StrEnum):
    TITLE = "TITLE"
    BACKGROUND = "BACKGROUND"
    MARKET_OBSERVATION = "MARKET_OBSERVATION"
    QUESTION = "QUESTION"
    CLAIM = "CLAIM"
    EXPLANATION = "EXPLANATION"
    CAUSAL_REASON = "CAUSAL_REASON"
    EVIDENCE = "EVIDENCE"
    EXAMPLE = "EXAMPLE"
    COUNTERARGUMENT = "COUNTERARGUMENT"
    CONCLUSION = "CONCLUSION"
    OPERATIONAL_RULE = "OPERATIONAL_RULE"
    RISK = "RISK"
    TRANSITION = "TRANSITION"
    MARKETING = "MARKETING"
    CASUAL_CHAT = "CASUAL_CHAT"


class ArgumentRelationType(StrEnum):
    QUESTION_ANSWER = "QUESTION_ANSWER"
    CLAIM_EVIDENCE = "CLAIM_EVIDENCE"
    CLAIM_EXPLANATION = "CLAIM_EXPLANATION"
    EXAMPLE_OF = "EXAMPLE_OF"
    COUNTER_TO = "COUNTER_TO"
    CONCLUSION_OF = "CONCLUSION_OF"
    CONTINUATION = "CONTINUATION"


class ParagraphMergeAction(StrEnum):
    KEEP_AS_ARGUMENT = "KEEP_AS_ARGUMENT"
    MERGE_WITH_PREVIOUS = "MERGE_WITH_PREVIOUS"
    MERGE_WITH_FOLLOWING = "MERGE_WITH_FOLLOWING"
    MERGE_WITH_BOTH = "MERGE_WITH_BOTH"
    DERIVED_EXCLUDE = "DERIVED_EXCLUDE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ParagraphUnitKind(StrEnum):
    TEXT = "TEXT"
    VISUAL_EVIDENCE = "VISUAL_EVIDENCE"


class KeywordScreenDecision(StrEnum):
    CANDIDATE = "CANDIDATE"
    EXCLUDED_DERIVED = "EXCLUDED_DERIVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ArgumentUnitStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    DERIVED_EXCLUDED = "DERIVED_EXCLUDED"


class SemanticRunStage(StrEnum):
    PLANNED = "PLANNED"
    INPUT_FROZEN = "INPUT_FROZEN"
    PARAGRAPHIZED = "PARAGRAPHIZED"
    KEYWORD_SCREENED = "KEYWORD_SCREENED"
    ARGUMENT_UNITS_BUILT = "ARGUMENT_UNITS_BUILT"
    EMBEDDING_READY = "EMBEDDING_READY"
    EMBEDDING_SCREENED = "EMBEDDING_SCREENED"
    DEEPSEEK_PACKET_READY = "DEEPSEEK_PACKET_READY"
    DEEPSEEK_RESULT_STAGED = "DEEPSEEK_RESULT_STAGED"
    IMPORT_VALIDATED = "IMPORT_VALIDATED"
    CANDIDATES_GENERATED = "CANDIDATES_GENERATED"
    AUDITED = "AUDITED"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    FAILED = "FAILED"


class SemanticEmbeddingView(StrEnum):
    PARAGRAPH_CURRENT = "PARAGRAPH_CURRENT"
    PARAGRAPH_LOCAL_CONTEXT = "PARAGRAPH_LOCAL_CONTEXT"
    ARGUMENT_UNIT = "ARGUMENT_UNIT"
    METHOD_PROTOTYPE = "METHOD_PROTOTYPE"
    CALIBRATION_EXAMPLE = "CALIBRATION_EXAMPLE"


class SemanticEmbeddingContract(StrEnum):
    LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1 = "LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1"
    ARGUMENT_UNIT_ONLY_V2 = "ARGUMENT_UNIT_ONLY_V2"
    PARAGRAPH_AUX_ARGUMENT_FINAL_V3 = "PARAGRAPH_AUX_ARGUMENT_FINAL_V3"


class SemanticPacketContract(StrEnum):
    LEGACY_ARGUMENT_PARAGRAPHS_V1 = "LEGACY_ARGUMENT_PARAGRAPHS_V1"
    COMPLETE_ARGUMENT_UNIT_V2 = "COMPLETE_ARGUMENT_UNIT_V2"


class SemanticScreenDecision(StrEnum):
    KEEP = "KEEP"
    EXCLUDE_DERIVED = "EXCLUDE_DERIVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"


class SemanticLlmBatchStatus(StrEnum):
    PACKET_READY = "PACKET_READY"
    RESULT_STAGED = "RESULT_STAGED"
    IMPORTED = "IMPORTED"
    REJECTED = "REJECTED"


class SemanticLlmDecision(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    REVIEW = "REVIEW"


class SemanticFunnelConfig(AStockModel):
    pipeline_version: str = Field(min_length=1)
    paragraphizer_version: str = Field(min_length=1)
    role_rule_version: str = Field(min_length=1)
    relation_rule_version: str = Field(min_length=1)
    argument_builder_version: str = Field(min_length=1)
    keyword_rule_version: str = Field(min_length=1)
    embedding_contract_version: SemanticEmbeddingContract = (
        SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1
    )
    question_terms: list[str] = Field(min_length=1)
    answer_terms: list[str] = Field(min_length=1)
    example_terms: list[str] = Field(min_length=1)
    counter_terms: list[str] = Field(min_length=1)
    conclusion_terms: list[str] = Field(min_length=1)
    transition_terms: list[str] = Field(min_length=1)
    marketing_terms: list[str] = Field(min_length=1)
    casual_terms: list[str] = Field(min_length=1)
    local_context: dict[str, int] = Field(default_factory=dict)
    argument_builder: dict[str, float | bool | int]
    semantic_screen: dict[str, float | bool]
    method_anchors: dict[BookMethodCategory, list[str]]

    @model_validator(mode="after")
    def validate_config(self) -> SemanticFunnelConfig:
        if set(self.method_anchors) != set(BookMethodCategory):
            raise ValueError("semantic funnel anchors must cover all method categories")
        for values in (
            self.question_terms,
            self.answer_terms,
            self.example_terms,
            self.counter_terms,
            self.conclusion_terms,
            self.transition_terms,
            self.marketing_terms,
            self.casual_terms,
        ):
            normalized = [value.casefold() for value in values]
            if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
                raise ValueError("semantic funnel term lists must be nonempty and unique")
        if self.embedding_contract_version is not (
            SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
        ):
            return self
        if self.schema_version != "3.0":
            raise ValueError("active semantic funnel config schema_version must be 3.0")
        if self.pipeline_version != "knowledge-semantic-funnel-three-view-v3":
            raise ValueError("active semantic funnel pipeline version is not exact")
        if self.local_context != {
            "previous_paragraphs": 1,
            "following_paragraphs": 2,
        }:
            raise ValueError("active local context must use previous 1 and following 2")
        maximum_chars_key = "maximum_argument_unit_chars"
        required_builder = {
            "prefer_merge_on_uncertainty",
            maximum_chars_key,
            "standalone_methodological_threshold",
            "review_boundary_threshold",
        }
        if set(self.argument_builder) != required_builder:
            raise ValueError("semantic argument builder config keys are incomplete")
        if not isinstance(self.argument_builder["prefer_merge_on_uncertainty"], bool):
            raise ValueError("prefer_merge_on_uncertainty must be boolean")
        if int(self.argument_builder[maximum_chars_key]) != 1800:
            raise ValueError("active maximum ArgumentUnit size must be exactly 1800")
        for key in ("standalone_methodological_threshold", "review_boundary_threshold"):
            value = float(self.argument_builder[key])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{key} must be normalized")
        required_screen = {
            "exploratory_keep_threshold",
            "exploratory_review_threshold",
            "automatic_exclusion_requires_calibration",
        }
        if set(self.semantic_screen) != required_screen:
            raise ValueError("semantic screen config keys are incomplete")
        keep_threshold = float(self.semantic_screen["exploratory_keep_threshold"])
        review_threshold = float(self.semantic_screen["exploratory_review_threshold"])
        if not 0.0 <= review_threshold <= keep_threshold <= 1.0:
            raise ValueError("semantic screen thresholds are invalid")
        if self.semantic_screen["automatic_exclusion_requires_calibration"] is not True:
            raise ValueError("uncalibrated semantic screening cannot enable exclusion")
        return self

    @property
    def maximum_argument_unit_chars(self) -> int:
        if self.embedding_contract_version is not (
            SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
        ):
            raise ValueError("unsupported semantic embedding contract version")
        return int(self.argument_builder["maximum_argument_unit_chars"])


class ParagraphLocator(AStockModel):
    locator_type: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_id: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    dom_path: str | None = None
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range_and_position(self) -> ParagraphLocator:
        if self.char_end <= self.char_start:
            raise ValueError("paragraph locator range must be positive")
        if self.page_number is None and not self.dom_path:
            raise ValueError("paragraph locator requires a page number or DOM path")
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if self.page_number is None or x1 <= x0 or y1 <= y0:
                raise ValueError("paragraph bbox requires a PDF page and positive area")
        return self


class ParagraphUnit(AStockModel):
    paragraph_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    content_version_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    locator: ParagraphLocator
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_char_count: int = Field(ge=1)
    primary_role: RhetoricalRole
    rhetorical_roles: list[RhetoricalRole] = Field(min_length=1)
    role_scores: dict[str, float]
    standalone_distillable: bool
    context_value: float = Field(ge=0.0, le=1.0)
    depends_on_previous: bool
    depends_on_next: bool
    merge_action: ParagraphMergeAction
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    matched_keyword_terms: list[str]
    reason_codes: list[str] = Field(min_length=1)
    role_rule_version: str = Field(min_length=1)
    paragraph_kind: ParagraphUnitKind = ParagraphUnitKind.TEXT
    visual_evidence_ids: list[str] = Field(default_factory=list)
    visual_chart_unit_ids: list[str] = Field(default_factory=list)
    visual_quality_status: str | None = None
    visual_reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role_and_merge_contract(self) -> ParagraphUnit:
        if self.primary_role not in self.rhetorical_roles:
            raise ValueError("primary rhetorical role must be listed")
        if len(self.rhetorical_roles) != len(set(self.rhetorical_roles)):
            raise ValueError("rhetorical roles must be unique")
        if set(self.role_scores) != {role.value for role in self.rhetorical_roles}:
            raise ValueError("role scores must cover exactly the selected roles")
        if any(not 0.0 <= score <= 1.0 for score in self.role_scores.values()):
            raise ValueError("role scores must be normalized")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("paragraph reason codes must be unique")
        if (
            self.standalone_distillable
            and self.merge_action is not ParagraphMergeAction.KEEP_AS_ARGUMENT
        ):
            raise ValueError("standalone paragraphs must keep as an argument")
        if (
            not self.standalone_distillable
            and self.merge_action is ParagraphMergeAction.KEEP_AS_ARGUMENT
        ):
            raise ValueError("context-dependent paragraphs require a merge or review action")
        if len(self.visual_evidence_ids) != len(set(self.visual_evidence_ids)):
            raise ValueError("visual evidence references must be unique")
        if len(self.visual_chart_unit_ids) != len(set(self.visual_chart_unit_ids)):
            raise ValueError("visual chart references must be unique")
        if len(self.visual_reason_codes) != len(set(self.visual_reason_codes)):
            raise ValueError("visual reason codes must be unique")
        if self.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE:
            if (
                not self.visual_evidence_ids
                or not self.visual_chart_unit_ids
                or self.visual_quality_status is None
                or self.standalone_distillable
                or self.merge_action is not ParagraphMergeAction.MERGE_WITH_BOTH
                or not self.depends_on_previous
                or not self.depends_on_next
                or RhetoricalRole.EVIDENCE not in self.rhetorical_roles
            ):
                raise ValueError(
                    "visual paragraphs require evidence lineage and both-side context"
                )
        return self


class SemanticContentItem(AStockModel):
    item_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    content_version_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paragraph_ids: list[str]
    title_paragraph_id: str | None = None

    @model_validator(mode="after")
    def validate_paragraph_refs(self) -> SemanticContentItem:
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("content item paragraph ids must be unique")
        if (
            self.title_paragraph_id is not None
            and self.title_paragraph_id not in self.paragraph_ids
        ):
            raise ValueError("title paragraph must belong to its content item")
        return self


class KeywordScreenResult(AStockModel):
    screen_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    decision: KeywordScreenDecision
    matched_terms_by_category: dict[BookMethodCategory, list[str]]
    matched_paragraph_ids: list[str]
    keyword_rule_version: str = Field(min_length=1)
    result_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_screen(self) -> KeywordScreenResult:
        has_hits = any(self.matched_terms_by_category.values())
        book_visual_lineage = self.candidate_reason_codes == [
            "BOOK_VISUAL_ARGUMENT_LINEAGE"
        ]
        if (
            self.decision is KeywordScreenDecision.CANDIDATE
            and not has_hits
            and not book_visual_lineage
        ):
            raise ValueError(
                "keyword candidates require a matched term or explicit book visual lineage"
            )
        if self.decision is KeywordScreenDecision.EXCLUDED_DERIVED and has_hits:
            raise ValueError("keyword-excluded items cannot carry method hits")
        if self.candidate_reason_codes and (
            self.decision is not KeywordScreenDecision.CANDIDATE
            or not book_visual_lineage
        ):
            raise ValueError("candidate reason codes are reserved for book visual lineage")
        if len(self.matched_paragraph_ids) != len(set(self.matched_paragraph_ids)):
            raise ValueError("keyword screen paragraph refs must be unique")
        return self


class ArgumentRelation(AStockModel):
    relation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    source_paragraph_id: str = Field(min_length=1)
    target_paragraph_id: str = Field(min_length=1)
    relation_type: ArgumentRelationType
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(min_length=1)
    relation_rule_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relation(self) -> ArgumentRelation:
        if self.source_paragraph_id == self.target_paragraph_id:
            raise ValueError("argument relations require two distinct paragraphs")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("argument relation reasons must be unique")
        return self


class ArgumentUnit(AStockModel):
    argument_unit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)
    relation_ids: list[str]
    start_ordinal: int = Field(ge=1)
    end_ordinal: int = Field(ge=1)
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rhetorical_roles: list[RhetoricalRole] = Field(min_length=1)
    status: ArgumentUnitStatus
    standalone_distillable: bool
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    boundary_confidence: float = Field(ge=0.0, le=1.0)
    method_categories: list[BookMethodCategory]
    reason_codes: list[str] = Field(min_length=1)
    builder_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_argument_boundary(self) -> ArgumentUnit:
        if self.end_ordinal < self.start_ordinal:
            raise ValueError("argument unit ordinal range is invalid")
        if self.end_ordinal - self.start_ordinal + 1 != len(self.paragraph_ids):
            raise ValueError("argument unit paragraphs must form one continuous range")
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("argument unit paragraph ids must be unique")
        if len(self.source_snapshot_ids) != len(set(self.source_snapshot_ids)):
            raise ValueError("argument unit source snapshots must be unique")
        if len(self.relation_ids) != len(set(self.relation_ids)):
            raise ValueError("argument unit relations must be unique")
        if self.standalone_distillable and self.status is not ArgumentUnitStatus.READY:
            raise ValueError("distillable argument units must be ready")
        return self


class SemanticFunnelRun(AStockModel):
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    input_hashes: list[str] = Field(min_length=1)
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pipeline_version: str = Field(min_length=1)
    paragraphizer_version: str = Field(min_length=1)
    role_rule_version: str = Field(min_length=1)
    # Runs written before relation extraction became an independently versioned
    # stage remain readable, while every new config must name the rule explicitly.
    relation_rule_version: str = Field(
        default="legacy-relation-rule-unrecorded",
        min_length=1,
    )
    argument_builder_version: str = Field(min_length=1)
    keyword_rule_version: str = Field(min_length=1)
    embedding_contract_version: SemanticEmbeddingContract = (
        SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1
    )
    rule_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stage: SemanticRunStage
    content_item_count: int = Field(ge=0)
    paragraph_count: int = Field(ge=0)
    argument_unit_count: int = Field(ge=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_run(self) -> SemanticFunnelRun:
        if len(self.input_hashes) != len(set(self.input_hashes)):
            raise ValueError("semantic run input hashes must be unique")
        if self.stage is SemanticRunStage.PENDING_HUMAN_REVIEW and self.finished_at is None:
            raise ValueError("finished semantic runs require finished_at")
        return self


class EmbeddingModelManifest(AStockModel):
    manifest_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    model_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tokenizer_asset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimension: int = Field(ge=1)
    normalized: bool = True
    local_only: bool = True
    embedding_contract_version: SemanticEmbeddingContract = (
        SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1
    )
    embedding_views: list[SemanticEmbeddingView] = Field(default_factory=list)
    auxiliary_views: list[SemanticEmbeddingView] = Field(default_factory=list)
    decision_view: SemanticEmbeddingView | None = None
    method_prototype_count: int = Field(default=0, ge=0)
    anchor_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    threshold_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calibration_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_view_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        contract = payload.get(
            "embedding_contract_version",
            SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1.value,
        )
        if isinstance(contract, SemanticEmbeddingContract):
            contract = contract.value
        if contract == SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3.value:
            return payload
        if "embedding_views" not in payload:
            payload["embedding_views"] = (
                [
                    SemanticEmbeddingView.PARAGRAPH_CURRENT.value,
                    SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT.value,
                    SemanticEmbeddingView.ARGUMENT_UNIT.value,
                ]
                if contract
                == SemanticEmbeddingContract.LEGACY_PARAGRAPH_CONTEXT_ARGUMENT_V1.value
                else [
                    SemanticEmbeddingView.ARGUMENT_UNIT.value,
                    SemanticEmbeddingView.METHOD_PROTOTYPE.value,
                ]
            )
        payload.setdefault(
            "auxiliary_views",
            [
                view
                for view in payload["embedding_views"]
                if view
                in {
                    SemanticEmbeddingView.PARAGRAPH_CURRENT.value,
                    SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT.value,
                }
            ],
        )
        payload.setdefault("decision_view", SemanticEmbeddingView.ARGUMENT_UNIT.value)
        payload.setdefault(
            "method_prototype_count",
            (
                14
                if SemanticEmbeddingView.METHOD_PROTOTYPE.value
                in payload["embedding_views"]
                else 0
            ),
        )
        return payload

    @model_validator(mode="after")
    def validate_required_views(self) -> EmbeddingModelManifest:
        if not self.local_only or not self.normalized:
            raise ValueError("semantic embeddings must be local and normalized")
        if self.embedding_contract_version is not (
            SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
        ):
            return self
        if self.schema_version != "3.0":
            raise ValueError("V3 embedding manifest schema_version must be 3.0")
        required_views = [
            SemanticEmbeddingView.PARAGRAPH_CURRENT,
            SemanticEmbeddingView.PARAGRAPH_LOCAL_CONTEXT,
            SemanticEmbeddingView.ARGUMENT_UNIT,
            SemanticEmbeddingView.METHOD_PROTOTYPE,
        ]
        required_auxiliary = required_views[:2]
        if self.embedding_views != required_views:
            raise ValueError("V3 manifest embedding views or order are not exact")
        if self.auxiliary_views != required_auxiliary:
            raise ValueError("V3 manifest auxiliary views or order are not exact")
        if self.decision_view is not SemanticEmbeddingView.ARGUMENT_UNIT:
            raise ValueError("V3 manifest requires ARGUMENT_UNIT decision view")
        if self.method_prototype_count != 14:
            raise ValueError("V3 manifest requires exactly 14 method prototypes")
        return self


class LocalEmbeddingAssetManifest(AStockModel):
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    repository_url: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    dimension: int = Field(ge=1)
    maximum_model_tokens: int = Field(ge=1)
    files: dict[str, str] = Field(min_length=1)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_file_hashes(self) -> LocalEmbeddingAssetManifest:
        if any(
            len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
            for digest in self.files.values()
        ):
            raise ValueError("embedding model file hashes must be sha256 values")
        return self


class SemanticArgumentScore(AStockModel):
    score_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    argument_unit_id: str = Field(min_length=1)
    embedding_manifest_id: str = Field(min_length=1)
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    category_scores: dict[BookMethodCategory, float]
    selected_categories: list[BookMethodCategory]
    decision: SemanticScreenDecision
    reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scores(self) -> SemanticArgumentScore:
        if any(not 0.0 <= score <= 1.0 for score in self.category_scores.values()):
            raise ValueError("semantic category scores must be normalized")
        if set(self.category_scores) != set(BookMethodCategory):
            raise ValueError("semantic scores must cover all 14 method categories")
        if not set(self.selected_categories).issubset(self.category_scores):
            raise ValueError("selected categories require scores")
        if self.selected_categories != sorted(
            set(self.selected_categories),
            key=lambda category: category.value,
        ):
            raise ValueError("selected categories must be unique and deterministically ordered")
        return self


class SemanticSkillCandidate(AStockModel):
    candidate_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    argument_unit_ids: list[str] = Field(min_length=1)
    method_categories: list[BookMethodCategory] = Field(min_length=1)
    payload_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1)
    llm_batch_id: str | None = Field(default=None, min_length=1)
    llm_response_object_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evaluation_status: BookEvaluationStatus = BookEvaluationStatus.NOT_RUN
    approval_status: BookApprovalStatus = BookApprovalStatus.PENDING
    human_review_status: HumanReviewStatus = HumanReviewStatus.PENDING

    @model_validator(mode="after")
    def prevent_automatic_approval(self) -> SemanticSkillCandidate:
        if self.evaluation_status is not BookEvaluationStatus.NOT_RUN:
            raise ValueError("semantic candidates cannot self-evaluate")
        if self.approval_status is not BookApprovalStatus.PENDING:
            raise ValueError("semantic candidates cannot self-approve")
        if self.human_review_status is not HumanReviewStatus.PENDING:
            raise ValueError("semantic candidates require human review")
        if len(self.argument_unit_ids) != len(set(self.argument_unit_ids)):
            raise ValueError("semantic candidate argument references must be unique")
        if (self.llm_batch_id is None) != (self.llm_response_object_sha256 is None):
            raise ValueError("semantic candidate batch provenance must be complete")
        return self


class SemanticPacketParagraph(AStockModel):
    paragraph_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    text: str = Field(min_length=1)
    text_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary_role: RhetoricalRole
    rhetorical_roles: list[RhetoricalRole] = Field(min_length=1)
    standalone_distillable: bool
    depends_on_previous: bool
    depends_on_next: bool
    merge_action: ParagraphMergeAction
    locator: ParagraphLocator | None = None
    paragraph_kind: ParagraphUnitKind = ParagraphUnitKind.TEXT
    visual_evidence_ids: list[str] = Field(default_factory=list)
    visual_chart_unit_ids: list[str] = Field(default_factory=list)
    visual_quality_status: str | None = None
    visual_reason_codes: list[str] = Field(default_factory=list)


class SemanticArgumentPacket(AStockModel):
    packet_contract_version: SemanticPacketContract = (
        SemanticPacketContract.LEGACY_ARGUMENT_PARAGRAPHS_V1
    )
    argument_unit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    source_snapshot_ids: list[str] = Field(min_length=1)
    argument_text: str | None = None
    argument_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paragraphs: list[SemanticPacketParagraph] = Field(min_length=1)
    relations: list[ArgumentRelation]
    topic_relevance: float = Field(ge=0.0, le=1.0)
    methodological_completeness: float = Field(ge=0.0, le=1.0)
    category_scores: dict[BookMethodCategory, float]
    selected_categories: list[BookMethodCategory]
    semantic_decision: SemanticScreenDecision
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_packet_boundary(self) -> SemanticArgumentPacket:
        if [paragraph.paragraph_id for paragraph in self.paragraphs] == []:
            raise ValueError("argument packet cannot contain an empty paragraph list")
        if any(
            relation.source_paragraph_id
            not in {paragraph.paragraph_id for paragraph in self.paragraphs}
            or relation.target_paragraph_id
            not in {paragraph.paragraph_id for paragraph in self.paragraphs}
            for relation in self.relations
        ):
            raise ValueError("argument packet relation must remain inside its argument")
        if (
            self.packet_contract_version
            is SemanticPacketContract.COMPLETE_ARGUMENT_UNIT_V2
        ):
            reconstructed = "\n".join(
                f"[{paragraph.ordinal}|{paragraph.primary_role.value}] {paragraph.text}"
                for paragraph in self.paragraphs
            )
            if self.argument_text != reconstructed:
                raise ValueError(
                    "active semantic packet must carry one complete ArgumentUnit"
                )
            if sha256(reconstructed.encode("utf-8")).hexdigest() != (
                self.argument_text_sha256
            ):
                raise ValueError("active semantic packet ArgumentUnit hash mismatch")
        return self


class SemanticDeepSeekCandidate(AStockModel):
    title: str = Field(min_length=1, max_length=120)
    method_summary: str = Field(min_length=1)
    applicability: list[str] = Field(min_length=1)
    counterevidence: list[str]
    invalidation_conditions: list[str] = Field(min_length=1)
    evidence_paragraph_ids: list[str] = Field(min_length=1)


class SemanticDeepSeekResult(AStockModel):
    argument_unit_id: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: SemanticLlmDecision
    method_categories: list[BookMethodCategory]
    reason_codes: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    candidates: list[SemanticDeepSeekCandidate]

    @model_validator(mode="after")
    def validate_result_decision(self) -> SemanticDeepSeekResult:
        if self.decision is SemanticLlmDecision.KEEP and not self.candidates:
            raise ValueError("kept argument results require at least one candidate")
        if self.decision is SemanticLlmDecision.KEEP and not self.method_categories:
            raise ValueError("kept argument results require method categories")
        if self.decision is SemanticLlmDecision.DROP and self.candidates:
            raise ValueError("dropped argument results cannot contain candidates")
        return self


class SemanticLlmBatch(AStockModel):
    batch_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_object_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: SemanticLlmBatchStatus
    local_only: bool = True
    exported_argument_count: int = Field(ge=1)
    imported_result_count: int = Field(ge=0)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_local_only(self) -> SemanticLlmBatch:
        if not self.local_only:
            raise ValueError("semantic LLM batches must remain local-only")
        return self


__all__ = [
    "ArgumentRelation",
    "ArgumentRelationType",
    "ArgumentUnit",
    "ArgumentUnitStatus",
    "SemanticEmbeddingContract",
    "EmbeddingModelManifest",
    "KeywordScreenDecision",
    "KeywordScreenResult",
    "LocalEmbeddingAssetManifest",
    "ParagraphLocator",
    "ParagraphMergeAction",
    "ParagraphUnitKind",
    "ParagraphUnit",
    "RhetoricalRole",
    "SemanticContentItem",
    "SemanticArgumentScore",
    "SemanticArgumentPacket",
    "SemanticDeepSeekCandidate",
    "SemanticDeepSeekResult",
    "SemanticEmbeddingView",
    "SemanticFunnelRun",
    "SemanticFunnelConfig",
    "SemanticRunStage",
    "SemanticScreenDecision",
    "SemanticSkillCandidate",
    "SemanticLlmBatch",
    "SemanticLlmBatchStatus",
    "SemanticLlmDecision",
    "SemanticPacketParagraph",
    "SemanticPacketContract",
]
