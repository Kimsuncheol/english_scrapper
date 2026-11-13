"""Original synthetic lesson text; no publisher content or network needed."""
from app.schemas import BoundingBox, OCRRegion, OCRText
from app.services.lesson import parse_lesson


def lesson():
    texts = [
        ('Try another way', 100, 40),
        ('다른 방법을 시도하다', 160, 25),
        ('새로운 방법을 이야기할 때 쓰는 표현이다.', 240, 22),
        ('상황에 맞게 사용할 수 있다.', 275, 22),
        ('A This puzzle is tricky.', 370, 22),
        ('Can you help?', 405, 22),
        ('B Try another way.', 450, 22),
        ('A 이 퍼즐 어렵다.', 550, 22),
        ('도와줄 수 있어?', 585, 22),
        ('B 다른 방법을 시도해 봐.', 630, 22),
        ('puzzle 퍼즐/tricky 까다로운', 710, 22),
        ('(YBM 가상 저자)', 760, 22),
        ('광고 문의 02)0000-0000', 850, 22),
    ]
    return OCRText(raw_text='\n'.join(t for t, _, _ in texts), engine='paddleocr', regions=[
        OCRRegion(text=t, confidence=.98, bounding_box=BoundingBox(
            x=150 if i==0 else 20, y=y, width=400 if i==0 else 550, height=h))
        for i, (t, y, h) in enumerate(texts)])


def parse(ocr, title='[스피킹이 강해지는 영어] Try another way'):
    return parse_lesson(ocr, 700, 1000, article_title=title)


def test_verified_bilingual_layout_organizes_without_altering_source():
    ocr = lesson()
    original = ocr.model_dump()
    result = parse(ocr)
    assert result.method == 'series_bilingual_layout_v1'
    assert all(field is not None for _, field in result.fields)
    assert result.fields.english_dialogue.text == 'A This puzzle is tricky. Can you help?\nB Try another way.'
    assert result.fields.korean_dialogue_translation.text == 'A 이 퍼즐 어렵다. 도와줄 수 있어?\nB 다른 방법을 시도해 봐.'
    assert result.fields.english_dialogue.region_indices == [4, 5, 6]
    assert 12 in result.unassigned_region_indices
    assert '광고' not in '\n'.join(f.text for _, f in result.fields if f)
    assert ocr.model_dump() == original


def test_unverified_heading_or_mismatched_speakers_stays_unassigned():
    assert parse(lesson(), title='Unrelated article').method != 'series_bilingual_layout_v1'
    ocr = lesson()
    ocr.regions[9].text = 'A 다른 방법을 시도해 봐.'
    assert parse(ocr).fields.english_dialogue is None


def test_uncertain_content_is_not_promoted_to_a_field():
    ocr = lesson()
    ocr.regions[5].low_confidence = True
    result = parse(ocr)
    assert result.fields.english_dialogue is None
    assert 5 in result.unassigned_region_indices
    assert result.fields.korean_meaning is not None
