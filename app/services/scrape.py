"""Future article parsing, image download, and OCR orchestration."""

from pydantic import HttpUrl

from app.schemas import ScrapeResponse


class ScrapingNotImplementedError(Exception):
    """Raised until the extraction pipeline is implemented."""


async def scrape_article(article_url: HttpUrl) -> ScrapeResponse:
    """Service stub: makes no network requests and writes no files."""
    raise ScrapingNotImplementedError("Article scraping and OCR are not implemented yet.")
