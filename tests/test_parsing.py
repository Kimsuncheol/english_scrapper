import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.services.article import FetchedArticle, validate_article_html
from app.services.errors import ArticleFetchError
from app.services.parsing import (
    ImageSelectionConfig, fusion_content, parse_article_html,
)
from app.services import scrape

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.chosun.com/example/"


def sample(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_jsonld_metadata_and_full_lesson_image():
    result = parse_article_html(sample("fusion_lesson.html"), URL)
    assert result.article.title == "오늘의 영어: Hello {world}"
    assert str(result.article.url).endswith("/culture/english/2026/10/20/LESSON/")
    assert result.article.published_at.isoformat() == "2026-10-20T06:00:00+09:00"
    assert result.article.section == "Education"
    assert result.article.language == "ko"
    assert str(result.lesson_image.url) == "https://images.example.test/lesson.jpg"
    assert (result.lesson_image.width, result.lesson_image.height) == (1200, 3600)
    assert result.lesson_image.alt_text == "영어 학습 그래픽"
    assert set(result.lesson_image.resized_urls) == {"small", "large", "resizeUrl", "fullSizeResizeUrl"}
    assert result.lesson_image.content_element_path == [2]
    assert result.lesson_image.source == "content_elements"
    assert not result.lesson_image.is_fallback
    assert len(result.article_images) == 3


def test_fusion_metadata_original_url_and_nested_structure():
    html = sample("fusion_only.html")
    result = parse_article_html(html, URL)
    assert result.article.title == "페이지 데이터 영어"
    assert str(result.article.url) == "https://www.chosun.com/education/english/LESSON/"
    assert result.article.published_at.isoformat() == "2026-10-19T23:00:00+00:00"
    assert result.article.language == "ko-KR"
    assert result.article.section == "영어"
    assert result.lesson_image.content_element_path == [0, 0]
    assert str(result.lesson_image.url).endswith("/original.jpg")
    assert result.lesson_image.width == 1000
    assert set(result.lesson_image.resized_urls) == {"medium"}
    assert validate_article_html(URL, html).title == "페이지 데이터 영어"


def test_og_fallback_is_explicit():
    result = parse_article_html(sample("og_fallback.html"), URL)
    assert result.lesson_image.is_fallback
    assert result.lesson_image.source == "og:image"
    assert str(result.lesson_image.url) == "https://www.chosun.com/media/fallback.jpg"
    assert result.lesson_image.width == 1200
    assert len(result.article_images) == 1


@pytest.mark.parametrize("filename", ["no_image.html", "malformed_fusion.html"])
def test_no_lesson_image_has_explicit_error(filename):
    with pytest.raises(ArticleFetchError) as error:
        parse_article_html(sample(filename), URL)
    assert error.value.code == "no_lesson_image"
    assert error.value.status_code == 422


def test_fallback_can_be_disabled_and_thresholds_configured():
    html = sample("og_fallback.html")
    with pytest.raises(ArticleFetchError):
        parse_article_html(html, URL, ImageSelectionConfig(allow_og_fallback=False))
    result = parse_article_html(html, URL, ImageSelectionConfig(min_width=20, min_height=20))
    assert not result.lesson_image.is_fallback
    with pytest.raises(ValueError):
        ImageSelectionConfig(tall_aspect_ratio=0)


def html_with_elements(elements):
    data = {"type": "story", "headlines": {"basic": "Lesson"}, "content_elements": elements}
    return '<script>Fusion.globalContent = ' + json.dumps(data) + ';</script>'


def test_article_order_breaks_ties():
    images = [{"type": "image", "url": f"https://images.example.test/{name}.jpg", "width": 800, "height": 1600} for name in ["first", "second"]]
    result = parse_article_html(html_with_elements(images), URL)
    assert str(result.lesson_image.url).endswith("first.jpg")


def test_unknown_dimensions_do_not_force_thumbnail_fallback():
    html = html_with_elements([{"type": "image", "url": "https://images.example.test/original.jpg", "width": False, "height": "bad"}])
    result = parse_article_html(html, URL)
    assert result.lesson_image.width is None
    assert result.lesson_image.height is None
    assert not result.lesson_image.is_fallback
    with pytest.raises(ArticleFetchError):
        parse_article_html(html, URL, ImageSelectionConfig(allow_unknown_dimensions=False))


@pytest.mark.parametrize("expression", [
    'alert("unsafe")', '({content_elements: []})', '{"content_elements": []} + attack()',
    '{"content_elements": attack()}', '{"content_elements": undefined}',
])
def test_fusion_does_not_interpret_javascript(expression):
    assert fusion_content("Fusion.globalContent = " + expression + ";") == {}


def test_json_parse_wrapper_and_quoted_braces():
    data = {"headlines": {"basic": 'quoted " } ; {'}, "content_elements": []}
    script = "/* ignore { } */ Fusion.globalContent = JSON.parse(" + json.dumps(json.dumps(data)) + "); next();"
    assert fusion_content(script) == data
    assert fusion_content("Fusion.globalContent = " + json.dumps(data) + ";") == data


def test_comments_and_strings_are_not_assignments():
    fake = 'Fusion.globalContent = {"content_elements": []};'
    assert fusion_content("// " + fake) == {}
    assert fusion_content("var s = " + json.dumps(fake) + ";") == {}
    assert fusion_content("/* " + fake + " */") == {}
    assert fusion_content("var s = `" + fake + "`;") == {}


def test_invalid_image_fields_and_urls_are_skipped():
    html = html_with_elements([None, "bad", {"type": "image", "url": "javascript:alert(1)"}, {"type": "image", "url": "https://127.0.0.1/a"}, {"type": "image", "url": "https://images.example.test/a.jpg", "resized_urls": {"bad": "file:///a", "good": {"url": "https://images.example.test/small.jpg"}}}])
    result = parse_article_html(html, URL)
    assert len(result.article_images) == 1
    assert set(result.lesson_image.resized_urls) == {"good"}


def test_metadata_invalid_values_fall_back_without_schema_error():
    html = sample("fusion_only.html").replace('"publish_date":"2026-10-19T23:00:00Z"', '"publish_date":"invalid"').replace('"canonical_url":"/education/english/LESSON/"', '"canonical_url":"https://evil.example/a"')
    result = parse_article_html(html, URL)
    assert result.article.published_at is None
    assert str(result.article.url) == URL


@pytest.mark.parametrize("value", ["https://[::1]/a", "https://[::ffff:127.0.0.1]/a", "https://localhost/a", "file:///tmp/a"])
def test_unsafe_image_metadata_is_not_selected(value):
    with pytest.raises(ArticleFetchError) as error:
        parse_article_html(html_with_elements([{"type": "image", "url": value}]), URL)
    assert error.value.code == "no_lesson_image"


def test_invalid_fusion_can_fall_back_without_executing_javascript():
    html = sample("malformed_fusion.html").replace("</head>", '<meta property="og:image" content="https://images.example.test/fallback.jpg"></head>')
    assert parse_article_html(html, URL).lesson_image.is_fallback


def test_malformed_nested_shapes_do_not_crash():
    malformed = html_with_elements([{"type": "gallery", "content_elements": {}}, {"type": "image", "url": "https://images.example.test/good.jpg", "additional_properties": [], "resized_urls": []}])
    assert len(parse_article_html(malformed, URL).article_images) == 1


def test_tall_preference_can_be_disabled():
    images = [
        {"type": "image", "url": "https://images.example.test/tall.jpg", "width": 500, "height": 1500},
        {"type": "image", "url": "https://images.example.test/wide.jpg", "width": 2000, "height": 1000},
    ]
    html = html_with_elements(images)
    assert str(parse_article_html(html, URL).lesson_image.url).endswith("tall.jpg")
    assert str(parse_article_html(html, URL, ImageSelectionConfig(prefer_tall_images=False)).lesson_image.url).endswith("wide.jpg")


def test_jsonld_article_matches_canonical_instead_of_related_story():
    html = sample("fusion_lesson.html").replace(
        '{"@type":"Organization","name":"Publisher"}',
        '{"@type":"NewsArticle","headline":"Related story","url":"https://www.chosun.com/other/"}'
    )
    assert parse_article_html(html, URL).article.title == "오늘의 영어: Hello {world}"


def test_parse_api_returns_metadata_and_no_ocr(monkeypatch, mock_image_download):
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(
        url=URL, html=sample("fusion_lesson.html"), title="Lesson"
    )))
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": URL})
    assert response.status_code == 200
    assert response.json()["ocr"] is None
    assert response.json()["article"]["section"] == "Education"
    assert len(response.json()["article_images"]) == 3


def test_parse_api_no_image_error(monkeypatch):
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(
        url=URL, html=sample("no_image.html"), title="No graphic"
    )))
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": URL})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_lesson_image"
