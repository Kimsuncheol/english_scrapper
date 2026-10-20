"""Bounded orchestration for the complete scrape pipeline."""

import asyncio
from dataclasses import dataclass
import logging
import math
import os
from threading import Lock
from time import perf_counter

from pydantic import HttpUrl
import anyio

from app.schemas import ProcessingStatus, ScrapeResponse
from app.services.article import fetch_article
from app.services.parsing import parse_article_html
from app.services.images import download_lesson_image
from app.services.ocr import recognize_image
from app.services.lesson import parse_lesson
from app.services.errors import ArticleFetchError
from app.services.urls import validate_article_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeConfig:
    timeout_seconds: float = 90
    max_concurrent_requests: int = 1

    def __post_init__(self):
        if isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Scrape timeout must be positive and finite")
        if isinstance(self.max_concurrent_requests, bool) or not isinstance(self.max_concurrent_requests, int) or self.max_concurrent_requests <= 0:
            raise ValueError("Scrape concurrency must be a positive integer")

    @classmethod
    def from_env(cls):
        return cls(timeout_seconds=float(os.getenv("SCRAPE_TIMEOUT_SECONDS", "90")),
            max_concurrent_requests=int(os.getenv("SCRAPE_MAX_CONCURRENT_REQUESTS", "1")))


class AdmissionGate:
    """Process-wide admission, independent of event-loop/thread identity."""
    def __init__(self):
        self._lock = Lock()
        self._active = 0

    def acquire(self, limit):
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active -= 1


admission = AdmissionGate()


async def scrape_article(article_url: HttpUrl) -> ScrapeResponse:
    """Validate and run all stages without leaking payloads into logs."""
    config = ScrapeConfig.from_env()
    try:
        url = validate_article_url(article_url)
    except ValueError as exc:
        raise ArticleFetchError("unsafe_url", "Article URL must use HTTPS on an approved Chosun host.", 422) from exc
    if not admission.acquire(config.max_concurrent_requests):
        raise ArticleFetchError("scrape_busy", "The service is processing another article. Retry later.", 503)
    started = perf_counter()
    stage = "article_fetch"
    try:
        async with asyncio.timeout(config.timeout_seconds):
            fetched = await fetch_article(str(url))
            stage = "article_parse"
            parsed = await anyio.to_thread.run_sync(parse_article_html, fetched.html, fetched.url)
            stage = "image_download"
            downloaded = await download_lesson_image(parsed.lesson_image)
            stage = "ocr"
            ocr = await recognize_image(downloaded.data)
            stage = "lesson_parse"
            lesson = await anyio.to_thread.run_sync(
                lambda: parse_lesson(ocr, downloaded.metadata.width, downloaded.metadata.height,
                    article_title=parsed.article.title)
            )
    except ArticleFetchError:
        raise
    except TimeoutError as exc:
        raise ArticleFetchError("scrape_timeout", "The scrape exceeded its processing deadline.", 504) from exc
    except Exception as exc:
        # Exception messages from dependencies may contain HTML/bytes. Log only
        # a fixed stage name and the exception class, never the exception itself.
        logger.error("Scrape stage %s failed (%s)", stage, type(exc).__name__)
        raise ArticleFetchError("processing_failed", f"Could not complete {stage} processing.", 500) from exc
    finally:
        admission.release()
    notes = list(ocr.warnings) + list(lesson.reasons)
    if downloaded.metadata.is_fallback:
        notes.append("The selected image is an og:image fallback and may be a thumbnail.")
    partial = ocr.status != "ok" or lesson.status != "structured" or bool(notes)
    return ScrapeResponse(
        article=parsed.article, lesson_image=downloaded.metadata,
        article_images=parsed.article_images, ocr=ocr, lesson=lesson,
        processing=ProcessingStatus(status="partial" if partial else "completed",
            completed_stages=["url_validation", "article_fetch", "article_parse", "image_download", "ocr", "lesson_parse"],
            ocr_status=ocr.status, lesson_status=lesson.status,
            elapsed_ms=(perf_counter() - started) * 1000, warnings=list(dict.fromkeys(notes))),
    )
