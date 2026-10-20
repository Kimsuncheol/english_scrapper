import asyncio
from io import BytesIO
import json
import logging
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app.main import app
from app.schemas import OCRText, ScrapeResponse
from app.services import article, images, ocr, scrape

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.chosun.com/lesson/one/"


def raster(name="one"):
    stream = BytesIO()
    Image.new("RGB", (1000, 1050), {"one": "white", "two": "red", "three": "blue"}.get(name, "white")).save(stream, format="PNG")
    return stream.getvalue()


def article_html(name="one"):
    metadata = {"@type": "NewsArticle", "headline": "English lesson", "url": f"https://www.chosun.com/lesson/{name}/", "datePublished": "2026-10-20T06:00:00+09:00", "inLanguage": "ko", "articleSection": "Education"}
    content = {"type": "story", "content_elements": [{"type": "image", "url": f"https://images.chosun.com/lesson-{name}.png", "width": 1200, "height": 3000}]}
    return ('<title>Lesson</title><script type="application/ld+json">' + json.dumps(metadata) + '</script><script>Fusion.globalContent = ' + json.dumps(content) + ';</script>').encode()


def reply(body, content_type="text/html", status=200, **headers):
    return httpx.Response(status, headers={"content-type": content_type, **headers}, stream=httpx.ByteStream(body))


@pytest.fixture
def pipeline(monkeypatch):
    """Use real fetch/parse/download/decode/post-processing, mock only I/O/OCR."""
    state = {"requests": [], "article_handler": None, "image_handler": None}
    result = OCRText.model_validate(json.loads((FIXTURES / "lesson_ocr_single_column.json").read_text())["ocr"])
    state["ocr"] = AsyncMock(side_effect=lambda data: result.model_copy(deep=True))
    state["result"] = result

    def handler(request):
        state["requests"].append(request)
        if request.headers["host"] == "www.chosun.com":
            if state["article_handler"]:
                return state["article_handler"](request)
            name = request.url.path.strip("/").split("/")[-1]
            return reply(article_html(name))
        if state["image_handler"]:
            return state["image_handler"](request)
        name = request.url.path.rsplit("-", 1)[-1].split(".")[0]
        return reply(raster(name), "image/png")

    transport = httpx.MockTransport(handler)
    real_fetch, real_download = article.fetch_article, images.download_lesson_image

    async def fetch(url):
        return await real_fetch(url, transport=transport)

    async def download(image):
        return await real_download(image, transport=transport)

    monkeypatch.setattr(article, "resolve_public_ip", AsyncMock(return_value="93.184.216.34"))
    monkeypatch.setattr(images, "resolve_public_ip", AsyncMock(return_value="93.184.216.34"))
    monkeypatch.setattr(scrape, "fetch_article", fetch)
    monkeypatch.setattr(scrape, "download_lesson_image", download)
    monkeypatch.setattr(scrape, "recognize_image", state["ocr"])
    return state


def post():
    return TestClient(app).post("/api/v1/scrape", json={"article_url": URL})


def test_complete_endpoint_returns_explicit_contract(pipeline):
    response = post()
    assert response.status_code == 200
    result = ScrapeResponse.model_validate(response.json())
    assert result.article.title == "English lesson"
    assert result.article.section == "Education"
    assert str(result.lesson_image.url) == "https://images.chosun.com/lesson-one.png"
    assert (result.lesson_image.width, result.lesson_image.height) == (1000, 1050)
    assert result.lesson_image.byte_size == len(raster())
    assert result.ocr == pipeline["result"]
    assert result.lesson.fields.expression.text == "Don't sweat it."
    assert result.lesson.fields.korean_dialogue_translation is not None
    assert result.processing.status == "completed"
    assert len(result.processing.completed_stages) == 6
    assert result.processing.elapsed_ms > 0
    pipeline["ocr"].assert_awaited_once_with(raster())
    assert [request.headers["host"] for request in pipeline["requests"]] == ["www.chosun.com", "images.chosun.com"]


@pytest.mark.parametrize("url", ["http://www.chosun.com/lesson/", "https://localhost/", "https://127.0.0.1/", "https://evil.com/", "bad-url"])
def test_url_rejected_before_any_http(pipeline, url):
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": url})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert pipeline["requests"] == []
    pipeline["ocr"].assert_not_awaited()


@pytest.mark.parametrize("status,expected,code", [(403, 403, "blocked_page"), (404, 502, "upstream_http_error"), (500, 502, "upstream_http_error")])
def test_article_http_failure_stops_pipeline(pipeline, status, expected, code):
    pipeline["article_handler"] = lambda request: reply(b"failure", status=status)
    response = post()
    assert response.status_code == expected
    assert response.json()["error"]["code"] == code
    assert len(pipeline["requests"]) == 1
    pipeline["ocr"].assert_not_awaited()


def test_http_timeout_is_useful_error(pipeline):
    def timed_out(request):
        raise httpx.ReadTimeout("upstream details", request=request)
    pipeline["article_handler"] = timed_out
    response = post()
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "article_timeout"
    pipeline["ocr"].assert_not_awaited()


def test_no_image_stops_before_download(pipeline):
    pipeline["article_handler"] = lambda request: reply(b'<title>Text article</title><meta property="og:type" content="article">')
    response = post()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_lesson_image"
    assert len(pipeline["requests"]) == 1


@pytest.mark.parametrize("content_type,body,code", [("text/html", b"<html>denied</html>", "invalid_image_content_type"), ("image/png", b"not an image", "invalid_image")])
def test_invalid_download_never_runs_ocr(pipeline, content_type, body, code):
    pipeline["image_handler"] = lambda request: reply(body, content_type)
    response = post()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    pipeline["ocr"].assert_not_awaited()


def test_environment_limits_apply_to_real_fetches(pipeline, monkeypatch):
    monkeypatch.setenv("ARTICLE_MAX_BYTES", "100")
    response = post()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "html_too_large"
    monkeypatch.delenv("ARTICLE_MAX_BYTES")
    monkeypatch.setenv("IMAGE_MAX_BYTES", "100")
    response = post()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "image_too_large"
    pipeline["ocr"].assert_not_awaited()


def test_environment_timeouts_are_forwarded_to_http_client(pipeline, monkeypatch):
    monkeypatch.setenv("ARTICLE_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ARTICLE_READ_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("IMAGE_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("IMAGE_READ_TIMEOUT_SECONDS", "4")
    assert post().status_code == 200
    assert pipeline["requests"][0].extensions["timeout"]["connect"] == 1
    assert pipeline["requests"][0].extensions["timeout"]["read"] == 2
    assert pipeline["requests"][1].extensions["timeout"]["connect"] == 3
    assert pipeline["requests"][1].extensions["timeout"]["read"] == 4


@pytest.mark.parametrize("code,status", [("ocr_unavailable", 503), ("ocr_recognition_failed", 504), ("ocr_invalid_image", 422)])
def test_ocr_errors_use_same_envelope(pipeline, code, status):
    pipeline["ocr"].side_effect = ocr.OCRError(code, "OCR processing failed.", status)
    response = post()
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("mode", ["empty", "low_confidence", "unparsed"])
def test_partial_results_preserve_raw_ocr(pipeline, mode):
    result = pipeline["result"].model_copy(deep=True)
    if mode == "empty":
        result = OCRText(raw_text="", status="empty")
    elif mode == "low_confidence":
        result.status = "low_confidence"
        result.warnings = ["Low confidence"]
        for region in result.regions:
            region.confidence = 0.2
    else:
        result = OCRText(raw_text="Unknown lesson format")
    pipeline["ocr"].side_effect = lambda data: result
    response = post()
    assert response.status_code == 200
    assert response.json()["processing"]["status"] == "partial"
    assert response.json()["ocr"] == result.model_dump(mode="json")
    assert response.json()["processing"]["warnings"]


def test_pipeline_timeout_releases_admission(pipeline, monkeypatch):
    async def stalled(url):
        await asyncio.sleep(1)
    monkeypatch.setattr(scrape, "fetch_article", stalled)
    monkeypatch.setenv("SCRAPE_TIMEOUT_SECONDS", "0.001")
    for _ in range(2):
        response = post()
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "scrape_timeout"


def test_unexpected_errors_do_not_log_payloads(pipeline, caplog):
    secret = "PRIVATE_ARTICLE_CONTENT_AND_IMAGE_BYTES"
    pipeline["ocr"].side_effect = RuntimeError(secret)
    with caplog.at_level(logging.ERROR):
        response = post()
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "processing_failed"
    assert secret not in caplog.text
    assert secret not in response.text


def test_engine_exception_does_not_log_image_or_article_payload(pipeline, caplog, monkeypatch):
    secret = "PRIVATE_IMAGE_BYTES_AND_ARTICLE_TEXT"
    engine = Mock()
    engine.recognize.side_effect = RuntimeError(secret)
    service = ocr.OCRService(lambda: engine)
    monkeypatch.setattr(scrape, "recognize_image", service.recognize)
    try:
        with caplog.at_level(logging.ERROR):
            response = post()
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "ocr_failed"
        assert secret not in caplog.text
        assert secret not in response.text
    finally:
        service.close()


@pytest.mark.parametrize("variable,value", [("ARTICLE_MAX_BYTES", "0"), ("ARTICLE_MAX_BYTES", "9999999"), ("IMAGE_MAX_PIXELS", "0"), ("SCRAPE_TIMEOUT_SECONDS", "nan"), ("SCRAPE_MAX_CONCURRENT_REQUESTS", "0")])
def test_invalid_server_limits_fail_startup(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    initialize = Mock(return_value=True)
    monkeypatch.setattr(ocr.ocr_service, "initialize", initialize)
    with pytest.raises(ValueError):
        with TestClient(app):
            pass
    initialize.assert_not_called()


def test_default_admission_rejects_second_request_before_http(pipeline, monkeypatch):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        original = scrape.fetch_article
        async def delayed(url):
            entered.set()
            await release.wait()
            return await original(url)
        monkeypatch.setattr(scrape, "fetch_article", delayed)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/v1/scrape", json={"article_url": URL}))
            await asyncio.wait_for(entered.wait(), 2)
            try:
                second = await client.post("/api/v1/scrape", json={"article_url": URL})
                assert second.status_code == 503
                assert second.json()["error"]["code"] == "scrape_busy"
                assert not pipeline["requests"]
            finally:
                release.set()
                response = await first
            assert response.status_code == 200
    asyncio.run(run())


def test_two_admitted_requests_cannot_corrupt_shared_ocr(pipeline, monkeypatch):
    monkeypatch.setenv("SCRAPE_MAX_CONCURRENT_REQUESTS", "2")
    entered, release = Event(), Event()
    calls = []
    class Engine:
        def recognize(self, data):
            calls.append(data)
            entered.set()
            assert release.wait(5)
            result = pipeline["result"].model_copy(deep=True)
            result.raw_text = "request-one" if data == raster("one") else "request-three"
            return result

        def close(self):
            pass
    factory = Mock(return_value=Engine())
    service = ocr.OCRService(factory)
    monkeypatch.setattr(scrape, "recognize_image", service.recognize)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/v1/scrape", json={"article_url": URL}))
            assert await asyncio.to_thread(entered.wait, 2)
            try:
                second = await client.post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/lesson/two/"})
                assert second.status_code == 503
                assert second.json()["error"]["code"] == "ocr_busy"
                assert len(calls) == 1
            finally:
                release.set()
                response = await first
            assert response.status_code == 200
            assert response.json()["ocr"]["raw_text"] == "request-one"
            third = await client.post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/lesson/three/"})
            assert third.status_code == 200
            assert third.json()["ocr"]["raw_text"] == "request-three"
    try:
        asyncio.run(run())
        factory.assert_called_once()
        assert calls == [raster("one"), raster("three")]
    finally:
        release.set()
        service.close()
