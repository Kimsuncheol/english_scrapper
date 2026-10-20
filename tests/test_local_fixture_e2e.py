"""Fixture-site HTTP → public API pipeline, with all network sockets forbidden.

Only pytest installs the loopback ASGI transport and synthetic image namespace.
Production URL validation is never changed, and no real server port is needed.
"""

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import json
from pathlib import Path
import socket
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app.dependencies import get_store
from app.dev_site import ASSETS, CASES, PREFIX, ROOT
from app.main import create_app
from app.schemas import OCRText, ScrapeResponse
from app.services import article, images, scrape
from test_daily import MemoryStore

PIN = "203.0.113.10"
ARTICLE_HOST = "www.chosun.com"
IMAGE_HOST = "fixture-images.example.test"
LOCAL_ORIGIN = "http://127.0.0.1:9876"  # ASGI address only; never a TCP connection.
MOCK_OCR = Path(__file__).parent / "fixtures" / "lesson_ocr_original_six_sections.json"


@dataclass
class LocalPipeline:
    fixture_app: object
    requests: list = field(default_factory=list)
    image_override: tuple | None = None
    client: TestClient | None = None
    ocr: object = None
    expected: dict = field(default_factory=dict)
    ocr_result: OCRText | None = None

    def article_url(self, slug):
        return f"https://{ARTICLE_HOST}/test-fixtures/{slug}/"


class FixtureTransport(httpx.AsyncBaseTransport):
    """No fallback: approved logical requests map only to real fixture routes."""
    def __init__(self, state):
        self.state = state

    async def handle_async_request(self, request):
        host = request.headers.get("host")
        assert request.method == "GET" and request.url.scheme == "https"
        assert request.url.host == PIN and request.extensions.get("sni_hostname") == host
        assert not request.url.query
        article_paths = {f"/test-fixtures/{slug}/": f"{PREFIX}/articles/{slug}" for slug in CASES}
        image_paths = {f"/{name}": f"{PREFIX}/assets/{name}" for name in ASSETS if name.endswith(".png")}
        if host == ARTICLE_HOST and request.url.path in article_paths:
            local_path = article_paths[request.url.path]
        elif host == IMAGE_HOST and request.url.path in image_paths:
            local_path = image_paths[request.url.path]
        else:
            raise AssertionError("Unlisted destination blocked by fixture transport")
        local_url = LOCAL_ORIGIN + local_path
        self.state.requests.append({"host": host, "path": request.url.path, "local_url": local_url})
        if host == IMAGE_HOST and self.state.image_override is not None:
            body, content_type = self.state.image_override
            return httpx.Response(200, headers={"content-type": content_type}, stream=httpx.ByteStream(body))
        transport = httpx.ASGITransport(app=self.state.fixture_app)
        return await transport.handle_async_request(httpx.Request("GET", local_url))


@pytest.fixture
def local_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.setenv("SCRAPE_IN_PROCESS", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    missing_models = tmp_path / "models-not-installed"
    monkeypatch.setenv("TESSDATA_PREFIX", str(missing_models))
    monkeypatch.setenv("ENABLE_TEST_SITE", "true")
    fixture_app = create_app()
    monkeypatch.setenv("ENABLE_TEST_SITE", "false")
    public_app = create_app()
    store = MemoryStore()
    public_app.dependency_overrides[get_store] = lambda: store
    state = LocalPipeline(fixture_app)
    fixture = json.loads(MOCK_OCR.read_text(encoding="utf-8"))
    state.expected = fixture["expected"]
    mock_result = OCRText.model_validate(fixture["ocr"])
    state.ocr_result = mock_result
    state.ocr = Mock()
    state.ocr.recognize = AsyncMock(side_effect=lambda data: mock_result.model_copy(deep=True))

    # A test-scoped metadata namespace, never an environment switch or runtime
    # localhost allowlist. The image fetcher's normal validation still runs.
    monkeypatch.setattr(images, "APPROVED_IMAGE_HOSTS", images.APPROVED_IMAGE_HOSTS | {IMAGE_HOST})
    async def test_dns(host):
        assert host in {ARTICLE_HOST, IMAGE_HOST}, "Unlisted DNS name blocked"
        return PIN
    monkeypatch.setattr(article, "resolve_public_ip", test_dns)
    monkeypatch.setattr(images, "resolve_public_ip", test_dns)
    real_fetch, real_download = article.fetch_article, images.download_lesson_image
    async def fetch(url):
        return await real_fetch(url, transport=FixtureTransport(state))
    async def download(image):
        return await real_download(image, transport=FixtureTransport(state))
    monkeypatch.setattr(scrape, "fetch_article", fetch)
    monkeypatch.setattr(scrape, "download_lesson_image", download)
    monkeypatch.setattr(scrape, "recognize_image", state.ocr.recognize)

    # Even an accidentally constructed default client or unexpected DNS/socket
    # call fails locally. Model initialization is forbidden, including direct use.
    wire = AsyncMock(side_effect=AssertionError("Real HTTP transport forbidden"))
    dns = Mock(side_effect=AssertionError("Real DNS forbidden"))
    connect = Mock(side_effect=AssertionError("Network sockets forbidden"))
    initialize = Mock(side_effect=AssertionError("Real OCR/model initialization forbidden"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect)
    monkeypatch.setattr("app.services.ocr.OCRService._initialize_locked", initialize)
    monkeypatch.setattr("app.services.ocr.create_tesseract_api", initialize)
    with TestClient(public_app) as client:
        state.client = client
        yield state
    wire.assert_not_awaited()
    dns.assert_not_called()
    connect.assert_not_called()
    initialize.assert_not_called()
    assert not missing_models.exists()


@pytest.mark.parametrize("slug,image_index", [("one-tall-image", 0), ("thumbnail-and-tall", 1)])
def test_local_site_drives_complete_real_pipeline_with_mocked_ocr(local_pipeline, slug, image_index):
    state = local_pipeline
    response = state.client.post("/api/v1/scrape", json={"article_url": state.article_url(slug)})
    assert response.status_code == 200, response.text
    result = ScrapeResponse.model_validate(response.json())
    assert str(result.article.url) == state.article_url(slug)
    assert result.article.title and result.article.section == "Fixture English"
    assert result.article.language == "ko" and result.article.published_at is not None
    assert str(result.lesson_image.url) == f"https://{IMAGE_HOST}/lesson.png"
    assert result.lesson_image.content_element_path == [image_index]
    assert not result.lesson_image.is_fallback
    assert len(result.article_images) == image_index + 1
    original_bytes = (ROOT / "assets" / "lesson.png").read_bytes()
    with Image.open(BytesIO(original_bytes)) as decoded:
        assert (result.lesson_image.width, result.lesson_image.height) == decoded.size == (720, 1800)
    assert result.lesson_image.byte_size == len(original_bytes)
    assert result.lesson_image.content_type == "image/png" and result.lesson_image.format == "PNG"
    assert result.lesson_image.download_url == result.lesson_image.url
    state.ocr.recognize.assert_awaited_once_with(original_bytes)
    assert result.ocr == state.ocr_result
    for name, expected in state.expected["fields"].items():
        actual = getattr(result.lesson.fields, name)
        assert actual.text == expected["text"] and actual.region_indices == expected["region_indices"]
    assert result.processing.status == "completed" and len(result.processing.completed_stages) == 6
    assert [entry["local_url"] for entry in state.requests] == [
        LOCAL_ORIGIN + f"{PREFIX}/articles/{slug}", LOCAL_ORIGIN + f"{PREFIX}/assets/lesson.png"]
    # The thumbnail was considered from page data, but never downloaded/OCRed.
    assert all(entry["path"] != "/thumbnail.png" for entry in state.requests)
    saved = state.client.get(response.headers["location"])
    assert saved.status_code == 200 and saved.json()["payload"] == response.json()


@pytest.mark.parametrize("slug,code", [("no-lesson-image", "no_lesson_image"), ("malformed-data", "invalid_article_data")])
def test_fixture_failures_stop_before_download_and_ocr(local_pipeline, slug, code):
    state = local_pipeline
    response = state.client.post("/api/v1/scrape", json={"article_url": state.article_url(slug)})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["message"]
    assert len(state.requests) == 1
    state.ocr.recognize.assert_not_awaited()


@pytest.mark.parametrize("body,content_type,code", [
    (b"<html>not an image</html>", "text/html", "invalid_image_content_type"),
    (b"corrupt PNG bytes", "image/png", "invalid_image"),
])
def test_image_bytes_and_mime_are_validated_before_ocr(local_pipeline, body, content_type, code):
    state = local_pipeline
    state.image_override = (body, content_type)
    response = state.client.post("/api/v1/scrape", json={"article_url": state.article_url("thumbnail-and-tall")})
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    state.ocr.recognize.assert_not_awaited()


@pytest.mark.parametrize("origin", [LOCAL_ORIGIN, "https://127.0.0.1", "https://localhost", "https://[::1]"])
def test_public_endpoint_rejects_local_test_urls_before_any_fetch(local_pipeline, origin):
    state = local_pipeline
    response = state.client.post("/api/v1/scrape", json={"article_url": origin + PREFIX + "/articles/one-tall-image"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert state.requests == []
    state.ocr.recognize.assert_not_awaited()
    assert state.client.get(PREFIX + "/articles/one-tall-image").status_code == 404


def test_fixture_server_requires_explicit_development_flag(local_pipeline):
    state = local_pipeline
    # The fixture constructed earlier had ENABLE_TEST_SITE=true. Environment is
    # now false, so a replacement server has no development routes to fetch.
    state.fixture_app = create_app()
    response = state.client.post("/api/v1/scrape", json={"article_url": state.article_url("one-tall-image")})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_http_error"
    state.ocr.recognize.assert_not_awaited()


@pytest.mark.parametrize("host,path", [("external.example", "/"), (ARTICLE_HOST, "/unlisted-article/"), (IMAGE_HOST, "/unlisted.png")])
def test_fixture_transport_has_no_external_or_unlisted_fallback(local_pipeline, host, path):
    async def run():
        async with httpx.AsyncClient(transport=FixtureTransport(local_pipeline), trust_env=False) as client:
            with pytest.raises(AssertionError, match="Unlisted destination"):
                await client.get(f"https://{PIN}{path}", headers={"Host": host}, extensions={"sni_hostname": host})
    asyncio.run(run())
    assert local_pipeline.requests == []
