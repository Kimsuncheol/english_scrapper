from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.schemas import OCRRegion, OCRText
from app.services import scrape
from app.services.article import FetchedArticle
from app.services.lesson import LessonParserConfig, parse_lesson

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    fixture = json.loads((FIXTURES / name).read_text())
    return OCRText.model_validate(fixture["ocr"]), fixture["image"]


def parse(ocr, image, config=None):
    return parse_lesson(ocr, image["width"], image["height"], config)


def test_single_column_all_fields_punctuation_and_raw_preservation():
    ocr, image = load("lesson_ocr_single_column.json")
    before = deepcopy(ocr.model_dump())
    result = parse(ocr, image)
    assert result.status == "structured", result.reasons
    fields = result.fields
    assert fields.expression.text == "Don't sweat it."
    assert fields.korean_meaning.text == "걱정하지 마."
    assert fields.korean_explanation.text == "상대방을 안심시킬 때 쓰는 표현입니다.\n작은 실수는 괜찮다는 뜻으로도 사용합니다."
    assert fields.english_dialogue.text == "A: I’m sorry I’m late.\nThe bus didn’t come on time.\nB: Don't sweat it!"
    assert fields.korean_dialogue_translation.text == "A: 늦어서 미안해.\n버스가 제시간에 오지 않았어.\nB: 걱정하지 마!"
    assert fields.vocabulary_notes.text == "sweat: 땀을 흘리다\non time: 제시간에"
    assert fields.source_credit.text == "Chosun English lesson"
    assert fields.expression.confidence == 0.97
    assert len(fields.expression.region_indices) == 3
    assert not result.unassigned_region_indices
    assert ocr.model_dump() == before


def test_two_columns_do_not_interleave_dialogues():
    ocr, image = load("lesson_ocr_two_columns.json")
    result = parse(ocr, image)
    assert result.status == "structured", result.reasons
    assert result.fields.expression.text == "It's up to you."
    assert result.fields.english_dialogue.text == "A: What should we do?\nShould we stay here?\nB: It’s up to you."
    assert result.fields.korean_dialogue_translation.text == "A: 무엇을 할까?\n여기에 있을까?\nB: 네가 결정해."
    assert result.fields.source_credit is None
    assert result.fields.english_dialogue.bounding_box.x == 540


def test_unknown_unlabeled_layout_is_not_guessed():
    ocr, image = load("lesson_ocr_ambiguous.json")
    result = parse(ocr, image)
    assert result.status == "unparsed"
    assert all(value is None for value in result.fields.model_dump().values())
    assert result.unassigned_region_indices == list(range(len(ocr.regions)))
    assert "ambiguous" in " ".join(result.reasons)


def test_same_text_moved_away_from_heading_is_not_assigned():
    ocr, image = load("lesson_ocr_single_column.json")
    baseline = parse(ocr, image)
    assert baseline.fields.korean_meaning is not None
    for region in ocr.regions:
        if region.text == "걱정하지 마.":
            region.bounding_box.y = 1000
    moved = parse(ocr, image)
    assert moved.fields.korean_meaning is None
    assert moved.status == "partial"


def test_geometric_reading_order_and_original_region_provenance():
    ocr, image = load("lesson_ocr_single_column.json")
    ocr.regions.reverse()
    result = parse(ocr, image)
    assert result.fields.expression.text == "Don't sweat it."
    assert [ocr.regions[i].text for i in result.fields.expression.region_indices] == ["Don't", "sweat", "it."]


def test_low_confidence_field_is_unset_but_other_fields_remain():
    ocr, image = load("lesson_ocr_single_column.json")
    for region in ocr.regions:
        if region.text == "걱정하지 마.":
            region.confidence = 0.3
            region.low_confidence = True
    result = parse(ocr, image)
    assert result.status == "partial"
    assert result.fields.korean_meaning is None
    assert result.fields.expression is not None
    assert any("insufficient OCR confidence" in reason for reason in result.reasons)


def test_uncertain_anchor_is_a_boundary_not_part_of_preceding_field():
    ocr, image = load("lesson_ocr_single_column.json")
    for region in ocr.regions:
        if region.text == "Korean meaning":
            region.confidence = 0.2
    result = parse(ocr, image)
    assert result.fields.korean_meaning is None
    assert result.fields.expression.text == "Don't sweat it."


def test_language_conflict_prevents_false_assignment():
    ocr, image = load("lesson_ocr_single_column.json")
    for region in ocr.regions:
        if region.text == "걱정하지 마.":
            region.text = "Unrelated English title"
    assert parse(ocr, image).fields.korean_meaning is None


def test_duplicate_section_labels_are_ambiguous():
    ocr, image = load("lesson_ocr_single_column.json")
    ocr.regions.append(OCRRegion(text="Expression", confidence=0.98,
        bounding_box={"x": 40, "y": 900, "width": 100, "height": 20}))
    result = parse(ocr, image)
    assert result.fields.expression is None
    assert result.status == "partial"
    assert any("repeated section" in reason for reason in result.reasons)


def test_empty_and_raw_text_without_regions_are_explained():
    assert parse_lesson(OCRText(raw_text="")).status == "empty"
    result = parse_lesson(OCRText(raw_text="Don't invent structure"))
    assert result.status == "unparsed"
    assert result.fields.expression is None
    assert "bounding boxes" in result.reasons[0]


def test_invalid_boxes_and_region_limit_degrade_to_raw():
    ocr, image = load("lesson_ocr_single_column.json")
    assert parse_lesson(ocr, image_width=20, image_height=20).status == "unparsed"
    assert parse(ocr, image, LessonParserConfig(max_regions=2)).status == "unparsed"


def test_all_fields_optional_when_only_expression_is_labeled():
    ocr = OCRText(raw_text="Expression: It's all right.", regions=[OCRRegion(
        text="Expression: It's all right.", confidence=0.98,
        bounding_box={"x": 20, "y": 20, "width": 350, "height": 25})])
    result = parse_lesson(ocr, 500, 200)
    assert result.status == "structured"
    assert result.fields.expression.text == "It's all right."
    assert result.fields.korean_meaning is None
    assert result.fields.english_dialogue is None


def test_labels_are_configurable():
    ocr = OCRText(raw_text="Key phrase: Let's go!", regions=[OCRRegion(
        text="Key phrase: Let's go!", confidence=0.98,
        bounding_box={"x": 20, "y": 20, "width": 350, "height": 25})])
    assert parse_lesson(ocr).status == "unparsed"
    config = LessonParserConfig(labels=(("expression", ("Key phrase",)),))
    assert parse_lesson(ocr, config=config).fields.expression.text == "Let's go!"


def test_mixed_unlabeled_translation_is_not_swallowed_by_expression():
    ocr = OCRText(raw_text="Expression\nDon't sweat it.\n걱정하지 마.", regions=[
        OCRRegion(text=text, confidence=0.98, bounding_box={"x": 20, "y": y, "width": 350, "height": 20})
        for text, y in [("Expression", 20), ("Don't sweat it.", 50), ("걱정하지 마.", 80)]
    ])
    result = parse_lesson(ocr, 500, 200)
    assert result.status == "unparsed"
    assert result.fields.expression is None
    assert result.fields.korean_meaning is None


def test_unknown_section_heading_stops_preceding_field():
    ocr = OCRText(raw_text="Vocabulary\nstay: 머무르다\nExercises:\nUnknown content", regions=[
        OCRRegion(text=text, confidence=0.98, bounding_box={"x": 20, "y": y, "width": 350, "height": 20})
        for text, y in [("Vocabulary", 20), ("stay: 머무르다", 50), ("Exercises:", 80), ("Unknown content", 110)]
    ])
    result = parse_lesson(ocr, 500, 200)
    assert result.status == "partial"
    assert result.fields.vocabulary_notes.text == "stay: 머무르다"
    assert result.unassigned_region_indices == [2, 3]


def test_ambiguous_wide_heading_does_not_reassign_its_content():
    ocr, image = load("lesson_ocr_two_columns.json")
    for region in ocr.regions:
        if region.text == "뜻":
            region.bounding_box.width = 650
    result = parse(ocr, image)
    assert result.status == "unparsed"
    assert result.fields.korean_meaning is None


def test_api_keeps_raw_output_for_unsupported_layout(monkeypatch, mock_image_download, persisted_store):
    ocr, image = load("lesson_ocr_ambiguous.json")
    html = '<script>Fusion.globalContent = {"content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":1000,"height":1050}]};</script>'
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(
        url="https://www.chosun.com/example/", html=html, title="Lesson")))
    monkeypatch.setattr(scrape, "recognize_image", AsyncMock(return_value=ocr))
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"})
    assert response.status_code == 200
    assert response.json()["ocr"] == ocr.model_dump(mode="json")
    assert response.json()["lesson"]["status"] == "unparsed"
    assert response.json()["lesson"]["reasons"]


def test_api_preserves_ocr_and_adds_lesson(monkeypatch, mock_image_download, persisted_store):
    ocr, image = load("lesson_ocr_single_column.json")
    before = ocr.model_dump(mode="json")
    html = '<script>Fusion.globalContent = {"content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":1000,"height":1050}]};</script>'
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(
        url="https://www.chosun.com/example/", html=html, title="Lesson")))
    monkeypatch.setattr(scrape, "recognize_image", AsyncMock(return_value=ocr))
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"})
    assert response.status_code == 200
    assert response.json()["ocr"] == before
    assert response.json()["lesson"]["fields"]["expression"]["text"] == "Don't sweat it."
