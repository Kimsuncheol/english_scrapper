"""One idempotent processing/persistence boundary for API and scheduled work."""

import logging

from app.schemas import SavedLessonResponse
from app.services import scrape as scrape_service
from app.services.discovery import stable_url
from app.services.errors import ArticleFetchError
from app.services.storage import STORAGE_ERRORS, StorageUnavailable

logger = logging.getLogger(__name__)


async def process_and_store(store, article_url, scrape=None, *, reprocess=False) -> tuple[SavedLessonResponse, bool]:
    """Return the durable first result and whether this call created it.

    Known URLs skip all external work. Previously unseen aliases may require
    processing to learn their canonical URL; canonical uniqueness keeps the first
    committed result and all aliases resolve to it thereafter. Only an explicit
    reprocess request replaces a saved payload, with a transactional revision.
    """
    url = stable_url(article_url)
    scrape = scrape or scrape_service.scrape_article
    try:
        async with store.article_session(url) as session:
            cached = await session.cached(url)
            if cached and not reprocess:
                await session.enqueue([url])
                await session.complete(url, cached["id"])
                return SavedLessonResponse.model_validate(cached), False
            await session.enqueue([url])
            await session.attempt(url)
            try:
                result = await scrape(url)
                if reprocess and cached and stable_url(str(result.article.url)) != cached["canonical_url"]:
                    raise ArticleFetchError("canonical_changed", "The article's canonical URL changed; the saved result was preserved.", 409)
                if reprocess and cached and not result.ocr.raw_text.strip():
                    raise ArticleFetchError("empty_transcription", "No text was recognized; the previous saved result was preserved.", 422)
            except ArticleFetchError as exc:
                await session.failed(url, exc.code)
                raise
            except Exception:
                await session.failed(url, "processing_failed")
                raise
            try:
                created = await session.save(url, result, replace_existing=True) if reprocess else await session.save(url, result)
            except ArticleFetchError as exc:
                # Persist storage-size failures as retryable queue records too.
                await session.failed(url, exc.code)
                raise
            saved = await session.cached(url)
            return SavedLessonResponse.model_validate(saved), created
    except ArticleFetchError:
        raise
    except STORAGE_ERRORS as exc:
        raise StorageUnavailable() from exc
    except Exception as exc:
        logger.error("Article persistence/processing failed (%s)", type(exc).__name__)
        raise ArticleFetchError("processing_failed", "Could not process and save this article.", 500) from exc
