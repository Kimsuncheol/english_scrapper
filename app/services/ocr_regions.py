"""Shared line-region normalization for local OCR engines."""

import math
import unicodedata
from app.schemas import BoundingBox, OCRRegion, OCRText
from app.services.errors import OCRError


def normalize_regions(boxes, texts, scores, width: int, height: int, min_confidence: float = 0.5, *, engine: str) -> OCRText:
    """Keep line regions in engine reading order; enclose polygons in pixel boxes."""
    if all(value is None for value in (boxes, texts, scores)):
        boxes, texts, scores = [], [], []
    if any(value is None for value in (boxes, texts, scores)) or not len(boxes) == len(texts) == len(scores):
        raise OCRError("ocr_failed", "OCR engine returned an unsupported result format.", 500)
    regions = []
    for polygon, text, score in zip(boxes, texts, scores):
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            score = float(score)
            points = [(float(x), float(y)) for x, y in polygon]
            if len(points) != 4 or not all(math.isfinite(v) for point in points for v in point):
                continue
            if not math.isfinite(score) or not 0 <= score <= 1:
                continue
            x = max(0, math.floor(min(p[0] for p in points)))
            y = max(0, math.floor(min(p[1] for p in points)))
            right = min(width, math.ceil(max(p[0] for p in points)))
            bottom = min(height, math.ceil(max(p[1] for p in points)))
            if right <= x or bottom <= y:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        regions.append(OCRRegion(
            text=unicodedata.normalize("NFC", " ".join(text.split())), confidence=score,
            bounding_box=BoundingBox(x=x, y=y, width=right-x, height=bottom-y),
            low_confidence=score < min_confidence,
        ))
    mean = sum(r.confidence for r in regions) / len(regions) if regions else None
    notes = []
    if not regions:
        notes.append("No readable text regions were found.")
    elif any(r.low_confidence for r in regions):
        notes.append("Some recognized regions have low confidence; verify their text.")
    return OCRText(engine=engine, regions=regions,
        raw_text="\n".join(r.text for r in regions), mean_confidence=mean,
        status="empty" if not regions else "low_confidence" if mean < min_confidence else "ok",
        warnings=notes)

