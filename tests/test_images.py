import asyncio
from io import BytesIO
import socket
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app.main import app
from app.schemas import LessonImageMetadata
from app.services import images, scrape
from app.services.article import FetchedArticle
from app.services.errors import ArticleFetchError
from app.services.images import ImageDownloadConfig, ImageDownloadError

URL = "https://images.chosun.com/lesson.png"


def make_image(format="PNG", size=(32, 64)):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format=format)
    return output.getvalue()


def response(body=None, status=200, **headers):
    return httpx.Response(status, headers={"content-type": "image/png", **headers}, stream=httpx.ByteStream(make_image() if body is None else body))


def download(monkeypatch, handler, url=URL, config=None):
    monkeypatch.setattr(images, "resolve_public_ip", AsyncMock(return_value="93.184.216.34"))
    return asyncio.run(images.download_lesson_image(
        LessonImageMetadata(url=url, width=999, height=999), config=config,
        transport=httpx.MockTransport(handler),
    ))


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")])
def test_valid_image_bytes_and_metadata(monkeypatch, format, mime):
    data = make_image(format)
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "images.chosun.com"
        assert request.extensions["sni_hostname"] == "images.chosun.com"
        assert "ChosunEnglishLessonAPI" in request.headers["user-agent"]
        assert request.extensions["timeout"]["connect"] == 5
        assert request.extensions["timeout"]["read"] == 10
        return response(data, **{"content-type": mime})
    result = download(monkeypatch, handler)
    assert result.data == data
    assert result.metadata.content_type == mime
    assert result.metadata.format == format
    assert result.metadata.byte_size == len(data)
    assert (result.metadata.width, result.metadata.height) == (32, 64)
    assert str(result.metadata.url) == URL
    assert str(result.metadata.download_url) == URL


@pytest.mark.parametrize("mime", ["text/html", "application/octet-stream", "image/svg+xml", "image/tiff", ""])
def test_invalid_content_type(monkeypatch, mime):
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(**{"content-type": mime}))
    assert error.value.code == "invalid_image_content_type"


@pytest.mark.parametrize("body,mime", [(b"not an image", "image/png"), (b"<html>Access denied</html>", "image/png"), (make_image()[:40], "image/png"), (make_image("JPEG"), "image/png")])
def test_corrupt_or_mislabeled_image(monkeypatch, body, mime):
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(body, **{"content-type": mime}))
    assert error.value.code == "invalid_image"


@pytest.mark.parametrize("headers", [{"content-length": "9999"}, {}, {"content-length": "1"}])
def test_oversized_responses(monkeypatch, headers):
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(b"x" * 101, **headers), config=ImageDownloadConfig(max_bytes=100))
    assert error.value.code == "image_too_large"


def test_oversized_stream_closed_before_consuming_rest(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        closed = False
        chunks = 0

        async def __aiter__(self):
            for _ in range(5):
                self.chunks += 1
                yield b"x" * 60

        async def aclose(self):
            self.closed = True

    stream = Stream()
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "image/png"}, stream=stream), config=ImageDownloadConfig(max_bytes=100))
    assert error.value.code == "image_too_large"
    assert stream.chunks == 2
    assert stream.closed


@pytest.mark.parametrize("url", [
    "http://images.chosun.com/a.png", "https://localhost/a.png", "https://127.0.0.1/a.png",
    "https://10.0.0.1/a.png", "https://[::1]/a.png", "https://169.254.169.254/a.png",
    "https://example.com/a.png", "https://images.chosun.com.evil.com/a.png",
    "https://cloudfront-ap-northeast-1.images.arcpublishing.com/other/a.png",
    "https://cloudfront-ap-northeast-1.images.arcpublishing.com/chosun/../other/a.png",
    "https://cloudfront-ap-northeast-1.images.arcpublishing.com/chosun/%2e%2e/other/a.png",
    "https://images.chosun.com:8443/a.png", "https://user@images.chosun.com/a.png",
])
def test_redirect_destinations_rejected_before_request(monkeypatch, url):
    calls = []
    def handler(request):
        calls.append(request)
        return response(status=302, location=url)
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, handler)
    assert error.value.code == "unsafe_image_url"
    assert len(calls) == 1


def test_initial_unapproved_url_never_resolves_or_connects(monkeypatch):
    resolver = AsyncMock()
    monkeypatch.setattr(images, "resolve_public_ip", resolver)
    with pytest.raises(ImageDownloadError):
        asyncio.run(images.download_lesson_image(LessonImageMetadata(url="https://example.com/a.png")))
    resolver.assert_not_awaited()


def test_approved_redirect_revalidates_dns_and_preserves_source_url(monkeypatch):
    resolver = AsyncMock(side_effect=["93.184.216.34", "93.184.216.35"])
    monkeypatch.setattr(images, "resolve_public_ip", resolver)
    targets = []
    final = "https://cloudfront-ap-northeast-1.images.arcpublishing.com/chosun/lesson.png"
    def handler(request):
        targets.append(request.url.host)
        if len(targets) == 1:
            return response(status=302, location=final)
        assert request.headers["host"] == images.ARC_IMAGE_HOST
        assert request.extensions["sni_hostname"] == images.ARC_IMAGE_HOST
        return response()
    result = asyncio.run(images.download_lesson_image(LessonImageMetadata(url=URL), transport=httpx.MockTransport(handler)))
    assert str(result.metadata.url) == URL
    assert str(result.metadata.download_url) == final
    assert targets == ["93.184.216.34", "93.184.216.35"]
    assert resolver.await_count == 2


def test_private_dns_on_redirect_never_connects(monkeypatch):
    resolver = AsyncMock(side_effect=["93.184.216.34", ArticleFetchError("unsafe_destination", "Private address", 422)])
    monkeypatch.setattr(images, "resolve_public_ip", resolver)
    calls = []
    def handler(request):
        calls.append(request)
        return response(status=302, location="/other.png")
    with pytest.raises(ImageDownloadError) as error:
        asyncio.run(images.download_lesson_image(LessonImageMetadata(url=URL), transport=httpx.MockTransport(handler)))
    assert error.value.code == "unsafe_image_destination"
    assert len(calls) == 1


def test_real_dns_policy_rejects_private_image_address(monkeypatch):
    async def run():
        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        handler = AsyncMock()
        with pytest.raises(ImageDownloadError) as error:
            await images.download_lesson_image(LessonImageMetadata(url=URL), transport=httpx.MockTransport(handler))
        assert error.value.code == "unsafe_image_destination"
        handler.assert_not_called()
    asyncio.run(run())


def test_dns_failure_and_total_network_timeout(monkeypatch):
    monkeypatch.setattr(images, "resolve_public_ip", AsyncMock(side_effect=socket.gaierror()))
    with pytest.raises(ImageDownloadError) as error:
        asyncio.run(images.download_lesson_image(LessonImageMetadata(url=URL)))
    assert error.value.code == "image_network_error"

    async def slow(host):
        await asyncio.sleep(1)
    monkeypatch.setattr(images, "resolve_public_ip", slow)
    with pytest.raises(ImageDownloadError) as error:
        asyncio.run(images.download_lesson_image(LessonImageMetadata(url=URL), config=ImageDownloadConfig(total_timeout=0.001)))
    assert error.value.code == "image_timeout"


@pytest.mark.parametrize("location", [None, "/loop.png"])
def test_missing_redirect_or_redirect_limit(monkeypatch, location):
    headers = {"location": location} if location is not None else {}
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(status=302, **headers), config=ImageDownloadConfig(max_redirects=1))
    assert error.value.code == "image_redirect_error"


def test_pillow_bomb_warning_becomes_application_error(monkeypatch):
    data = make_image(size=(10, 10))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 60)
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(data))
    assert error.value.code == "image_dimensions_exceeded"


def test_animated_images_rejected(monkeypatch):
    output = BytesIO()
    first = Image.new("RGB", (32, 32), "white")
    second = Image.new("RGB", (32, 32), "black")
    first.save(output, format="GIF", save_all=True, append_images=[second])
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(output.getvalue(), **{"content-type": "image/gif"}))
    assert error.value.code == "invalid_image"


@pytest.mark.parametrize("options", [{"max_bytes": 0}, {"max_pixels": -1}, {"read_timeout": float("inf")}, {"max_redirects": -1}])
def test_invalid_config_is_rejected(options):
    with pytest.raises(ValueError):
        ImageDownloadConfig(**options)


def test_custom_timeouts_and_exact_size_limit(monkeypatch):
    data = make_image()
    def handler(request):
        assert request.extensions["timeout"]["connect"] == 2
        assert request.extensions["timeout"]["read"] == 3
        return response(data, **{"content-length": str(len(data))})
    result = download(monkeypatch, handler, config=ImageDownloadConfig(max_bytes=len(data), connect_timeout=2, read_timeout=3))
    assert result.metadata.byte_size == len(data)


@pytest.mark.parametrize("config", [ImageDownloadConfig(max_pixels=100), ImageDownloadConfig(max_dimension=20)])
def test_decoded_dimensions_are_bounded(monkeypatch, config):
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(), config=config)
    assert error.value.code == "image_dimensions_exceeded"


@pytest.mark.parametrize("exception,code", [(httpx.ConnectTimeout, "image_timeout"), (httpx.ReadTimeout, "image_timeout"), (httpx.ConnectError, "image_network_error")])
def test_network_errors(monkeypatch, exception, code):
    def handler(request):
        raise exception("Failed", request=request)
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, handler)
    assert error.value.code == code


@pytest.mark.parametrize("status,code", [(403, "image_blocked"), (429, "image_blocked"), (404, "image_http_error"), (500, "image_http_error")])
def test_http_errors(monkeypatch, status, code):
    with pytest.raises(ImageDownloadError) as error:
        download(monkeypatch, lambda request: response(status=status))
    assert error.value.code == code


def test_api_uses_downloaded_metadata_and_maps_image_errors(monkeypatch, mock_ocr):
    html = '<title>Lesson</title><script>Fusion.globalContent = {"type":"story","content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":800,"height":2000}]};</script>'
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(url="https://www.chosun.com/example/", html=html, title="Lesson")))
    real_download = images.download_lesson_image
    monkeypatch.setattr(images, "resolve_public_ip", AsyncMock(return_value="93.184.216.34"))
    async def mocked_transport_download(image):
        return await real_download(image, transport=httpx.MockTransport(lambda request: response()))
    monkeypatch.setattr(scrape, "download_lesson_image", mocked_transport_download)
    result = TestClient(app).post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"})
    assert result.status_code == 200
    assert result.json()["lesson_image"]["width"] == 32
    assert result.json()["lesson_image"]["format"] == "PNG"
    assert result.json()["ocr"]["raw_text"] == "안녕하세요 Hello"
    assert "data" not in result.json()["lesson_image"]
    monkeypatch.setattr(scrape, "download_lesson_image", AsyncMock(side_effect=ImageDownloadError("image_timeout", "Timed out", 504)))
    result = TestClient(app).post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"})
    assert result.status_code == 504
    assert result.json()["error"]["code"] == "image_timeout"
