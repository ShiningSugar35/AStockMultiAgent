"""Strict OOXML review-workbook parsing and bounded conclusion interpretation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from astock.schemas import (
    ReviewArgumentTarget,
    ReviewParagraphRange,
    ReviewVerdict,
    ReviewWorkbookRecord,
)

REVIEW_SHEET_NAME = "Sheet1"
REVIEW_SUMMARY_SHEET_NAME = "复核意见汇总"
EXPECTED_REVIEW_RECORD_COUNT = 300
REVIEW_HEADERS = (
    "页码",
    "论点范围",
    "涉及主题",
    "复核原因",
    "图片",
    "可信度",
    "完整度",
    "原文预览",
    "复核结论（V2.0最终）",
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SOURCE_RANGE = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$")
_PAGE_RANGE = re.compile(
    r"第(?P<start_page>\d+)页第(?P<start_ordinal>\d+)(?:段)?"
    r"(?:\s*[~～至-]\s*"
    r"(?:第(?P<end_page>\d+)页)?第?(?P<end_ordinal>\d+)段)?"
)
_SHORT_RANGE = re.compile(r"第(?P<start_ordinal>\d+)段\s*[~～至-]\s*第?(?P<end_ordinal>\d+)段")
_RANGE_TOKEN = re.compile(
    rf"(?P<page>{_PAGE_RANGE.pattern})"
    r"|(?P<short>第(?P<short_start_ordinal>\d+)段\s*[~～至-]\s*"
    r"第?(?P<short_end_ordinal>\d+)段)"
)
_ANCHOR = re.compile(
    r"^【(?:"
    r"起：「(?P<start>.*?)」；止：「(?P<end>.*?)」"
    r"|段首：「(?P<head>.*?)」"
    r")】"
)
_TARGET_TITLE = re.compile(
    r"(?:合并为|另立|归为|完成|形成|构成|重建为|可保留|保留为|为)"
    r"[“](?P<title>[^”]+)[”]"
)
_TOPIC_CORRECTION = re.compile(r"主题(?:改为|调整为|修正为|保持)[“](?P<topics>[^”]+)[”]")
_ONLY_KEEP_TITLE = re.compile(r"(?:只保留|仅保留)[“](?P<title>[^”]+)[”]")
_CLAUSE_BOUNDARY = re.compile(r"[；]")


@dataclass(frozen=True, slots=True)
class ReviewRangeMention:
    value: ReviewParagraphRange
    start: int
    end: int
    clause: str


@dataclass(frozen=True, slots=True)
class ParsedReviewConclusion:
    targets: tuple[ReviewArgumentTarget, ...]
    corrected_topics: tuple[str, ...]
    range_mentions: tuple[ReviewRangeMention, ...]
    uncertainty_reason: str | None = None


def parse_review_workbook(
    path: Path,
    *,
    expected_record_count: int = EXPECTED_REVIEW_RECORD_COUNT,
) -> list[ReviewWorkbookRecord]:
    """Read the fixed review workbook without allowing implicit engine behavior."""

    sheets = _read_workbook(path, {REVIEW_SHEET_NAME, REVIEW_SUMMARY_SHEET_NAME})
    rows = sheets[REVIEW_SHEET_NAME]
    if not rows:
        raise ValueError("review workbook main sheet is empty")
    headers = tuple(str(value or "").strip() for value in rows[0][: len(REVIEW_HEADERS)])
    if headers != REVIEW_HEADERS:
        raise ValueError(f"review workbook headers changed: {headers!r}")
    records = [
        _record_from_row(excel_row, row)
        for excel_row, row in enumerate(rows[1:], start=2)
        if any(value not in (None, "") for value in row)
    ]
    if len(records) != expected_record_count:
        raise ValueError(
            f"review workbook record count changed: {len(records)} "
            f"(expected {expected_record_count})"
        )
    if len(
        {(item.page_number, item.source_start_ordinal, item.source_end_ordinal) for item in records}
    ) != len(records):
        raise ValueError("review workbook source ranges must be unique")
    summary = sheets[REVIEW_SUMMARY_SHEET_NAME]
    summary_values = {
        str(row[0]).strip(): int(row[1])
        for row in summary
        if len(row) >= 2 and row[0] not in (None, "") and isinstance(row[1], (int, float))
    }
    expected_summary = {
        "复核条目总数": expected_record_count,
        "V2.0 通过": sum(item.verdict is ReviewVerdict.PASS for item in records),
        "V2.0 需修改": sum(item.verdict is ReviewVerdict.MODIFY for item in records),
        "V2.0 驳回": sum(item.verdict is ReviewVerdict.REJECT for item in records),
    }
    if any(summary_values.get(key) != value for key, value in expected_summary.items()):
        raise ValueError("review workbook summary does not match its 300 detail rows")
    return records


def interpret_review_conclusion(record: ReviewWorkbookRecord) -> ParsedReviewConclusion:
    """Interpret only the current review row's explicit page/paragraph instructions."""

    source_range = ReviewParagraphRange(
        start_page=record.page_number,
        start_paragraph_ordinal=record.source_start_ordinal,
        end_page=record.page_number,
        end_paragraph_ordinal=record.source_end_ordinal,
    )
    if record.verdict is ReviewVerdict.REJECT:
        return ParsedReviewConclusion(
            targets=(),
            corrected_topics=tuple(record.topics),
            range_mentions=(),
        )
    if record.verdict is ReviewVerdict.PASS:
        target = ReviewArgumentTarget(
            title=_fallback_title(record, record.topics),
            ranges=[source_range],
            topics=record.topics,
        )
        return ParsedReviewConclusion(
            targets=(target,),
            corrected_topics=tuple(record.topics),
            range_mentions=(),
        )

    conclusion = record.conclusion.removeprefix("需修改：").strip()
    corrected_topics = _corrected_topics(conclusion, record.topics)
    mentions = _range_mentions(conclusion)
    title_matches = [
        match
        for match in _TARGET_TITLE.finditer(conclusion)
        if "主题" not in conclusion[max(0, match.start() - 8) : match.start()]
    ]
    targets: list[ReviewArgumentTarget] = []
    previous_title_end = 0
    for match in title_matches:
        group_mentions = [
            mention
            for mention in mentions
            if previous_title_end <= mention.start < match.start()
            and not _pure_exclusion_clause(mention.clause)
        ]
        previous_title_end = match.end()
        ranges = _deduplicated_ranges(group_mentions)
        if not ranges:
            continue
        targets.append(
            ReviewArgumentTarget(
                title=_clean_title(match.group("title")),
                ranges=ranges,
                topics=corrected_topics,
            )
        )
    if not targets:
        actionable = [mention for mention in mentions if not _pure_exclusion_clause(mention.clause)]
        ranges = _deduplicated_ranges(actionable)
        if ranges:
            keep_title = _ONLY_KEEP_TITLE.search(conclusion)
            targets.append(
                ReviewArgumentTarget(
                    title=(
                        _clean_title(keep_title.group("title"))
                        if keep_title is not None
                        else _fallback_title(record, corrected_topics)
                    ),
                    ranges=ranges,
                    topics=corrected_topics,
                )
            )
    uncertainty = None
    if not targets:
        uncertainty = "NO_ACTIONABLE_REVIEWED_PARAGRAPH_RANGE"
    return ParsedReviewConclusion(
        targets=tuple(targets),
        corrected_topics=tuple(corrected_topics),
        range_mentions=tuple(mentions),
        uncertainty_reason=uncertainty,
    )


def _record_from_row(excel_row: int, row: list[Any]) -> ReviewWorkbookRecord:
    values = [*row[: len(REVIEW_HEADERS)], *([None] * len(REVIEW_HEADERS))]
    page, source_range, topics, reason, image, confidence, completeness, preview, conclusion = (
        values[: len(REVIEW_HEADERS)]
    )
    if any(
        value in (None, "")
        for value in (
            page,
            source_range,
            topics,
            reason,
            image,
            confidence,
            completeness,
            preview,
            conclusion,
        )
    ):
        raise ValueError(f"review workbook row {excel_row} has a blank required cell")
    range_match = _SOURCE_RANGE.fullmatch(str(source_range))
    if range_match is None:
        raise ValueError(f"review workbook row {excel_row} has an invalid source range")
    conclusion_text = str(conclusion).strip()
    verdict = (
        ReviewVerdict.PASS
        if conclusion_text.startswith("通过")
        else ReviewVerdict.REJECT
        if conclusion_text.startswith("驳回")
        else ReviewVerdict.MODIFY
        if conclusion_text.startswith("需修改")
        else None
    )
    if verdict is None:
        raise ValueError(f"review workbook row {excel_row} has an unknown verdict")
    return ReviewWorkbookRecord(
        excel_row=excel_row,
        page_number=int(page),
        source_start_ordinal=int(range_match.group(1)),
        source_end_ordinal=int(range_match.group(2) or range_match.group(1)),
        topics=_split_topics(str(topics)),
        review_reason=str(reason).strip(),
        image_marker=str(image).strip(),
        confidence=float(confidence),
        completeness=float(completeness),
        source_preview=str(preview).strip(),
        conclusion=conclusion_text,
        verdict=verdict,
    )


def _range_mentions(text: str) -> list[ReviewRangeMention]:
    mentions: list[ReviewRangeMention] = []
    current_page: int | None = None
    matches = list(_RANGE_TOKEN.finditer(text))
    for match_index, match in enumerate(matches):
        token = match.group(0)
        page_match = _PAGE_RANGE.fullmatch(token)
        if page_match is not None:
            start_page = int(page_match.group("start_page"))
            start_ordinal = int(page_match.group("start_ordinal"))
            end_page = int(page_match.group("end_page") or start_page)
            end_ordinal = int(page_match.group("end_ordinal") or start_ordinal)
            current_page = end_page
        else:
            if current_page is None:
                continue
            start_page = current_page
            end_page = current_page
            start_ordinal = int(match.group("short_start_ordinal"))
            end_ordinal = int(match.group("short_end_ordinal"))
        anchor = _ANCHOR.match(text[match.end() :])
        start_summary = None
        end_summary = None
        end = match.end()
        if anchor is not None:
            start_summary = anchor.group("start") or anchor.group("head")
            end_summary = anchor.group("end") or anchor.group("head")
            end += anchor.end()
        next_boundary = _CLAUSE_BOUNDARY.search(text, end)
        next_range_start = (
            matches[match_index + 1].start() if match_index + 1 < len(matches) else len(text)
        )
        clause_end = min(
            next_boundary.start() if next_boundary is not None else len(text),
            next_range_start,
        )
        mentions.append(
            ReviewRangeMention(
                value=ReviewParagraphRange(
                    start_page=start_page,
                    start_paragraph_ordinal=start_ordinal,
                    end_page=end_page,
                    end_paragraph_ordinal=end_ordinal,
                    start_summary=start_summary,
                    end_summary=end_summary,
                ),
                start=match.start(),
                end=end,
                clause=text[end:clause_end],
            )
        )
    return mentions


def _pure_exclusion_clause(clause: str) -> bool:
    exclusion = any(
        token in clause for token in ("为页码", "不收录", "不进入方法论", "剔除", "排除")
    )
    retention = any(
        token in clause
        for token in (
            "保留",
            "并入",
            "合并",
            "论点",
            "规则",
            "原则",
            "讨论",
            "验证",
            "建立",
            "用于",
            "延续",
            "完整",
            "应与",
            "应并",
        )
    )
    return exclusion and not retention


def _deduplicated_ranges(
    mentions: list[ReviewRangeMention],
) -> list[ReviewParagraphRange]:
    result: list[ReviewParagraphRange] = []
    seen: set[tuple[int, int, int, int]] = set()
    for mention in mentions:
        value = mention.value
        key = (
            value.start_page,
            value.start_paragraph_ordinal,
            value.end_page,
            value.end_paragraph_ordinal,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _corrected_topics(text: str, fallback: list[str]) -> list[str]:
    match = _TOPIC_CORRECTION.search(text)
    return _split_topics(match.group("topics")) if match is not None else fallback


def _split_topics(value: str) -> list[str]:
    topics = [
        item.strip()
        for item in re.split(r"[,，、；/]", value)
        if item.strip() and item.strip() != "（未分类）"
    ]
    return topics or ["未分类"]


def _clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" 。；")


def _fallback_title(record: ReviewWorkbookRecord, topics: list[str]) -> str:
    if topics and topics != ["未分类"]:
        return "、".join(topics)
    return (
        f"第{record.page_number}页第{record.source_start_ordinal}"
        f"-{record.source_end_ordinal}段复核论点"
    )


def _column_index(cell_reference: str) -> int:
    match = re.match(r"[A-Z]+", cell_reference)
    if match is None:
        raise ValueError(f"invalid OOXML cell reference: {cell_reference}")
    index = 0
    for char in match.group():
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _read_workbook(path: Path, sheet_names: set[str]) -> dict[str, list[list[Any]]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with ZipFile(path) as archive:
        shared_strings = _shared_strings(archive)
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        }
        result: dict[str, list[list[Any]]] = {}
        for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
            name = sheet.attrib["name"]
            if name not in sheet_names:
                continue
            relation_id = sheet.attrib[f"{{{_REL_NS}}}id"]
            target = targets[relation_id].lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            result[name] = _sheet_rows(archive.read(target), shared_strings)
    missing = sheet_names - result.keys()
    if missing:
        raise ValueError(f"review workbook missing sheets: {sorted(missing)!r}")
    return result


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]


def _sheet_rows(data: bytes, shared_strings: list[str]) -> list[list[Any]]:
    root = ElementTree.fromstring(data)
    rows: list[list[Any]] = []
    for row in root.findall(f".//{{{_MAIN_NS}}}row"):
        values: list[Any] = []
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            index = _column_index(cell.attrib["r"])
            while len(values) <= index:
                values.append(None)
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value: Any = "".join(node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t"))
            else:
                node = cell.find(f"{{{_MAIN_NS}}}v")
                raw = node.text if node is not None else None
                if raw is None:
                    value = None
                elif cell_type == "s":
                    value = shared_strings[int(raw)]
                elif cell_type == "b":
                    value = raw == "1"
                elif cell_type in {"str", "e"}:
                    value = raw
                else:
                    number = float(raw)
                    value = int(number) if number.is_integer() else number
            values[index] = value
        rows.append(values)
    return rows


__all__ = [
    "EXPECTED_REVIEW_RECORD_COUNT",
    "ParsedReviewConclusion",
    "REVIEW_HEADERS",
    "ReviewRangeMention",
    "interpret_review_conclusion",
    "parse_review_workbook",
]
