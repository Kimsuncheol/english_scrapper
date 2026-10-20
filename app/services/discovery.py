"""Discover only publisher-listed links for the requested English series."""

from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from app.services.article import fetch_article
from app.services.errors import ArticleFetchError
from app.services.parsing import read_page, nested
from app.services.urls import validate_article_url

SERIES_PAGE = "https://www.chosun.com/national/nie/english/"
SERIES_TITLE = re.compile(r"^\s*\[?\s*스피킹이\s+강해지는\s+영어\s*\]?")


def stable_url(value: str) -> str:
    url = validate_article_url(value)
    return str(url).split("?", 1)[0].split("#", 1)[0].replace("https://chosun.com/", "https://www.chosun.com/", 1)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.words = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.words = []

    def handle_data(self, data):
        if self.href is not None:
            self.words.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.items.append(("".join(self.words), self.href))
            self.href = None


def discover_from_html(html: str) -> list[str]:
    page = read_page(html)
    if any(marker in page.title.lower() for marker in ("access denied", "just a moment", "captcha")):
        raise ArticleFetchError("blocked_page", "The publisher returned a discovery challenge page.", 403)
    links = Links()
    links.feed(html)
    candidates = list(links.items)
    stack = [(page.global_content, 0), (page.content_cache, 0)]
    visited = 0
    while stack and visited < 20000:
        item, depth = stack.pop()
        visited += 1
        if depth > 40:
            continue
        if isinstance(item, dict):
            title = nested(item, "headlines", "basic")
            if isinstance(title, str):
                candidates.append((title, item.get("canonical_url") or item.get("website_url") or nested(item, "websites", "chosun", "website_url")))
            stack.extend((child, depth + 1) for child in reversed(list(item.values())) if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in reversed(item) if isinstance(child, (dict, list)))
    found = []
    for title, href in candidates:
        if not isinstance(title, str) or not SERIES_TITLE.match(title) or not isinstance(href, str):
            continue
        try:
            url = stable_url(urljoin(SERIES_PAGE, href))
        except ValueError:
            continue
        if urlsplit(url).path.startswith("/national/nie/english/") and urlsplit(url).path != "/national/nie/english/" and url not in found:
            found.append(url)
    if not found and page.global_content.get("node_type") != "section" and "영어" not in page.title:
        raise ArticleFetchError("discovery_invalid", "Discovery source did not contain a recognizable English section.", 502)
    return found[:100]


async def discover_articles() -> list[str]:
    fetched = await fetch_article(SERIES_PAGE, listing=True)
    return discover_from_html(fetched.html)
