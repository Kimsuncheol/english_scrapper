"""Safe, bounded image downloads and decoding entirely in memory."""

import asyncio
from dataclasses import dataclass
from io import BytesIO
import math
from urllib.parse import urljoin, urlsplit
import warnings

import anyio
import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import HttpUrl

from app.schemas import LessonImageMetadata
from app.services.article import resolve_public_ip, USER_AGENT
from app.services.errors import ArticleFetchError

# Explicit publisher/CDN hosts, not a suffix wildcard. The Arc CDN is shared
# between publishers, so only the Chosun namespace is approved there.
APPROVED_IMAGE_HOSTS = frozenset({
    "images.chosun.com", "cloudfront-ap-northeast-1.images.arcpublishing.com",
})
ARC_IMAGE_HOST = "cloudfront-ap-northeast-1.images.arcpublishing.com"
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", "image/gif": "GIF"}
DECODE_LIMITER = anyio.CapacityLimiter(1)


class ImageDownloadError(ArticleFetchError):
    """Uses the existing application-error envelope and handler."""


@dataclass(frozen=True)
class ImageDownloadConfig:
    max_bytes: int = 10 * 1024 * 1024
    max_pixels: int = 16_000_000
    max_dimension: int = 20_000
    connect_timeout: float = 5
    read_timeout: float = 10
    total_timeout: float = 30
    max_redirects: int = 3

    def __post_init__(self):
        for value in (self.max_bytes, self.max_pixels, self.max_dimension):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Image size limits must be positive integers")
        for value in (self.connect_timeout, self.read_timeout, self.total_timeout):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Image timeouts must be positive finite numbers")
        if isinstance(self.max_redirects, bool) or not isinstance(self.max_redirects, int) or self.max_redirects < 0:
            raise ValueError("max_redirects must be a nonnegative integer")


@dataclass(frozen=True)
class DownloadedImage:
    data: bytes
    metadata: LessonImageMetadata


def validate_image_url(value: str | HttpUrl) -> HttpUrl:
    raw = str(value)
    try:
        if "\\" in raw or any(character.isspace() or ord(character) < 32 for character in raw):
            raise ValueError
        parts = urlsplit(raw)
        if parts.scheme.lower() != "https" or not parts.netloc:
            raise ValueError
        url = HttpUrl(raw)
        if url.host not in APPROVED_IMAGE_HOSTS or url.port != 443 or url.username is not None or url.password is not None:
            raise ValueError
        if url.host == ARC_IMAGE_HOST:
            # Reject escaped separators and traversal on the shared CDN.
            if not url.path.startswith("/chosun/") or "%" in url.path or any(part in {".", ".."} for part in parts.path.split("/")):
                raise ValueError
        return url
    except ValueError as exc:
        raise ImageDownloadError("unsafe_image_url", "Image URL must use HTTPS on an approved Chosun image host and namespace, without credentials or a custom port.", 422) from exc


def validate_image_bytes(data: bytes, content_type: str, config: ImageDownloadConfig) -> tuple[int, int, str]:
    """Verify structure, then reopen and fully decode before accepting bytes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=list(IMAGE_FORMATS.values())) as image:
                width, height = image.size
                image_format = image.format
                if width * height > config.max_pixels or max(width, height) > config.max_dimension:
                    raise ImageDownloadError("image_dimensions_exceeded", "Decoded image exceeds the configured pixel or dimension limit.", 422)
                if image_format != IMAGE_FORMATS[content_type]:
                    raise ImageDownloadError("invalid_image", "Image bytes do not match the declared Content-Type.", 422)
                if getattr(image, "is_animated", False):
                    raise ImageDownloadError("invalid_image", "Animated lesson images are not supported.", 422)
                image.verify()
            with Image.open(BytesIO(data), formats=[image_format]) as image:
                image.load()
            return width, height, image_format
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImageDownloadError("image_dimensions_exceeded", "Image exceeds the decoder's safety limit.", 422) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ImageDownloadError("invalid_image", "Downloaded bytes are not a complete supported image.", 422) from exc


async def _download(url: HttpUrl, config: ImageDownloadConfig, transport) -> tuple[bytes, str, HttpUrl]:
    current = str(url)
    timeout = httpx.Timeout(connect=config.connect_timeout, read=config.read_timeout, write=5, pool=5)
    async with httpx.AsyncClient(
        timeout=timeout, trust_env=False, follow_redirects=False, transport=transport,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        for hop in range(config.max_redirects + 1):
            logical = validate_image_url(current)
            try:
                address = await resolve_public_ip(logical.host)
            except ArticleFetchError as exc:
                raise ImageDownloadError("unsafe_image_destination", "Image host must resolve only to public IP addresses.", 422) from exc
            target = httpx.URL(str(logical)).copy_with(host=address, fragment=None)
            async with client.stream(
                "GET", target,
                headers={"Host": logical.host, "User-Agent": USER_AGENT, "Accept": ", ".join(IMAGE_FORMATS), "Accept-Encoding": "identity"},
                extensions={"sni_hostname": logical.host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == config.max_redirects:
                        raise ImageDownloadError("image_redirect_error", "Image redirect is missing a destination or exceeds the configured limit.")
                    current = urljoin(str(logical), location)
                    continue
                if response.status_code in {401, 403, 429}:
                    raise ImageDownloadError("image_blocked", "The publisher denied or rate-limited image access.", 403)
                if response.status_code != 200:
                    raise ImageDownloadError("image_http_error", f"Image host returned HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in IMAGE_FORMATS:
                    raise ImageDownloadError("invalid_image_content_type", "Image response must be JPEG, PNG, WebP, or GIF.", 422)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ImageDownloadError("image_encoding_error", "Image host returned unexpected HTTP compression.")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                        if size < 0:
                            raise ValueError
                    except ValueError as exc:
                        raise ImageDownloadError("image_http_error", "Image host returned an invalid Content-Length.") from exc
                    if size > config.max_bytes:
                        raise ImageDownloadError("image_too_large", "Image response exceeds the configured byte limit.")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > config.max_bytes:
                        raise ImageDownloadError("image_too_large", "Image response exceeds the configured byte limit.")
                    body.extend(chunk)
                return bytes(body), content_type, logical


async def download_lesson_image(
    image: LessonImageMetadata, *, config: ImageDownloadConfig | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadedImage:
    """Download the parser-selected URL; never approve a host from page data.

    Accepts image metadata discovered in an article, not arbitrary request URLs.
    Redirects may use another explicitly approved publisher image host.
    """
    config = config or ImageDownloadConfig()
    url = validate_image_url(image.url)
    try:
        async with asyncio.timeout(config.total_timeout):
            data, content_type, final_url = await _download(url, config, transport)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise ImageDownloadError("image_timeout", "Timed out downloading the lesson image.", 504) from exc
    except (httpx.RequestError, OSError) as exc:
        raise ImageDownloadError("image_network_error", "Could not establish a secure connection to the image host.") from exc
    width, height, image_format = await anyio.to_thread.run_sync(
        validate_image_bytes, data, content_type, config, limiter=DECODE_LIMITER,
    )
    metadata = image.model_copy(update={
        "width": width, "height": height, "content_type": content_type,
        "byte_size": len(data), "format": image_format, "download_url": final_url,
    })
    return DownloadedImage(data=data, metadata=metadata)
