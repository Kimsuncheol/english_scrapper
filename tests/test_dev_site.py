"""Opt-in local fixture navigation and the actual pure parser, without I/O."""

from html.parser import HTMLParser
from io import BytesIO
import json
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.main import create_app

PREFIX = "/_dev/test-site"
SLUGS = ["one-tall-image", "thumbnail-and-tall", "no-lesson-image", "malformed-data"]


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.images = [], []
        self.in_debug = False
        self.debug = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag == "img":
            self.images.append(attrs)
        if tag == "pre" and attrs.get("id") == "parser-debug":
            self.in_debug = True

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_debug = False

    def handle_data(self, data):
        if self.in_debug:
            self.debug.append(data)

    @property
    def result(self):
        return json.loads("".join(self.debug))


@pytest.fixture
def local_site(monkeypatch):
    monkeypatch.setenv("ENABLE_TEST_SITE", "true")
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    network = AsyncMock(side_effect=AssertionError("Fixtures must not fetch HTTP"))
    initialize = Mock(side_effect=AssertionError("Fixtures must not initialize OCR"))
    database = AsyncMock(side_effect=AssertionError("Fixtures must not access PostgreSQL"))
    monkeypatch.setattr("httpx.AsyncClient.send", network)
    monkeypatch.setattr("app.services.ocr.ocr_service.initialize", initialize)
    monkeypatch.setattr("app.services.storage.PostgresStore.connect", database)
    with TestClient(create_app()) as client:
        yield client
    network.assert_not_awaited()
    initialize.assert_not_called()
    database.assert_not_awaited()


@pytest.mark.parametrize("enabled", [None, "false"])
def test_development_routes_do_not_exist_by_default(monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("ENABLE_TEST_SITE", raising=False)
    else:
        monkeypatch.setenv("ENABLE_TEST_SITE", enabled)
    client = TestClient(create_app())
    for path in [PREFIX + "/", PREFIX + "/articles/one-tall-image", PREFIX + "/assets/lesson.png"]:
        assert client.get(path).status_code == 404
    assert not any(getattr(route, "path", "").startswith(PREFIX) for route in client.app.routes)


def test_invalid_flag_fails_closed(monkeypatch):
    monkeypatch.setenv("ENABLE_TEST_SITE", "maybe")
    with pytest.raises(ValueError, match="ENABLE_TEST_SITE"):
        create_app()


def test_index_links_to_every_fixture_and_hides_debug_routes_from_schema(local_site):
    response = local_site.get(PREFIX + "/")
    assert response.status_code == 200
    assert "DEVELOPMENT ONLY" in response.text
    page = Page(response.text)
    for slug in SLUGS:
        assert f"{PREFIX}/articles/{slug}" in page.links
    assert all(link.startswith(PREFIX) for link in page.links)
    assert not any(path.startswith(PREFIX) for path in local_site.get("/openapi.json").json()["paths"])


@pytest.mark.parametrize("slug,index", [("one-tall-image", 0), ("thumbnail-and-tall", 1)])
def test_article_debug_shows_real_metadata_and_tall_selection(local_site, slug, index):
    response = local_site.get(f"{PREFIX}/articles/{slug}")
    assert response.status_code == 200
    page = Page(response.text)
    data = page.result
    assert data["status"] == "ok"
    assert data["article"]["url"] == f"https://www.chosun.com/test-fixtures/{slug}/"
    assert data["article"]["language"] == "ko"
    assert data["article"]["section"] == "Fixture English"
    assert data["article"]["published_at"] == "2026-10-21T06:00:00+09:00"
    assert data["lesson_image"]["url"].endswith("/lesson.png")
    assert (data["lesson_image"]["width"], data["lesson_image"]["height"]) == (720, 1800)
    assert not data["lesson_image"]["is_fallback"]
    assert data["lesson_image"]["content_element_path"] == [index]
    assert data["lesson_image"]["resized_urls"]["small"].endswith("/lesson-small.png")
    assert len(data["article_images"]) == index + 1
    assert data["json_ld_documents"] == 1 and data["fusion_global_content_parsed"]
    for image in page.images:
        assert image["src"].startswith(PREFIX + "/assets/")
        asset = local_site.get(image["src"])
        assert asset.status_code == 200
        assert asset.headers["content-type"] == "image/png"
        with Image.open(BytesIO(asset.content)) as decoded:
            assert decoded.size == (int(image["width"]), int(image["height"]))


@pytest.mark.parametrize("slug,code", [("no-lesson-image", "no_lesson_image"), ("malformed-data", "invalid_article_data")])
def test_error_fixtures_still_render_metadata_and_concise_parser_error(local_site, slug, code):
    response = local_site.get(f"{PREFIX}/articles/{slug}")
    assert response.status_code == 200  # The fixture page is valid; its data intentionally fails.
    page = Page(response.text)
    assert page.result["status"] == "error"
    assert page.result["error"]["code"] == code
    assert page.result["lesson_image"] is None and not page.images
    assert page.result["article"]["title"]
    if slug == "malformed-data":
        assert not page.result["fusion_global_content_parsed"]
        assert page.result["json_ld_documents"] == 0
    else:
        assert page.result["fusion_global_content_parsed"]
        assert page.result["json_ld_documents"] == 1


def test_local_assets_security_headers_and_unknown_paths(local_site):
    response = local_site.get(PREFIX + "/")
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "img-src 'self'" in policy
    assert "style-src 'self'" in policy and "unsafe-inline" not in policy
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert local_site.get(PREFIX + "/assets/style.css").status_code == 200
    small = local_site.get(PREFIX + "/assets/lesson-small.png")
    with Image.open(BytesIO(small.content)) as image:
        assert image.size == (240, 600)
    for path in ["/assets/.env", "/assets/missing.png", "/articles/unknown", "/assets/%2e%2e%2fmain.py"]:
        assert local_site.get(PREFIX + path).status_code == 404


def test_test_site_does_not_relax_manual_api_url_validation(local_site):
    response = local_site.post("/api/v1/scrape", json={"article_url": "http://127.0.0.1:8000" + PREFIX + "/articles/one-tall-image"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
