from __future__ import annotations

import json

from astock.knowledge.distillation import _docx_segments, _pdf_segments, _zhihu_segments


def test_pdf_layout_lines_are_merged_without_crossing_page_boundaries() -> None:
    raw = "估值需要结合\n现金流折现。\n\n第二段需要\n风险控制。"

    segments = _pdf_segments(raw)

    assert [text for text, _, _ in segments] == [
        "估值需要结合现金流折现。",
        "第二段需要风险控制。",
    ]
    assert [raw[start:end] for _, start, end in segments] == [
        "估值需要结合\n现金流折现。",
        "第二段需要\n风险控制。",
    ]


def test_oversized_docx_block_is_split_at_sentence_boundaries() -> None:
    sentence = "现金流与估值需要共同验证。"
    raw = sentence * 100

    segments = _docx_segments(raw)

    assert len(segments) > 1
    assert all(len(text) <= 800 for text, _, _ in segments)
    assert "".join(text for text, _, _ in segments) == raw
    assert all(raw[start:end] == text for text, start, end in segments)


def test_zhihu_html_is_split_into_visible_blocks_with_exact_source_spans() -> None:
    raw = (
        "<h2>估值框架</h2><p>现金流&nbsp;<strong>折现</strong></p>"
        "<script>不应进入蒸馏</script><p>风险<br>止损</p>"
    )

    segments = _zhihu_segments(raw)

    assert [text for text, _, _ in segments] == [
        "估值框架",
        "现金流 折现",
        "风险",
        "止损",
    ]
    assert all(text in raw[start:end] or " " in text for text, start, end in segments)
    assert "不应进入蒸馏" not in {text for text, _, _ in segments}


def test_structured_thought_uses_html_once_instead_of_duplicating_string_leaves() -> None:
    raw = json.dumps(
        {
            "content_html": "<p>第一段</p><p>第二段</p>",
            "segments": [{"content": "第一段", "type": "text"}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    segments = _zhihu_segments(raw)

    assert [text for text, _, _ in segments] == ["第一段", "第二段"]
    assert {(start, end) for _, start, end in segments} == {(0, len(raw))}
