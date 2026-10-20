"""Original six-section mock OCR: deterministic geometry, no OCR or HTTP."""

from copy import deepcopy
import json
from pathlib import Path
import random
from unittest.mock import AsyncMock, Mock

import pytest

from app.schemas import OCRText
from app.services.lesson import LessonParserConfig, group_lines, parse_lesson

FIXTURE = Path(__file__).parent / "fixtures" / "lesson_ocr_original_six_sections.json"
FIELDS = ("expression", "korean_meaning", "korean_explanation", "english_dialogue",
    "korean_dialogue_translation", "vocabulary_notes")


@pytest.fixture(autouse=True)
def forbid_engine_and_network(monkeypatch):
    initialize = Mock(side_effect=AssertionError("Synthetic fixtures must not initialize OCR"))
    async_http = AsyncMock(side_effect=AssertionError("Synthetic fixtures must not request HTTP"))
    sync_http = Mock(side_effect=AssertionError("Synthetic fixtures must not request HTTP"))
    monkeypatch.setattr("app.services.ocr.OCRService._initialize_locked", initialize)
    monkeypatch.setattr("httpx.AsyncClient.send", async_http)
    monkeypatch.setattr("httpx.Client.send", sync_http)
    yield
    initialize.assert_not_called()
    async_http.assert_not_awaited()
    sync_http.assert_not_called()


@pytest.fixture
def lesson():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return fixture, OCRText.model_validate(fixture["ocr"])


def parse(fixture, ocr):
    return parse_lesson(ocr, fixture["image"]["width"], fixture["image"]["height"])


def test_mock_fixture_has_valid_boxes_and_complete_expected_reading_order(lesson):
    fixture, ocr = lesson
    assert ocr.engine == "mock"
    assert set(fixture["expected"]["fields"]) == set(FIELDS)
    indices = fixture["expected"]["reading_order"]
    assert sorted(indices) == list(range(len(ocr.regions)))
    assert indices != list(range(len(ocr.regions)))  # Deliberately shuffled input.
    assert indices == [index for line in fixture["expected"]["lines"] for index in line["region_indices"]]
    for region in ocr.regions:
        box = region.bounding_box
        assert box.x + box.width <= fixture["image"]["width"]
        assert box.y + box.height <= fixture["image"]["height"]


def test_reading_order_and_combined_text_match_explicit_expectations(lesson):
    fixture, ocr = lesson
    lines = group_lines(ocr, LessonParserConfig())
    assert [{"text": line.text, "region_indices": line.indices} for line in lines] == fixture["expected"]["lines"]
    assert "\n".join(line.text for line in lines) == ocr.raw_text
    assert [index for line in lines for index in line.indices] == fixture["expected"]["reading_order"]


def test_all_six_original_fields_preserve_text_provenance_and_raw_ocr(lesson):
    fixture, ocr = lesson
    before = deepcopy(ocr.model_dump(mode="json"))
    result = parse(fixture, ocr)
    assert result.status == fixture["expected"]["status"] == "structured"
    for name, expected in fixture["expected"]["fields"].items():
        field = getattr(result.fields, name)
        assert field is not None
        assert field.text == expected["text"]
        assert field.region_indices == expected["region_indices"]
    assert result.fields.source_credit is fixture["expected"]["source_credit"] is None
    assert result.unassigned_region_indices == fixture["expected"]["unassigned_region_indices"] == []
    assert ocr.model_dump(mode="json") == before


@pytest.mark.parametrize("order", ["reversed", "rotated", "shuffled"])
def test_parser_uses_boxes_instead_of_input_region_order(lesson, order):
    fixture, ocr = lesson
    expected_tokens = {name: [ocr.regions[index].text for index in expected["region_indices"]]
        for name, expected in fixture["expected"]["fields"].items()}
    if order == "reversed":
        ocr.regions.reverse()
    elif order == "rotated":
        ocr.regions = ocr.regions[7:] + ocr.regions[:7]
    else:
        random.Random(17).shuffle(ocr.regions)
    result = parse(fixture, ocr)
    assert result.status == "structured"
    for name, expected in fixture["expected"]["fields"].items():
        field = getattr(result.fields, name)
        assert field.text == expected["text"]
        assert [ocr.regions[index].text for index in field.region_indices] == expected_tokens[name]


@pytest.mark.parametrize("name", FIELDS)
def test_uncertain_section_is_optional_without_affecting_other_sections(lesson, name):
    fixture, ocr = lesson
    index = fixture["expected"]["fields"][name]["region_indices"][0]
    ocr.regions[index].confidence = 0.2
    ocr.regions[index].low_confidence = True
    before = ocr.model_dump(mode="json")
    result = parse(fixture, ocr)
    assert result.status == "partial"
    assert getattr(result.fields, name) is None
    assert index in result.unassigned_region_indices
    for other in FIELDS:
        if other != name:
            assert getattr(result.fields, other).text == fixture["expected"]["fields"][other]["text"]
    assert ocr.model_dump(mode="json") == before


def test_raw_text_without_supporting_boxes_cannot_invent_vocabulary(lesson):
    fixture, ocr = lesson
    missing = set(fixture["expected"]["fields"]["vocabulary_notes"]["region_indices"])
    ocr.regions = [region for index, region in enumerate(ocr.regions) if index not in missing]
    # Combined text still contains the notes, but geometry no longer supports them.
    assert "blue box:" in ocr.raw_text
    result = parse(fixture, ocr)
    assert result.status == "partial" and result.fields.vocabulary_notes is None
    assert result.fields.expression.text == fixture["expected"]["fields"]["expression"]["text"]
