"""Shared, network-free article URL policy."""

from urllib.parse import urlsplit

from pydantic import HttpUrl

APPROVED_HOSTS = frozenset({"chosun.com", "www.chosun.com"})


def validate_article_url(value: str | HttpUrl) -> HttpUrl:
    raw = str(value)
    if any(character.isspace() or ord(character) < 32 for character in raw) or "\\" in raw:
        raise ValueError("article_url must not contain whitespace, control characters, or backslashes")
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ValueError("article_url must be an absolute HTTPS URL")
    url = HttpUrl(raw)
    if url.host not in APPROVED_HOSTS:
        raise ValueError("article_url host must be chosun.com or www.chosun.com")
    if url.username is not None or url.password is not None:
        raise ValueError("article_url must not include credentials")
    if url.port != 443:
        raise ValueError("article_url must use the standard HTTPS port 443")
    return url
