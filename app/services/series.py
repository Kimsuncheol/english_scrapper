"""Check the publisher's newest listed lesson using the shared saved pipeline."""

import asyncio
from datetime import date, datetime, timezone
import re
import time
from urllib.parse import urlsplit

from app.schemas import SavedLessonResponse, SeriesCheckResponse, SeriesExpression, SeriesListedDay, SeriesSavedResponse
from app.services.discovery import SERIES_PAGE, SERIES_TITLE, discover_articles
from app.services.errors import ArticleFetchError
from app.services.saved import process_and_store


def issue_date_from_url(url):
    match = re.fullmatch(r"/national/nie/english/(\d{4})/(\d{2})/(\d{2})/[^/]+/?", urlsplit(str(url)).path)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            pass
    return None


def expression_card(record):
    saved = SavedLessonResponse.model_validate(record)
    fields = saved.payload.lesson.fields
    return SeriesExpression(id=saved.id, article=saved.payload.article,
        expression=fields.expression.text if fields.expression else None,
        korean_meaning=fields.korean_meaning.text if fields.korean_meaning else None,
        processing_status=saved.payload.processing.status,
        lesson_status=saved.payload.lesson.status, saved_at=saved.created_at,
        issue_date=issue_date_from_url(saved.payload.article.url), lesson_image=saved.payload.lesson_image,
        lesson=saved.payload.lesson, ocr=saved.payload.ocr)


async def saved_expressions(store, limit=5, offset=0):
    # Paginate all stored records with bounded full-OCR payloads per request.
    # Offsets scan underlying records, including non-series articles, so a page
    # containing no matching lessons must still expose its continuation.
    page = await store.series_articles(limit=limit, offset=offset)
    items = []
    for summary in page["items"]:
        article = summary["article"]
        title = article.title if hasattr(article, "title") else article.get("title")
        if not title or not SERIES_TITLE.match(title):
            continue
        record = await store.lesson(summary["id"])
        if record:
            items.append(expression_card(record))
    items.sort(key=lambda item: (item.issue_date.isoformat() if item.issue_date else "", item.saved_at.timestamp()), reverse=True)
    scanned = offset + len(page["items"])
    return SeriesSavedResponse(items=items, offset=offset,
        next_offset=scanned if scanned < page["total"] else None)


class SeriesChecker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.urls = None
        self.checked_at = None
        self.expires_at = 0

    async def check(self, store):
        if self.lock.locked():
            raise ArticleFetchError("series_check_busy", "An expression check is already running. Retry shortly.", 503)
        async with self.lock:
            cached = self.urls is not None and time.monotonic() < self.expires_at
            if not cached:
                # Cache only successful discovery, never errors or failed OCR.
                self.urls = await discover_articles()
                self.checked_at = datetime.now(timezone.utc)
                self.expires_at = time.monotonic() + 60
            if not self.urls:
                return SeriesCheckResponse(status="empty", source_url=SERIES_PAGE,
                    checked_at=self.checked_at, discovery_cached=cached, listed_count=0)
            # Publisher order is the source of recency; never guess article IDs/dates.
            # One article per check keeps CPU and request duration bounded on Free.
            saved, created = await process_and_store(store, self.urls[0])
            return SeriesCheckResponse(status="new" if created else "up_to_date",
                source_url=SERIES_PAGE, checked_at=self.checked_at,
                discovery_cached=cached, listed_count=len(self.urls), latest=expression_card(saved),
                listed_days=[SeriesListedDay(article_url=url, issue_date=issue_date_from_url(url)) for url in self.urls])
