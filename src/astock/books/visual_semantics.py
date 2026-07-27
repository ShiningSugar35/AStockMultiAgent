"""Adapter from book layout atoms into the existing argument-aware semantic funnel."""

from __future__ import annotations

from pathlib import Path

import yaml

from astock.books.repository import BookRepository
from astock.books.visual_repository import BookVisualRepository
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.semantic_funnel import ParagraphizedContent, build_argument_units
from astock.knowledge.semantic_repository import SemanticFunnelRepository
from astock.schemas import (
    BookLayoutAtom,
    BookLayoutAtomKind,
    BookMethodCategory,
    BookSourceManifest,
    BookVisualDistillationConfig,
    BookVisualRun,
    BookVisualRunStage,
    BookVisualSemanticRef,
    ChartUnit,
    ImageOcrResult,
    KeywordScreenDecision,
    KeywordScreenResult,
    ParagraphLocator,
    ParagraphMergeAction,
    ParagraphUnit,
    ParagraphUnitKind,
    RhetoricalRole,
    SemanticContentItem,
    SemanticEmbeddingContract,
    SemanticFunnelConfig,
    SemanticFunnelRun,
    SemanticRunStage,
)


def load_book_visual_distillation_config(
    path: Path,
) -> BookVisualDistillationConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"book visual config must contain a mapping: {path}")
    return BookVisualDistillationConfig.model_validate(payload)


class BookVisualSemanticService:
    """Materialize book visuals inside the canonical semantic repository."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.books = BookRepository(state)
        self.visuals = BookVisualRepository(state)
        self.semantic = SemanticFunnelRepository(state)

    def materialize(
        self,
        run_id: str,
        *,
        config: SemanticFunnelConfig,
        keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
    ) -> tuple[SemanticFunnelRun, list[BookVisualSemanticRef]]:
        visual_run = self.visuals.get_run(run_id)
        if visual_run is None:
            raise KeyError(run_id)
        if visual_run.stage is BookVisualRunStage.AUDITED:
            if visual_run.semantic_run_id is None:
                raise ValueError("audited visual run lost its semantic run id")
            semantic_run = self.semantic.get_run(visual_run.semantic_run_id)
            if semantic_run is None:
                raise ValueError("audited visual run lost its semantic run")
            refs = self.visuals.semantic_refs(run_id)
            self._verify_complete_refs(run_id, refs)
            return semantic_run, refs
        if visual_run.stage is BookVisualRunStage.SEMANTIC_MATERIALIZED:
            if visual_run.semantic_run_id is None:
                raise ValueError("semantic visual run lost its semantic run id")
            semantic_run = self.semantic.get_run(visual_run.semantic_run_id)
            if semantic_run is None:
                raise ValueError("semantic visual run lost its semantic run")
            refs = self.visuals.semantic_refs(run_id)
            self._verify_complete_refs(run_id, refs)
            return semantic_run, refs
        if visual_run.stage is not BookVisualRunStage.CHARTS_CLASSIFIED:
            raise ValueError("book visual semantics require classified charts")
        if config.embedding_contract_version is not (
            SemanticEmbeddingContract.PARAGRAPH_AUX_ARGUMENT_FINAL_V3
        ):
            raise ValueError("book visual semantics require the active three-view contract")

        manifest = self.books.get_manifest(visual_run.source_manifest_id)
        if manifest is None:
            raise ValueError("book visual source manifest is missing")
        atoms = self.visuals.layout_atoms(run_id)
        chart_units = self.visuals.chart_units(run_id)
        ocr_results = self.visuals.ocr_results(run_id)
        charts_by_evidence = {unit.evidence_id: unit for unit in chart_units}
        ocr_by_evidence = {result.evidence_id: result for result in ocr_results}
        config_hash = content_hash(config.model_dump(mode="json"))
        keyword_hash = content_hash(
            {
                category.value: list(terms)
                for category, terms in sorted(
                    keyword_terms.items(),
                    key=lambda item: item[0].value,
                )
            }
        )
        semantic_run_id = "semantic-run:" + content_hash(
            {
                "book_visual_run_id": run_id,
                "semantic_config_sha256": config_hash,
                "keyword_terms_sha256": keyword_hash,
            }
        )
        contents, chart_paragraph_ids = self._contents(
            visual_run,
            semantic_run_id,
            config,
            keyword_terms,
            charts_by_evidence,
            ocr_by_evidence,
            atoms,
        )
        input_manifest_object = self.object_store.put_json(
            {
                "book_visual_run_id": run_id,
                "source_manifest_id": manifest.manifest_id,
                "source_snapshot_id": manifest.snapshot_id,
                "raw_object_sha256": manifest.raw_object_sha256,
                "semantic_config_sha256": config_hash,
                "keyword_terms_sha256": keyword_hash,
            }
        )
        semantic_run = SemanticFunnelRun(
            schema_version=config.schema_version,
            run_id=semantic_run_id,
            author_source_id=manifest.author_source_id,
            input_hashes=sorted(
                {
                    manifest.raw_object_sha256,
                    run_id,
                    config_hash,
                    keyword_hash,
                }
            ),
            input_manifest_sha256=input_manifest_object.sha256,
            pipeline_version=config.pipeline_version,
            paragraphizer_version=config.paragraphizer_version,
            role_rule_version=config.role_rule_version,
            relation_rule_version=config.relation_rule_version,
            argument_builder_version=config.argument_builder_version,
            keyword_rule_version=config.keyword_rule_version,
            embedding_contract_version=config.embedding_contract_version,
            rule_config_sha256=config_hash,
            stage=SemanticRunStage.ARGUMENT_UNITS_BUILT,
            content_item_count=len(contents),
            paragraph_count=sum(len(content.paragraphs) for content in contents),
            argument_unit_count=sum(
                len(content.argument_units) for content in contents
            ),
            started_at=visual_run.started_at,
            created_at=visual_run.started_at,
        )
        self.semantic.register_paragraphized(semantic_run, contents)
        refs = self._semantic_refs(
            visual_run,
            semantic_run,
            contents,
            chart_units,
            chart_paragraph_ids,
        )
        materialized = _object_store_run(
            self.object_store,
            visual_run.model_copy(
                update={
                    "stage": BookVisualRunStage.SEMANTIC_MATERIALIZED,
                    "semantic_run_id": semantic_run_id,
                }
            ),
        )
        self.visuals.register_semantic_refs(materialized, refs)
        return semantic_run, refs

    def _verify_complete_refs(
        self,
        run_id: str,
        refs: list[BookVisualSemanticRef],
    ) -> None:
        expected = sum(
            not unit.decorative_excluded
            for unit in self.visuals.chart_units(run_id)
        )
        if len(refs) != expected:
            raise ValueError("semantic visual run has incomplete chart lineage")

    def _contents(
        self,
        visual_run: BookVisualRun,
        semantic_run_id: str,
        config: SemanticFunnelConfig,
        keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
        charts_by_evidence: dict[str, ChartUnit],
        ocr_by_evidence: dict[str, ImageOcrResult],
        atoms: list[BookLayoutAtom],
    ) -> tuple[list[ParagraphizedContent], dict[str, str]]:
        manifest = self.books.get_manifest(visual_run.source_manifest_id)
        assert manifest is not None
        page_numbers = sorted({atom.page_number for atom in atoms})
        contents: list[ParagraphizedContent] = []
        chart_paragraph_ids: dict[str, str] = {}
        for page_number in page_numbers:
            page_atoms = [atom for atom in atoms if atom.page_number == page_number]
            paragraph_specs: list[
                tuple[
                    BookLayoutAtom,
                    str,
                    str,
                    ChartUnit | None,
                    ImageOcrResult | None,
                ]
            ] = []
            for atom in page_atoms:
                if atom.atom_kind is BookLayoutAtomKind.TEXT_BLOCK:
                    assert atom.text_object_sha256 is not None
                    text = self.object_store.get_bytes(
                        atom.text_object_sha256
                    ).decode("utf-8")
                    paragraph_specs.append(
                        (atom, text, atom.text_object_sha256, None, None)
                    )
                    continue
                assert atom.evidence_id is not None
                chart = charts_by_evidence[atom.evidence_id]
                if chart.decorative_excluded:
                    continue
                ocr = ocr_by_evidence[atom.evidence_id]
                if ocr.text_object_sha256 is not None:
                    ocr_text = self.object_store.get_bytes(
                        ocr.text_object_sha256
                    ).decode("utf-8")
                else:
                    ocr_text = ""
                text = ocr_text.strip() or f"[Visual evidence: {chart.chart_type.value}]"
                text_object = self.object_store.put_bytes(text.encode("utf-8"))
                paragraph_specs.append((atom, text, text_object.sha256, chart, ocr))
            if not paragraph_specs:
                continue

            content_id = f"{manifest.manifest_id}:page:{page_number}"
            item_id = "semantic-item:" + content_hash(
                {
                    "semantic_run_id": semantic_run_id,
                    "content_id": content_id,
                    "page_number": page_number,
                }
            )
            paragraphs: list[ParagraphUnit] = []
            matched_by_category: dict[BookMethodCategory, set[str]] = {
                category: set() for category in BookMethodCategory
            }
            for ordinal, (atom, text, text_hash, chart, ocr) in enumerate(
                paragraph_specs,
                start=1,
            ):
                hits = _keyword_hits(text, keyword_terms)
                for category, terms in hits.items():
                    matched_by_category[category].update(terms)
                matched_terms = sorted(
                    {term for terms in hits.values() for term in terms},
                    key=lambda term: (term.casefold(), term),
                )
                paragraph_id = "paragraph-unit:" + content_hash(
                    {
                        "semantic_run_id": semantic_run_id,
                        "item_id": item_id,
                        "ordinal": ordinal,
                        "layout_atom_id": atom.atom_id,
                        "text_object_sha256": text_hash,
                    }
                )
                if chart is None:
                    roles = _native_roles(text, config)
                    paragraph = _text_paragraph(
                        paragraph_id=paragraph_id,
                        semantic_run_id=semantic_run_id,
                        manifest=manifest,
                        content_id=content_id,
                        page_number=page_number,
                        ordinal=ordinal,
                        atom=atom,
                        text=text,
                        text_hash=text_hash,
                        roles=roles,
                        matched_terms=matched_terms,
                        config=config,
                    )
                else:
                    assert ocr is not None and atom.evidence_id is not None
                    visual_reasons = sorted(set(chart.review_reason_codes))
                    paragraph = ParagraphUnit(
                        paragraph_id=paragraph_id,
                        run_id=semantic_run_id,
                        author_source_id=manifest.author_source_id,
                        content_type="PRIVATE_BOOK_PAGE",
                        content_id=content_id,
                        content_version_id=manifest.raw_object_sha256,
                        ordinal=ordinal,
                        locator=ParagraphLocator(
                            locator_type="BOOK_PDF_IMAGE_PLACEMENT",
                            source_snapshot_id=manifest.snapshot_id,
                            source_object_sha256=manifest.raw_object_sha256,
                            content_id=content_id,
                            page_number=page_number,
                            bbox=atom.bbox,
                            char_start=0,
                            char_end=len(text),
                            created_at=visual_run.started_at,
                        ),
                        text_object_sha256=text_hash,
                        normalized_char_count=len(text),
                        primary_role=RhetoricalRole.EVIDENCE,
                        rhetorical_roles=[RhetoricalRole.EVIDENCE],
                        role_scores={RhetoricalRole.EVIDENCE.value: 1.0},
                        standalone_distillable=False,
                        context_value=1.0,
                        depends_on_previous=True,
                        depends_on_next=True,
                        merge_action=ParagraphMergeAction.MERGE_WITH_BOTH,
                        topic_relevance=0.8 if matched_terms else 0.5,
                        methodological_completeness=0.3,
                        matched_keyword_terms=matched_terms,
                        reason_codes=["VISUAL_EVIDENCE_REQUIRES_BOTH_SIDES"],
                        role_rule_version=config.role_rule_version,
                        paragraph_kind=ParagraphUnitKind.VISUAL_EVIDENCE,
                        visual_evidence_ids=[atom.evidence_id],
                        visual_chart_unit_ids=[chart.chart_unit_id],
                        visual_quality_status=ocr.status.value,
                        visual_reason_codes=visual_reasons,
                        created_at=visual_run.started_at,
                    )
                    chart_paragraph_ids[chart.chart_unit_id] = paragraph_id
                paragraphs.append(paragraph)

            any_hits = any(matched_by_category.values())
            has_visual = any(
                paragraph.paragraph_kind is ParagraphUnitKind.VISUAL_EVIDENCE
                for paragraph in paragraphs
            )
            candidate_reason_codes: list[str] = []
            if has_visual and not any_hits:
                candidate_reason_codes = ["BOOK_VISUAL_ARGUMENT_LINEAGE"]
                paragraphs = [
                    paragraph.model_copy(
                        update={
                            "visual_reason_codes": sorted(
                                {
                                    *paragraph.visual_reason_codes,
                                    "BOOK_VISUAL_NO_METHOD_KEYWORD_REVIEW_REQUIRED",
                                }
                            )
                        }
                    )
                    if paragraph.paragraph_kind
                    is ParagraphUnitKind.VISUAL_EVIDENCE
                    else paragraph
                    for paragraph in paragraphs
                ]
            normalized = self.object_store.put_bytes(
                "\n".join(
                    self.object_store.get_bytes(
                        paragraph.text_object_sha256
                    ).decode("utf-8")
                    for paragraph in paragraphs
                ).encode("utf-8")
            )
            item = SemanticContentItem(
                item_id=item_id,
                run_id=semantic_run_id,
                author_source_id=manifest.author_source_id,
                content_type="PRIVATE_BOOK_PAGE",
                content_id=content_id,
                content_version_id=manifest.raw_object_sha256,
                source_snapshot_id=manifest.snapshot_id,
                source_object_sha256=manifest.raw_object_sha256,
                normalized_object_sha256=normalized.sha256,
                paragraph_ids=[paragraph.paragraph_id for paragraph in paragraphs],
                created_at=visual_run.started_at,
            )
            decision = (
                KeywordScreenDecision.CANDIDATE
                if any_hits or has_visual
                else KeywordScreenDecision.NEEDS_REVIEW
            )
            screen_core = {
                "screen_id": "keyword-screen:"
                + content_hash(
                    {
                        "semantic_run_id": semantic_run_id,
                        "item_id": item_id,
                        "keyword_rule_version": config.keyword_rule_version,
                    }
                ),
                "run_id": semantic_run_id,
                "item_id": item_id,
                "decision": decision.value,
                "matched_terms_by_category": {
                    category.value: sorted(terms)
                    for category, terms in matched_by_category.items()
                },
                "matched_paragraph_ids": [
                    paragraph.paragraph_id
                    for paragraph in paragraphs
                    if paragraph.matched_keyword_terms
                ],
                "keyword_rule_version": config.keyword_rule_version,
                "candidate_reason_codes": candidate_reason_codes,
                "created_at": visual_run.started_at.isoformat(),
            }
            screen_object = self.object_store.put_json(screen_core)
            screen = KeywordScreenResult.model_validate(
                {
                    **screen_core,
                    "result_object_sha256": screen_object.sha256,
                }
            )
            argument_units, relations = build_argument_units(
                item,
                paragraphs,
                object_store=self.object_store,
                config=config,
                keyword_terms=keyword_terms,
            )
            contents.append(
                ParagraphizedContent(
                    item=item,
                    paragraphs=tuple(paragraphs),
                    screen=screen,
                    argument_units=tuple(argument_units),
                    relations=tuple(relations),
                )
            )
        return contents, chart_paragraph_ids

    def _semantic_refs(
        self,
        visual_run: BookVisualRun,
        semantic_run: SemanticFunnelRun,
        contents: list[ParagraphizedContent],
        chart_units: list[ChartUnit],
        chart_paragraph_ids: dict[str, str],
    ) -> list[BookVisualSemanticRef]:
        argument_by_paragraph = {
            paragraph_id: argument
            for content in contents
            for argument in content.argument_units
            for paragraph_id in argument.paragraph_ids
        }
        relation_by_id = {
            relation.relation_id: relation
            for content in contents
            for relation in content.relations
        }
        refs: list[BookVisualSemanticRef] = []
        for chart in chart_units:
            if chart.decorative_excluded:
                continue
            paragraph_id = chart_paragraph_ids.get(chart.chart_unit_id)
            if paragraph_id is None:
                raise ValueError("non-decorative chart is missing its visual paragraph")
            argument = argument_by_paragraph.get(paragraph_id)
            if argument is None:
                raise ValueError("visual paragraph is missing its ArgumentUnit")
            relation_ids = [
                relation_id
                for relation_id in argument.relation_ids
                if paragraph_id
                in {
                    relation_by_id[relation_id].source_paragraph_id,
                    relation_by_id[relation_id].target_paragraph_id,
                }
            ]
            ref = BookVisualSemanticRef(
                ref_id="book-visual-semantic-ref:"
                + content_hash(
                    {
                        "run_id": visual_run.run_id,
                        "chart_unit_id": chart.chart_unit_id,
                        "semantic_run_id": semantic_run.run_id,
                        "paragraph_id": paragraph_id,
                        "argument_unit_id": argument.argument_unit_id,
                    }
                ),
                run_id=visual_run.run_id,
                chart_unit_id=chart.chart_unit_id,
                semantic_run_id=semantic_run.run_id,
                paragraph_id=paragraph_id,
                argument_unit_id=argument.argument_unit_id,
                relation_ids=relation_ids,
                created_at=visual_run.started_at,
            )
            stored = self.object_store.put_json(
                ref.model_dump(mode="json", exclude={"ref_object_sha256"})
            )
            refs.append(ref.model_copy(update={"ref_object_sha256": stored.sha256}))
        return refs


def _text_paragraph(
    *,
    paragraph_id: str,
    semantic_run_id: str,
    manifest: BookSourceManifest,
    content_id: str,
    page_number: int,
    ordinal: int,
    atom: BookLayoutAtom,
    text: str,
    text_hash: str,
    roles: list[RhetoricalRole],
    matched_terms: list[str],
    config: SemanticFunnelConfig,
) -> ParagraphUnit:
    role_set = set(roles)
    depends_previous = bool(
        role_set
        & {
            RhetoricalRole.EVIDENCE,
            RhetoricalRole.CONCLUSION,
            RhetoricalRole.COUNTERARGUMENT,
        }
    )
    depends_next = RhetoricalRole.QUESTION in role_set
    if depends_previous and depends_next:
        merge_action = ParagraphMergeAction.MERGE_WITH_BOTH
    elif depends_previous:
        merge_action = ParagraphMergeAction.MERGE_WITH_PREVIOUS
    elif depends_next:
        merge_action = ParagraphMergeAction.MERGE_WITH_FOLLOWING
    else:
        merge_action = ParagraphMergeAction.NEEDS_REVIEW
    return ParagraphUnit(
        paragraph_id=paragraph_id,
        run_id=semantic_run_id,
        author_source_id=manifest.author_source_id,
        content_type="PRIVATE_BOOK_PAGE",
        content_id=content_id,
        content_version_id=manifest.raw_object_sha256,
        ordinal=ordinal,
        locator=ParagraphLocator(
            locator_type="BOOK_PDF_NATIVE_TEXT_BLOCK",
            source_snapshot_id=manifest.snapshot_id,
            source_object_sha256=manifest.raw_object_sha256,
            content_id=content_id,
            page_number=page_number,
            bbox=atom.bbox,
            char_start=0,
            char_end=len(text),
            created_at=manifest.created_at,
        ),
        text_object_sha256=text_hash,
        normalized_char_count=len(text),
        primary_role=roles[0],
        rhetorical_roles=roles,
        role_scores={
            role.value: round(max(0.6, 0.95 - index * 0.08), 6)
            for index, role in enumerate(roles)
        },
        standalone_distillable=False,
        context_value=0.8 if depends_previous or depends_next else 0.6,
        depends_on_previous=depends_previous,
        depends_on_next=depends_next,
        merge_action=merge_action,
        topic_relevance=0.8 if matched_terms else 0.4,
        methodological_completeness=(
            0.5
            if role_set & {RhetoricalRole.CLAIM, RhetoricalRole.OPERATIONAL_RULE}
            else 0.3
        ),
        matched_keyword_terms=matched_terms,
        reason_codes=["BOOK_NATIVE_TEXT_LAYOUT_CONTEXT"],
        role_rule_version=config.role_rule_version,
        created_at=manifest.created_at,
    )


def _native_roles(text: str, config: SemanticFunnelConfig) -> list[RhetoricalRole]:
    folded = text.casefold()
    roles: list[RhetoricalRole] = []
    if (
        "claim" in folded
        or "观点" in text
        or "主张" in text
        or any(term.casefold() in folded for term in config.answer_terms)
    ):
        roles.append(RhetoricalRole.CLAIM)
    if (
        "therefore" in folded
        or "thus" in folded
        or "因此" in text
        or "所以" in text
        or any(term.casefold() in folded for term in config.conclusion_terms)
    ):
        roles.insert(0, RhetoricalRole.CONCLUSION)
    if any(term.casefold() in folded for term in config.question_terms):
        roles.insert(0, RhetoricalRole.QUESTION)
    if not roles:
        roles.append(RhetoricalRole.BACKGROUND)
    return list(dict.fromkeys(roles))


def _keyword_hits(
    text: str,
    keyword_terms: dict[BookMethodCategory, tuple[str, ...]],
) -> dict[BookMethodCategory, tuple[str, ...]]:
    folded = text.casefold()
    return {
        category: tuple(term for term in terms if term.casefold() in folded)
        for category, terms in keyword_terms.items()
    }


def _object_store_run(
    object_store: ObjectStore,
    run: BookVisualRun,
) -> BookVisualRun:
    base = run.model_copy(update={"run_object_sha256": None})
    stored = object_store.put_json(
        base.model_dump(mode="json", exclude={"run_object_sha256"})
    )
    return base.model_copy(update={"run_object_sha256": stored.sha256})


__all__ = [
    "BookVisualSemanticService",
    "load_book_visual_distillation_config",
]
