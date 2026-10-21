"""Bounded async fetching, with no browser, retries, or persistent files."""

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
import socket
import os
import math
from urllib.parse import urljoin

import httpx

from app.services.errors import ArticleFetchError
from app.services.urls import validate_article_url
from app.services.parsing import article_nodes, metadata_from_page, read_page

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TOTAL_TIMEOUT = 30
USER_AGENT = "ChosunEnglishLessonAPI/0.1 (article text learning service)"


@dataclass(frozen=True)
class ArticleFetchConfig:
    max_bytes: int = MAX_HTML_BYTES
    connect_timeout: float = 5
    read_timeout: float = 10
    total_timeout: float = TOTAL_TIMEOUT
    max_redirects: int = MAX_REDIRECTS

    def __post_init__(self):
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int) or not 0 < self.max_bytes <= MAX_HTML_BYTES:
            raise ValueError("Article byte limit must be a positive integer no larger than the 2 MiB parser cap")
        if isinstance(self.max_redirects, bool) or not isinstance(self.max_redirects, int) or self.max_redirects < 0:
            raise ValueError("Article redirect limit must be a nonnegative integer")
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (self.connect_timeout, self.read_timeout, self.total_timeout)):
            raise ValueError("Article timeouts must be positive finite numbers")

    @classmethod
    def from_env(cls):
        return cls(
            max_bytes=int(os.getenv("ARTICLE_MAX_BYTES", str(MAX_HTML_BYTES))),
            connect_timeout=float(os.getenv("ARTICLE_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout=float(os.getenv("ARTICLE_READ_TIMEOUT_SECONDS", "10")),
            total_timeout=float(os.getenv("ARTICLE_TOTAL_TIMEOUT_SECONDS", str(TOTAL_TIMEOUT))),
            max_redirects=int(os.getenv("ARTICLE_MAX_REDIRECTS", str(MAX_REDIRECTS))),
        )


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


def validate_article_html(url: str, html: str) -> FetchedArticle:
    page = read_page(html)
    metadata = metadata_from_page(page, url)
    title = metadata.title or ""
    blocked_titles = ("access denied", "just a moment", "captcha", "verify you are human", "접근 차단")
    if any(marker in (page.title + " " + title).lower() for marker in blocked_titles) or any(
        marker in html.lower() for marker in ("/cdn-cgi/challenge-platform/", "cf-chl-", "g-recaptcha", "hcaptcha.com/1/api.js")
    ):
        raise ArticleFetchError("blocked_page", "The publisher returned an access-control or challenge page.", 403)
    data = page.global_content
    is_article = (
        page.meta.get("og:type", "").lower() == "article"
        or any(article_nodes(page.jsonld))
        or (isinstance(data.get("content_elements"), list) and data.get("type") == "story")
    )
    if not title or not is_article:
        raise ArticleFetchError("invalid_article", "The HTML lacks an article title or supported article metadata.", 422)
    return FetchedArticle(url=url, html=html, title=title)


async def _fetch_article(article_url: str, transport: httpx.AsyncBaseTransport | None, config: ArticleFetchConfig, listing: bool = False, json_response: bool = False) -> FetchedArticle:
    current = article_url
    # No environment proxies, cookies from other requests, automatic redirects,
    # or connection reuse across hosts sharing a CDN IP.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=config.connect_timeout, read=config.read_timeout, write=5, pool=5),
        follow_redirects=False, trust_env=False,
        transport=transport, limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        for hop in range(config.max_redirects + 1):
            try:
                logical_url = validate_article_url(current)
            except ValueError as exc:
                raise ArticleFetchError("unsafe_url", str(exc), 422) from exc
            host = logical_url.host
            address = await resolve_public_ip(host)
            target = httpx.URL(str(logical_url)).copy_with(host=address, fragment=None)
            async with client.stream(
                "GET", target,
                headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "application/json" if json_response else "text/html,application/xhtml+xml", "Accept-Encoding": "identity"},
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == config.max_redirects:
                        raise ArticleFetchError("redirect_error", "Article redirect is missing a destination or exceeds the redirect limit.")
                    current = urljoin(str(logical_url), location)
                    continue
                if response.status_code in {401, 403, 429}:
                    raise ArticleFetchError("blocked_page", "The publisher denied or rate-limited article access.", 403)
                if response.status_code != 200:
                    raise ArticleFetchError("upstream_http_error", f"The publisher returned HTTP {response.status_code}.", upstream_status=response.status_code)
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in ({"application/json"} if json_response else {"text/html", "application/xhtml+xml"}):
                    raise ArticleFetchError("invalid_article", "The publisher did not return the expected JSON feed." if json_response else "The publisher did not return HTML.", 422)
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
                    if size > config.max_bytes:
                        raise ArticleFetchError("html_too_large", "Article HTML exceeds the configured byte limit.")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > config.max_bytes:
                        raise ArticleFetchError("html_too_large", "Article HTML exceeds the configured byte limit.")
                    body.extend(chunk)
                try:
                    html = body.decode(response.encoding or "utf-8", errors="replace")
                except LookupError as exc:
                    raise ArticleFetchError("invalid_article", "The publisher returned an unsupported character encoding.", 422) from exc
                if listing:
                    return FetchedArticle(url=str(logical_url), html=html, title="")
                return validate_article_html(str(logical_url), html)


async def fetch_article(article_url: str, *, transport: httpx.AsyncBaseTransport | None = None, config: ArticleFetchConfig | None = None, listing: bool = False, json_response: bool = False) -> FetchedArticle:
    """Fetch a validated article; transport injection is only for offline tests."""
    config = config or ArticleFetchConfig.from_env()
    try:
        async with asyncio.timeout(config.total_timeout):
            return await _fetch_article(article_url, transport, config, listing, json_response)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ArticleFetchError("article_timeout", "Timed out fetching the article.", 504) from exc
    except (httpx.RequestError, OSError) as exc:
        raise ArticleFetchError("upstream_network_error", "Could not establish a secure connection to the article publisher.") from exc
