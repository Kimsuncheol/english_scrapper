"""Organize the verified publisher lesson layout without generating new text."""

import re

from app.schemas import LessonField, LessonFields, LessonParsingResult

TURN = re.compile(r"^([AB])(?:\s*[:：.)-]\s*|\s+|(?=[가-힣]))(\S.*)$")
HANGUL = re.compile(r"[가-힣]")


def organize_series(ocr, lines, expression, config):
    """Require a verified heading and matching bilingual speaker sequences.

    This handles a single vertical lesson, not arbitrary unlabeled documents.
    Every field references only original regions. Unrecognized material, including
    publisher mastheads and advertising, remains in unassigned raw OCR.
    """
    from app.services.lesson import union_box

    body = [line for line in lines if line.box.y >= expression.bounding_box.y + expression.bounding_box.height]
    if not body:
        return None
    starts = [(i, TURN.match(line.text)) for i, line in enumerate(body) if TURN.match(line.text)]
    en = [(i, match) for i, match in starts if not HANGUL.search(match[2])]
    ko = [(i, match) for i, match in starts if HANGUL.search(match[2])]
    if (len(en) < 2 or len(ko) < 2 or en[-1][0] >= ko[0][0]):
        return None
    matching_speakers = [m[1] for _, m in en] == [m[1] for _, m in ko]
    # Reject extra languages inside the English run rather than treating them
    # as dialogue continuations. Korean translations may contain English names.
    english = body[en[0][0]:ko[0][0]]
    if any(HANGUL.search(line.text) for line in english):
        return None
    source = next((i for i in range(ko[-1][0]+1, len(body))
                   if re.fullmatch(r"\([^()]*YBM[^()]*\)", body[i].text, re.IGNORECASE)), None)
    if source is None:
        return None
    # Large side-by-side gaps indicate a different layout; do not interleave it.
    if any(a.box.y + a.box.height > b.box.y and
           (a.box.x + a.box.width < b.box.x or b.box.x + b.box.width < a.box.x)
           for a, b in zip(body[:source], body[1:source])):
        return None
    # Vocabulary follows the final translated turn. A line containing an English
    # headword, a Korean gloss, and slash-separated entries provides a boundary.
    vocab = next((i for i in range(ko[-1][0]+1, source)
                  if re.match(r"^[a-z][a-z '-]+\s*[~~을를의가-힣]", body[i].text)
                  and "/" in body[i].text), None)
    if vocab is None:
        return None
    translated = body[ko[0][0]:vocab]
    before = body[:en[0][0]]
    if len(before) < 2 or not all(HANGUL.search(line.text) for line in before):
        return None
    # A separated short line immediately under the expression is its meaning.
    meaning = before[0]
    if (len(meaning.text) > 100 or before[1].box.y - meaning.box.y - meaning.box.height
            < meaning.box.height * 0.5):
        return None

    fields = {"expression": expression}

    def assign(name, selected, *, turns=False):
        indices = [i for line in selected for i in line.indices]
        if not indices or any(ocr.regions[i].low_confidence or
                              ocr.regions[i].confidence < config.min_confidence for i in indices):
            return
        paragraphs = []
        for line in selected:
            # Join only visual wraps; preserve the OCR's spelling and punctuation.
            if not paragraphs or (turns and TURN.match(line.text)):
                paragraphs.append(line.text)
            else:
                paragraphs[-1] += " " + line.text
        fields[name] = LessonField(text="\n".join(paragraphs), region_indices=indices,
            confidence=min(ocr.regions[i].confidence for i in indices),
            bounding_box=union_box([ocr.regions[i].bounding_box for i in indices]))

    assign("korean_meaning", [meaning])
    assign("korean_explanation", before[1:])
    if matching_speakers:
        assign("english_dialogue", english, turns=True)
    elif len(en) + 1 == len(ko):
        # A missed speaker marker must not hide otherwise readable English.
        # Preserve visual lines rather than inventing a turn or joining speakers.
        assign("english_dialogue", english, turns=True)
        if "english_dialogue" in fields:
            fields["english_dialogue"].text = "\n".join(line.text for line in english)
    assign("korean_dialogue_translation", translated, turns=True)
    assign("vocabulary_notes", body[vocab:source])
    assign("source_credit", [body[source]])
    assigned = {i for field in fields.values() for i in field.region_indices}
    return LessonParsingResult(status="partial", fields=LessonFields(**fields),
        unassigned_region_indices=[i for i in range(len(ocr.regions)) if i not in assigned],
        method="series_bilingual_layout_v1",
        reasons=["Sections follow the verified series layout and matching bilingual speaker order; review OCR text against the original image."])
