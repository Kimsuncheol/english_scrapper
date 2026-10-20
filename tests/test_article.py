import asyncio
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from app.main import app
from app.services import article
from app.services.errors import ArticleFetchError
from app.services.urls import validate_article_url
from fastapi.testclient import TestClient

URL = "https://www.chosun.com/news/example/"
HTML = b'<html><head><title>Lesson</title><meta property="og:type" content="article"></head><body>Lesson</body></html>'


def response(status=200, body=HTML, **headers):
    return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **headers}, stream=httpx.ByteStream(body))


def fetch(monkeypatch, handler, url=URL):
    monkeypatch.setattr(article, "resolve_public_ip", AsyncMock(return_value="93.184.216.34"))
    return asyncio.run(article.fetch_article(url, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("url", [
    "http://chosun.com/a", "ftp://chosun.com/a", "file:///etc/passwd",
    "https://localhost/a", "https://127.0.0.1/a", "https://10.0.0.1/a",
    "https://[::1]/a", "https://169.254.169.254/a", "https://example.com/a",
    "https://chosun.com.evil.com/a", "https://evilchosun.com/a",
    "https://unknown.chosun.com/a", "https://user@chosun.com/a",
    "https://chosun.com:8443/a", "https:chosun.com", "https://chosun.com\\@evil.com/a",
    " https://chosun.com/a", "https://chosun.com/\na",
])
def test_disallowed_urls(url):
    with pytest.raises(ValueError):
        validate_article_url(url)


@pytest.mark.parametrize("url", [URL, "https://chosun.com/a", "https://www.chosun.com:443/a"])
def test_approved_urls(url):
    assert validate_article_url(url).scheme == "https"


def test_success_pins_ip_preserving_host_and_tls_name(monkeypatch):
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.url.path == "/news/example/"
        assert request.headers["host"] == "www.chosun.com"
        assert request.extensions["sni_hostname"] == "www.chosun.com"
        assert "ChosunEnglishLessonAPI" in request.headers["user-agent"]
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["timeout"]["connect"] == 5
        assert request.extensions["timeout"]["read"] == 10
        return response()
    result = fetch(monkeypatch, handler)
    assert result.url == URL
    assert result.title == "Lesson"
    assert result.html == HTML.decode()


@pytest.mark.parametrize("status,code", [(401, "blocked_page"), (403, "blocked_page"), (429, "blocked_page"), (404, "upstream_http_error"), (500, "upstream_http_error")])
def test_http_failures(monkeypatch, status, code):
    with pytest.raises(ArticleFetchError) as error:
        fetch(monkeypatch, lambda request: response(status))
    assert error.value.code == code


@pytest.mark.parametrize("exception,code", [(httpx.ConnectTimeout, "article_timeout"), (httpx.ReadTimeout, "article_timeout"), (httpx.ConnectError, "upstream_network_error"), (httpx.RemoteProtocolError, "upstream_network_error")])
def test_network_failures(monkeypatch, exception, code):
    def handler(request):
        raise exception("failure", request=request)
    with pytest.raises(ArticleFetchError) as error:
        fetch(monkeypatch, handler)
    assert error.value.code == code


@pytest.mark.parametrize("body,headers,code", [
    (b"image", {"content-type": "image/png"}, "invalid_article"),
    (b"<title>Home</title>", {}, "invalid_article"),
    (b"<title>Access Denied</title>", {}, "blocked_page"),
    (b"<title>Just a moment...</title>", {}, "blocked_page"),
    (HTML, {"content-length": str(article.MAX_HTML_BYTES + 1)}, "html_too_large"),
    (HTML, {"content-length": "bad"}, "upstream_http_error"),
    (HTML, {"content-encoding": "gzip"}, "unsupported_encoding"),
    (b"x" * (article.MAX_HTML_BYTES + 1), {}, "html_too_large"),
])
def test_invalid_responses(monkeypatch, body, headers, code):
    with pytest.raises(ArticleFetchError) as error:
        fetch(monkeypatch, lambda request: response(body=body, **headers))
    assert error.value.code == code


@pytest.mark.parametrize("destination", ["http://www.chosun.com/a", "https://localhost/", "https://10.0.0.1/", "https://evil.com/", "https://unknown.chosun.com/"])
def test_redirect_rejected_before_second_request(monkeypatch, destination):
    calls = []
    def handler(request):
        calls.append(request)
        return response(302, location=destination)
    with pytest.raises(ArticleFetchError) as error:
        fetch(monkeypatch, handler)
    assert error.value.code == "unsafe_url"
    assert len(calls) == 1


def test_relative_redirect_and_limit(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return response(302, location="/final/") if len(calls) == 1 else response()
    assert fetch(monkeypatch, handler).url == "https://www.chosun.com/final/"
    assert len(calls) == 2
    with pytest.raises(ArticleFetchError, match="redirect"):
        fetch(monkeypatch, lambda request: response(302, location="/loop/"))


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["::1"], ["fc00::1"], ["::ffff:127.0.0.1"], ["93.184.216.34", "192.168.1.1"]])
def test_dns_rejects_any_nonpublic_address(monkeypatch, addresses):
    async def run():
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records))
        with pytest.raises(ArticleFetchError) as error:
            await article.resolve_public_ip("www.chosun.com")
        assert error.value.code == "unsafe_destination"
    asyncio.run(run())


def test_dns_failure_and_total_deadline(monkeypatch):
    monkeypatch.setattr(article, "resolve_public_ip", AsyncMock(side_effect=socket.gaierror()))
    with pytest.raises(ArticleFetchError) as error:
        asyncio.run(article.fetch_article(URL))
    assert error.value.code == "upstream_network_error"
    async def slow(host):
        await asyncio.sleep(1)
    monkeypatch.setattr(article, "resolve_public_ip", slow)
    monkeypatch.setattr(article, "TOTAL_TIMEOUT", 0.001)
    with pytest.raises(ArticleFetchError) as error:
        asyncio.run(article.fetch_article(URL))
    assert error.value.code == "article_timeout"


def test_jsonld_article_recognition():
    html = '<title>Lesson</title><script type="application/ld+json">{"@graph":[{"@type":"NewsArticle"}]}</script>'
    assert article.validate_article_html(URL, html).title == "Lesson"


def test_stream_size_limit_ignores_false_length_and_closes(monkeypatch):
    class ChunkStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * article.MAX_HTML_BYTES
            yield b"x"

        async def aclose(self):
            self.closed = True

    stream = ChunkStream()
    with pytest.raises(ArticleFetchError) as error:
        fetch(monkeypatch, lambda request: httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "1"}, stream=stream
        ))
    assert error.value.code == "html_too_large"
    assert stream.closed


def test_unsafe_dns_never_sends_http_request(monkeypatch):
    handler = AsyncMock()
    monkeypatch.setattr(article, "resolve_public_ip", AsyncMock(side_effect=ArticleFetchError(
        "unsafe_destination", "Article host must resolve only to public IP addresses.", 422
    )))
    with pytest.raises(ArticleFetchError):
        asyncio.run(article.fetch_article(URL, transport=httpx.MockTransport(handler)))
    handler.assert_not_called()


def test_redirect_destination_resolved_again(monkeypatch):
    resolver = AsyncMock(side_effect=["93.184.216.34", "93.184.216.35"])
    monkeypatch.setattr(article, "resolve_public_ip", resolver)
    targets = []

    def handler(request):
        targets.append(request.url.host)
        if len(targets) == 1:
            return response(302, location="https://chosun.com/final/")
        assert request.headers["host"] == "chosun.com"
        assert request.extensions["sni_hostname"] == "chosun.com"
        return response()

    result = asyncio.run(article.fetch_article(URL, transport=httpx.MockTransport(handler)))
    assert result.url == "https://chosun.com/final/"
    assert targets == ["93.184.216.34", "93.184.216.35"]
    assert resolver.await_count == 2


def test_fetch_error_is_exposed_through_api(monkeypatch, persisted_store):
    from app.services import scrape
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(side_effect=ArticleFetchError("article_timeout", "Timed out fetching the article.", 504)))
    result = TestClient(app).post("/api/v1/scrape", json={"article_url": URL})
    assert result.status_code == 504
    assert result.json()["error"]["code"] == "article_timeout"
