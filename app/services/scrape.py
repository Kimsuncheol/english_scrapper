"""Article fetching and parsing orchestration."""

from pydantic import HttpUrl

from app.schemas import ScrapeResponse
from app.services.article import fetch_article
from app.services.parsing import parse_article_html


async def scrape_article(article_url: HttpUrl) -> ScrapeResponse:
    """Fetch and parse the article; image downloading and OCR are pending."""
    fetched = await fetch_article(str(article_url))
    parsed = parse_article_html(fetched.html, fetched.url)
    return ScrapeResponse(
        article=parsed.article, lesson_image=parsed.lesson_image,
        article_images=parsed.article_images, ocr=None,
    )
