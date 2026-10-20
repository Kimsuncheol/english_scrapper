"""Bounded async fetching, with no browser, retries, or persistent files."""

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import ip_address
import json
import socket
from urllib.parse import urljoin

import httpx

from app.services.errors import ArticleFetchError
from app.services.urls import validate_article_url

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TOTAL_TIMEOUT = 30
HTTP_TIMEOUT = httpx.Timeout(connect=5, read=10, write=5, pool=5)
USER_AGENT = "ChosunEnglishLessonAPI/0.1 (article text learning service)"


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    html: str
    title: str


async def resolve_public_ip(host: str) -> str:
    records = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    addresses = [ip_address(record[4][0]) for record in records]
    if not addresses or any(
        not address.is_global
        or (getattr(address, "ipv4_mapped", None) is not None
            and not address.ipv4_mapped.is_global)
        for address in addresses
    ):
        raise ArticleFetchError("unsafe_destination", "Article host must resolve only to public IP addresses.", 422)
    return str(addresses[0])


class ArticleHTML(HTMLParser):
    """Recognize article metadata without assuming lesson-image selectors."""

    def __init__(self):
        super().__init__()
        self.title_parts = []
        self.in_title = False
        self.in_json = False
        self.json_parts = []
        self.is_article = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and attributes.get("property", "").lower() == "og:type":
            self.is_article |= attributes.get("content", "").lower() == "article"
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self.in_json = True
            self.json_parts = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.in_json:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_json:
            self.in_json = False
            try:
                self.is_article |= self.contains_article(json.loads("".join(self.json_parts)))
            except (ValueError, RecursionError):
                pass

    @classmethod
    def contains_article(cls, value):
        if isinstance(value, list):
            return any(cls.contains_article(item) for item in value)
        if isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            return (isinstance(types, list) and any(
                item in {"Article", "NewsArticle", "ReportageNewsArticle"}
                for item in types if isinstance(item, str)
            )) or cls.contains_article(value.get("@graph", []))
        return False


def validate_article_html(url: str, html: str) -> FetchedArticle:
    parser = ArticleHTML()
    try:
        parser.feed(html)
    except (ValueError, RecursionError) as exc:
        raise ArticleFetchError("invalid_article", "The response is not a valid article page.", 422) from exc
    title = "".join(parser.title_parts).strip()
    blocked_titles = ("access denied", "just a moment", "captcha", "verify you are human", "접근 차단")
    if any(marker in title.lower() for marker in blocked_titles) or any(
        marker in html.lower() for marker in ("/cdn-cgi/challenge-platform/", "cf-chl-", "g-recaptcha", "hcaptcha.com/1/api.js")
    ):
        raise ArticleFetchError("blocked_page", "The publisher returned an access-control or challenge page.", 403)
    if not title or not parser.is_article:
        raise ArticleFetchError("invalid_article", "The HTML lacks an article title or supported article metadata.", 422)
    return FetchedArticle(url=url, html=html, title=title)


async def _fetch_article(article_url: str, transport: httpx.AsyncBaseTransport | None) -> FetchedArticle:
    current = article_url
    # No environment proxies, cookies from other requests, automatic redirects,
    # or connection reuse across hosts sharing a CDN IP.
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT, follow_redirects=False, trust_env=False,
        transport=transport, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        for hop in range(MAX_REDIRECTS + 1):
            try:
                logical_url = validate_article_url(current)
            except ValueError as exc:
                raise ArticleFetchError("unsafe_url", str(exc), 422) from exc
            host = logical_url.host
            address = await resolve_public_ip(host)
            target = httpx.URL(str(logical_url)).copy_with(host=address, fragment=None)
            async with client.stream(
                "GET", target,
                headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"},
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == MAX_REDIRECTS:
                        raise ArticleFetchError("redirect_error", "Article redirect is missing a destination or exceeds the redirect limit.")
                    current = urljoin(str(logical_url), location)
                    continue
                if response.status_code in {401, 403, 429}:
                    raise ArticleFetchError("blocked_page", "The publisher denied or rate-limited article access.", 403)
                if response.status_code != 200:
                    raise ArticleFetchError("upstream_http_error", f"The publisher returned HTTP {response.status_code}.")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                    raise ArticleFetchError("invalid_article", "The publisher did not return HTML.", 422)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ArticleFetchError("unsupported_encoding", "The publisher ignored the uncompressed HTML request.")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                        if size < 0:
                            raise ValueError
                    except ValueError as exc:
                        raise ArticleFetchError("upstream_http_error", "The publisher returned an invalid Content-Length.") from exc
                    if size > MAX_HTML_BYTES:
                        raise ArticleFetchError("html_too_large", "Article HTML exceeds the 2 MiB limit.")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                        raise ArticleFetchError("html_too_large", "Article HTML exceeds the 2 MiB limit.")
                    body.extend(chunk)
                try:
                    html = body.decode(response.encoding or "utf-8", errors="replace")
                except LookupError as exc:
                    raise ArticleFetchError("invalid_article", "The publisher returned an unsupported character encoding.", 422) from exc
                return validate_article_html(str(logical_url), html)


async def fetch_article(article_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> FetchedArticle:
    """Fetch a validated article; transport injection is only for offline tests."""
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            return await _fetch_article(article_url, transport)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ArticleFetchError("article_timeout", "Timed out fetching the article.", 504) from exc
    except (httpx.RequestError, OSError) as exc:
        raise ArticleFetchError("upstream_network_error", "Could not establish a secure connection to the article publisher.") from exc
