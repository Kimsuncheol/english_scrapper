"""Pure HTML/page-data parsing. Embedded JavaScript is never executed."""

from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from ipaddress import ip_address
import json
import re
from urllib.parse import urljoin

from pydantic import HttpUrl, ValidationError

from app.schemas import ArticleMetadata, LessonImageMetadata
from app.services.errors import ArticleFetchError
from app.services.urls import validate_article_url

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_TREE_DEPTH = 40
ARTICLE_TYPES = {"Article", "NewsArticle", "ReportageNewsArticle"}
ASSIGNMENT = re.compile(r"(?:window\s*\.\s*)?Fusion\s*\.\s*globalContent\s*=\s*(?!=)")
JSON_PARSE = re.compile(r"JSON\s*\.\s*parse\s*\(\s*")
JSON_PARSE_CLOSE = re.compile(r"\s*\)")
WHITESPACE = re.compile(r"\s*")
STRING_OR_COMMENT = re.compile(
    r'''"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|`(?:\\[\s\S]|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/'''
)


def decode_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def fusion_content(script: str, variable: str = "globalContent") -> dict:
    """Read a JSON object assignment (or JSON.parse of a JSON string) only.

    A small lexical scan skips strings/comments, including braces within them.
    raw_decode finds the JSON boundary; expression suffixes are rejected.
    Unsupported JavaScript object literals are deliberately not interpreted.
    """
    decoder = json.JSONDecoder()
    if variable not in {"globalContent", "contentCache"}:
        raise ValueError("Unsupported Fusion data variable")
    assignment = ASSIGNMENT if variable == "globalContent" else re.compile(r"(?:window\s*\.\s*)?Fusion\s*\.\s*contentCache\s*=\s*(?!=)")
    position = 0
    while position < len(script):
        skipped = STRING_OR_COMMENT.match(script, position)
        if skipped:
            position = skipped.end()
            continue
        match = assignment.match(script, position)
        if match and (position == 0 or not (script[position - 1].isalnum() or script[position - 1] in "_$.")):
            start = match.end()
            wrapped = JSON_PARSE.match(script, start)
            if wrapped:
                start = wrapped.end()
            try:
                value, end = decoder.raw_decode(script, start)
                if wrapped:
                    closing = JSON_PARSE_CLOSE.match(script, end)
                    if not closing or not isinstance(value, str):
                        raise ValueError
                    end = closing.end()
                    value = decode_json(value)
                end = WHITESPACE.match(script, end).end()
                if isinstance(value, dict) and (end == len(script) or script[end] == ";"):
                    return value
            except (ValueError, RecursionError):
                pass
        position += 1
    return {}


class PageHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.canonical = None
        self.language = None
        self.title_parts = []
        self.in_title = False
        self.script_type = None
        self.script_parts = []
        self.jsonld = []
        self.invalid_jsonld = False
        self.global_content = {}
        self.content_cache = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.language = attrs.get("lang")
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content"):
                self.meta.setdefault(key.lower(), attrs["content"])
        elif tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical = attrs.get("href")
        elif tag == "script":
            self.script_type = (attrs.get("type") or "text/javascript").lower().split(";", 1)[0].strip()
            self.script_parts = []

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.script_type is not None:
            self.script_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "script" and self.script_type is not None:
            script = "".join(self.script_parts)
            if self.script_type == "application/ld+json":
                value = decode_json(script)
                if value is not None:
                    self.jsonld.append(value)
                elif script.strip() != "null":
                    self.invalid_jsonld = True
            elif self.script_type in {"text/javascript", "application/javascript", "module"}:
                if not self.global_content:
                    self.global_content = fusion_content(script)
                if not self.content_cache:
                    self.content_cache = fusion_content(script, "contentCache")
            self.script_type = None
            self.script_parts = []

    @property
    def title(self):
        return "".join(self.title_parts).strip()


def article_nodes(value, depth=0):
    if depth > MAX_TREE_DEPTH:
        return
    if isinstance(value, list):
        for child in value:
            yield from article_nodes(child, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(isinstance(item, str) and item in ARTICLE_TYPES for item in types):
            yield value
        yield from article_nodes(value.get("@graph", []), depth + 1)


def read_page(html: str) -> PageHTML:
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ArticleFetchError("html_too_large", "Article HTML exceeds the 2 MiB parsing limit.")
    page = PageHTML()
    try:
        page.feed(html)
        page.close()
    except (ValueError, RecursionError) as exc:
        raise ArticleFetchError("invalid_article", "Unable to parse the article HTML.", 422) from exc
    return page


def text(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        return next((result for item in value if (result := text(item))), None)
    return None


def nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def article_url(value, base):
    if isinstance(value, dict):
        value = value.get("@id") or value.get("url")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return validate_article_url(urljoin(base, value))
    except ValueError:
        return None


def metadata_from_page(page: PageHTML, source_url: str) -> ArticleMetadata:
    data = page.global_content
    nodes = list(article_nodes(page.jsonld))
    canonical = article_url(page.canonical, source_url)
    target = str(canonical or source_url).split("?", 1)[0].rstrip("/")
    node = next((node for node in nodes if str(article_url(
        node.get("mainEntityOfPage") or node.get("url"), source_url
    ) or "").split("?", 1)[0].rstrip("/") == target), nodes[0] if nodes else {})
    url = canonical or article_url(node.get("mainEntityOfPage"), source_url) or article_url(node.get("url"), source_url)
    url = url or article_url(data.get("canonical_url"), source_url) or article_url(
        nested(data, "websites", "chosun", "website_url"), source_url
    ) or article_url(data.get("website_url"), source_url) or validate_article_url(source_url)
    published = None
    for raw in (node.get("datePublished"), data.get("publish_date"), data.get("display_date"), data.get("first_publish_date")):
        if isinstance(raw, str):
            try:
                published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                break
            except ValueError:
                pass
    return ArticleMetadata(
        url=url,
        title=text(node.get("headline")) or text(nested(data, "headlines", "basic")) or text(node.get("name")) or text(page.meta.get("og:title")) or text(page.title),
        published_at=published,
        section=text(node.get("articleSection")) or text(nested(data, "taxonomy", "primary_section", "name")) or text(nested(data, "websites", "chosun", "website_section", "name")),
        language=text(node.get("inLanguage")) or text(data.get("language")) or text(page.language) or text(page.meta.get("og:locale")),
    )


def image_url(value, base):
    """Validate metadata URLs without fetching or trusting them as destinations."""
    if not isinstance(value, str) or not value.strip() or "\\" in value or any(character.isspace() for character in value):
        return None
    try:
        url = HttpUrl(urljoin(base, value))
        if url.username is not None or url.password is not None or url.host in {"localhost", "localhost.localdomain"} or url.host.endswith(".localhost"):
            return None
        try:
            address = ip_address(url.host.strip("[]"))
            if not address.is_global or (getattr(address, "ipv4_mapped", None) is not None and not address.ipv4_mapped.is_global):
                return None
        except ValueError:
            pass
        return url
    except (ValueError, ValidationError):
        return None


def dimension(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and 0 < len(value) < 10:
        return int(value) or None
    return None


def collect_images(elements, base, path=(), depth=0):
    if not isinstance(elements, list) or depth > MAX_TREE_DEPTH:
        return
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        position = (*path, index)
        if element.get("type") == "image":
            additional = element.get("additional_properties")
            additional = additional if isinstance(additional, dict) else {}
            url = next((url for raw in (additional.get("originalUrl"), additional.get("original_url"), element.get("url")) if (url := image_url(raw, base))), None)
            if url:
                resized = {}
                for container in (element, additional):
                    for key in ("resized_urls", "resizedUrls", "resizer_urls"):
                        variants = container.get(key)
                        if isinstance(variants, dict):
                            for name, raw in variants.items():
                                value = raw.get("url") if isinstance(raw, dict) else raw
                                if variant := image_url(value, base):
                                    resized[str(name)] = variant
                    for key in ("resizeUrl", "thumbnailResizeUrl", "fullSizeResizeUrl", "proxyUrl"):
                        if variant := image_url(container.get(key), base):
                            resized[key] = variant
                yield LessonImageMetadata(
                    url=url, width=dimension(element.get("width")), height=dimension(element.get("height")),
                    alt_text=text(element.get("alt_text")) or text(element.get("caption")) or text(element.get("subtitle")),
                    resized_urls=resized, content_element_path=list(position),
                )
        # Only traverse article structure, never promo_items or related stories.
        if element.get("type") in {"gallery", "container", "group"}:
            yield from collect_images(element.get("content_elements"), base, position, depth + 1)


@dataclass(frozen=True)
class ImageSelectionConfig:
    min_width: int = 300
    min_height: int = 300
    tall_aspect_ratio: float = 1.2
    allow_unknown_dimensions: bool = True
    allow_og_fallback: bool = True
    prefer_tall_images: bool = True

    def __post_init__(self):
        if self.min_width <= 0 or self.min_height <= 0 or self.tall_aspect_ratio <= 0:
            raise ValueError("Image selection thresholds must be positive")


def select_lesson_image(images, fallback, config: ImageSelectionConfig):
    suitable = []
    for image in images:
        if image.width is not None and image.width < config.min_width:
            continue
        if image.height is not None and image.height < config.min_height:
            continue
        if not config.allow_unknown_dimensions and (image.width is None or image.height is None):
            continue
        suitable.append(image)

    def score(image):
        known = image.width is not None and image.height is not None
        ratio = image.height / image.width if known else 0
        # Prefer tall graphics, then known dimensions, then taller/larger images.
        # Stable max() keeps earlier article positions on ties, including direct
        # body images before nested gallery entries at the same position.
        area = image.width * image.height if known else 0
        if not config.prefer_tall_images:
            return (known, area, ratio)
        return (ratio >= config.tall_aspect_ratio, known, ratio, area)

    if suitable:
        return max(suitable, key=score)
    if config.allow_og_fallback and fallback is not None:
        return fallback
    raise ArticleFetchError("no_lesson_image", "No suitable article image or usable og:image fallback was found.", 422)


@dataclass(frozen=True)
class ParsedArticle:
    article: ArticleMetadata
    lesson_image: LessonImageMetadata
    article_images: list[LessonImageMetadata] = field(default_factory=list)


def parse_article_html(html: str, source_url: str, config: ImageSelectionConfig | None = None) -> ParsedArticle:
    """Extract metadata and image candidates without any I/O."""
    page = read_page(html)
    metadata = metadata_from_page(page, source_url)
    images = list(collect_images(page.global_content.get("content_elements"), str(metadata.url)))
    fallback = None
    if url := image_url(page.meta.get("og:image"), str(metadata.url)):
        fallback = LessonImageMetadata(
            url=url, width=dimension(page.meta.get("og:image:width")),
            height=dimension(page.meta.get("og:image:height")),
            alt_text=text(page.meta.get("og:image:alt")), source="og:image", is_fallback=True,
        )
    try:
        selected = select_lesson_image(images, fallback, config or ImageSelectionConfig())
    except ArticleFetchError as exc:
        if exc.code == "no_lesson_image" and page.invalid_jsonld and not page.jsonld and not page.global_content:
            raise ArticleFetchError("invalid_article_data", "Article data is malformed and no usable lesson image was found.", 422) from exc
        raise
    return ParsedArticle(article=metadata, lesson_image=selected, article_images=images)
