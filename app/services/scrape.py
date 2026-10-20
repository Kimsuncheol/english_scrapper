"""Article fetching and parsing orchestration."""

from pydantic import HttpUrl

from app.schemas import ScrapeResponse
from app.services.article import fetch_article
from app.services.parsing import parse_article_html
from app.services.images import download_lesson_image


async def scrape_article(article_url: HttpUrl) -> ScrapeResponse:
    """Fetch, parse, and validate image bytes; OCR is pending."""
    fetched = await fetch_article(str(article_url))
    parsed = parse_article_html(fetched.html, fetched.url)
    downloaded = await download_lesson_image(parsed.lesson_image)
    return ScrapeResponse(
        article=parsed.article, lesson_image=downloaded.metadata,
        article_images=parsed.article_images, ocr=None,
    )
