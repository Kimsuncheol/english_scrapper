"""Future article parsing, image download, and OCR orchestration."""

from pydantic import HttpUrl

from app.schemas import ScrapeResponse
from app.services.article import fetch_article


class ScrapingNotImplementedError(Exception):
    """Raised until the extraction pipeline is implemented."""


async def scrape_article(article_url: HttpUrl) -> ScrapeResponse:
    """Fetch HTML now; image extraction and OCR remain unimplemented."""
    await fetch_article(str(article_url))
    raise ScrapingNotImplementedError("Lesson image extraction and OCR are not implemented yet.")
