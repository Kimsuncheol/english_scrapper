"""Conservative lesson classification using labels, geometry and reading order.

The original OCR object is never mutated. No semantic completion, translation,
or punctuation correction is performed. Unknown layouts stay unassigned.
"""

from dataclasses import dataclass
import math
import re
from statistics import median

from app.schemas import BoundingBox, LessonField, LessonFields, LessonParsingResult, OCRText

DEFAULT_LABELS = (
    ("expression", ("expression", "오늘의 표현", "핵심 표현", "표현")),
    ("korean_meaning", ("korean meaning", "meaning", "우리말 뜻", "뜻", "의미")),
    ("korean_explanation", ("korean explanation", "explanation", "표현 설명", "설명", "해설")),
    ("english_dialogue", ("english dialogue", "dialogue", "영어 대화", "대화")),
    ("korean_dialogue_translation", ("korean dialogue translation", "translation", "대화 해석", "우리말 대화", "번역", "해석")),
    ("vocabulary_notes", ("vocabulary notes", "vocabulary", "words & phrases", "어휘 노트", "어휘", "단어")),
    ("source_credit", ("source credit", "source", "credit", "출처", "제공")),
)
SPEAKER = re.compile(r"^\s*[A-Z]\s*[:：.)-]\s*", re.IGNORECASE)
HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class LessonParserConfig:
    labels: tuple = DEFAULT_LABELS
    min_confidence: float = 0.6
    heading_confidence: float = 0.75
    min_vertical_overlap: float = 0.5
    max_word_gap_heights: float = 3.0
    column_gap_ratio: float = 0.22
    max_section_gap_heights: float = 3.0
    max_regions: int = 5000

    def __post_init__(self):
        for value in (self.min_confidence, self.heading_confidence, self.min_vertical_overlap, self.column_gap_ratio):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("Lesson confidence/overlap/column thresholds must be in (0, 1]")
        if any(not math.isfinite(value) or value <= 0 for value in (self.max_word_gap_heights, self.max_section_gap_heights)) or self.max_regions <= 0:
            raise ValueError("Lesson resource and gap limits must be positive")
        allowed = set(LessonFields.model_fields)
        if any(name not in allowed or not labels for name, labels in self.labels):
            raise ValueError("Labels must refer to supported lesson fields")


@dataclass
class Line:
    indices: list[int]
    box: BoundingBox
    text: str = ""
    heading: str | None = None
    inline: str = ""


def union_box(boxes):
    x, y = min(box.x for box in boxes), min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return BoundingBox(x=x, y=y, width=right - x, height=bottom - y)


def group_lines(ocr: OCRText, config: LessonParserConfig) -> list[Line]:
    """Cluster only vertically aligned, horizontally adjacent words.

    Large horizontal gaps remain separate, preventing column interleaving.
    Indices into the original region list survive geometric reordering.
    """
    lines = []
    for index in sorted(range(len(ocr.regions)), key=lambda i: (
        ocr.regions[i].bounding_box.y, ocr.regions[i].bounding_box.x, i
    )):
        region = ocr.regions[index]
        box = region.bounding_box
        best = None
        for line in reversed(lines):
            if box.y - (line.box.y + line.box.height) > box.height:
                break
            overlap = min(box.y + box.height, line.box.y + line.box.height) - max(box.y, line.box.y)
            gap = max(0, box.x - (line.box.x + line.box.width), line.box.x - (box.x + box.width))
            if overlap / min(box.height, line.box.height) >= config.min_vertical_overlap and gap <= config.max_word_gap_heights * min(box.height, line.box.height):
                best = line
                break
        if best is None:
            lines.append(Line(indices=[index], box=box))
        else:
            best.indices.append(index)
            best.box = union_box([best.box, box])
    for line in lines:
        line.indices.sort(key=lambda i: (ocr.regions[i].bounding_box.x, i))
        line.text = " ".join(ocr.regions[i].text for i in line.indices).strip()
    return sorted(lines, key=lambda line: (line.box.y, line.box.x, min(line.indices)))


def identify_heading(line: Line, config: LessonParserConfig):
    for name, aliases in config.labels:
        for alias in sorted(aliases, key=len, reverse=True):
            # Whole headings or colon-delimited inline labels only; never match
            # incidental words such as "this expression means ..." in prose.
            match = re.fullmatch(re.escape(alias) + r"\s*(?:[:：]\s*(.*))?", line.text, re.IGNORECASE | re.DOTALL)
            if match:
                line.heading, line.inline = name, (match.group(1) or "").strip()
                return


def acceptable_language(name: str, texts: list[str]) -> bool:
    stripped = [SPEAKER.sub("", value) for value in texts]
    text = " ".join(stripped)
    korean, english = len(HANGUL.findall(text)), len(LATIN.findall(text))
    if name in {"expression", "english_dialogue"}:
        return english >= 2 and korean == 0 and (
            name != "english_dialogue" or any(SPEAKER.match(value) for value in texts)
        )
    if name in {"korean_meaning", "korean_explanation", "korean_dialogue_translation"}:
        threshold = 0.25 if name == "korean_explanation" else 0.65
        return korean >= 2 and korean / max(1, korean + english) >= threshold and (
            name != "korean_dialogue_translation" or all(HANGUL.search(value) or not LATIN.search(value) for value in stripped)
        )
    return korean + english >= 2


def parse_lesson(ocr: OCRText, image_width: int | None = None, image_height: int | None = None,
                 config: LessonParserConfig | None = None) -> LessonParsingResult:
    config = config or LessonParserConfig()
    all_indices = list(range(len(ocr.regions)))
    if not ocr.regions:
        return LessonParsingResult(status="unparsed" if ocr.raw_text.strip() else "empty",
            reasons=["No OCR bounding boxes are available; raw combined text is preserved without classification."])
    if len(ocr.regions) > config.max_regions:
        return LessonParsingResult(status="unparsed", unassigned_region_indices=all_indices,
            reasons=["OCR region count exceeds the configured layout-processing limit."])
    if any((image_width is not None and r.bounding_box.x + r.bounding_box.width > image_width)
           or (image_height is not None and r.bounding_box.y + r.bounding_box.height > image_height) for r in ocr.regions):
        return LessonParsingResult(status="unparsed", unassigned_region_indices=all_indices,
            reasons=["OCR bounding boxes exceed the lesson image; layout classification is unsafe."])
    lines = group_lines(ocr, config)
    for line in lines:
        identify_heading(line, config)
    anchors = [line for line in lines if line.heading]
    if not anchors:
        return LessonParsingResult(status="unparsed", unassigned_region_indices=all_indices,
            reasons=["No supported section labels were found. Unlabeled titles, meanings, and dialogues are ambiguous."])

    # Cluster heading left edges to discover columns, rather than merging all
    # text at a shared Y coordinate into a single reading stream.
    width = image_width or max(line.box.x + line.box.width for line in lines)
    starts = []
    for anchor in sorted(anchors, key=lambda line: line.box.x):
        if not starts or anchor.box.x - starts[-1] > width * config.column_gap_ratio:
            starts.append(anchor.box.x)
    columns = [[] for _ in starts]
    reasons = []
    height = median(region.bounding_box.height for region in ocr.regions)
    for line in lines:
        options = [i for i, left in enumerate(starts) if line.box.x >= left - height
                   and (i == len(starts) - 1 or line.box.x + line.box.width <= starts[i + 1] - height / 2)]
        if options:
            column = min(options, key=lambda i: abs(line.box.x - starts[i]))
            columns[column].append(line)
        elif line.text:
            if line.heading:
                return LessonParsingResult(status="unparsed", unassigned_region_indices=all_indices,
                    reasons=["A section label crosses column boundaries; reliable section ownership is unavailable."])
            reasons.append("Some OCR lines cross column boundaries or have unsupported alignment.")

    sections = {}
    for column in columns:
        current = None
        for line in column:
            if line.heading:
                current = {"anchor": line, "lines": [], "texts": [], "last_bottom": line.box.y + line.box.height}
                sections.setdefault(line.heading, []).append(current)
                if line.inline:
                    current["lines"].append(line)
                    current["texts"].append(line.inline)
            elif current is not None and line.text:
                if (re.fullmatch(r".{2,50}[:：]", line.text) and not SPEAKER.match(line.text)) or line.text.startswith("©"):
                    reasons.append("An unsupported section label or credit marker was left unassigned.")
                    current = None
                    continue
                gap = line.box.y - current["last_bottom"]
                if gap > height * config.max_section_gap_heights:
                    reasons.append(f"{current['anchor'].heading}: detached text was left unassigned because of a large layout gap.")
                    current = None
                    continue
                current["lines"].append(line)
                current["texts"].append(line.text)
                current["last_bottom"] = line.box.y + line.box.height

    fields = {}
    assigned = set()
    for name, candidates in sections.items():
        if len(candidates) != 1:
            reasons.append(f"{name}: repeated section labels are ambiguous; no field was assigned.")
            continue
        section = candidates[0]
        anchor = section["anchor"]
        lines_for_field = section["lines"]
        indices = list(dict.fromkeys(i for line in lines_for_field for i in line.indices))
        anchor_indices = anchor.indices
        if not indices:
            reasons.append(f"{name}: section label has no nearby content.")
            continue
        if any(ocr.regions[i].low_confidence or ocr.regions[i].confidence < config.heading_confidence for i in anchor_indices):
            reasons.append(f"{name}: section label has insufficient OCR confidence.")
            continue
        if any(ocr.regions[i].low_confidence or ocr.regions[i].confidence < config.min_confidence for i in indices):
            reasons.append(f"{name}: content has insufficient OCR confidence; raw regions were retained.")
            continue
        if not acceptable_language(name, section["texts"]):
            reasons.append(f"{name}: language or speaker evidence conflicts with the labeled section.")
            continue
        fields[name] = LessonField(text="\n".join(section["texts"]), region_indices=indices,
            confidence=min(ocr.regions[i].confidence for i in set(indices + anchor_indices)),
            bounding_box=union_box([ocr.regions[i].bounding_box for i in indices]))
        assigned.update(indices + anchor_indices)
    unassigned = sorted(set(all_indices) - assigned)
    if unassigned:
        reasons.append("Some regions could not be classified reliably; consult the unchanged raw OCR.")
    return LessonParsingResult(status=("structured" if not unassigned and not reasons else "partial") if fields else "unparsed",
        fields=LessonFields(**fields), reasons=list(dict.fromkeys(reasons)), unassigned_region_indices=unassigned)
