"""Lazy OCR adapter with a small deterministic result surface."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    average_confidence: float | None


class OcrEngine(Protocol):
    name: str
    version: str

    def recognize(self, image_bytes: bytes) -> OcrResult: ...


class RapidOcrEngine:
    name = "rapidocr-onnxruntime"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self.version = version("rapidocr-onnxruntime")
        self._engine = RapidOCR()

    def recognize(self, image_bytes: bytes) -> OcrResult:
        raw_result, _ = self._engine(image_bytes)
        if not raw_result:
            return OcrResult(text="", average_confidence=None)
        rows: list[tuple[float, float, str, float]] = []
        for raw_item in raw_result:
            item = list(raw_item)
            if len(item) < 3:
                continue
            box = item[0]
            text = str(item[1]).strip()
            confidence = float(item[2])
            if not text:
                continue
            y = _box_coordinate(box, axis=1)
            x = _box_coordinate(box, axis=0)
            rows.append((y, x, text, confidence))
        rows.sort(key=lambda item: (item[0], item[1]))
        if not rows:
            return OcrResult(text="", average_confidence=None)
        confidence = sum(item[3] for item in rows) / len(rows)
        return OcrResult(
            text="\n".join(item[2] for item in rows),
            average_confidence=max(0.0, min(1.0, confidence)),
        )


def _box_coordinate(raw_box: Any, *, axis: int) -> float:
    try:
        points = list(raw_box)
        values = [float(list(point)[axis]) for point in points]
    except (TypeError, ValueError, IndexError):
        return 0.0
    return min(values, default=0.0)
