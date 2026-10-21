"""Externally triggered daily job, with an opt-in local development timer."""

import asyncio
from datetime import datetime, timezone
import logging
import random
import time

from app.daily_config import DailyConfig
from app.services.discovery import discover_articles
from app.services.errors import ArticleFetchError
from app.services.storage import FirestoreStore
from app.services.saved import process_and_store

logger = logging.getLogger(__name__)


def transient(error):
    return isinstance(error, ArticleFetchError) and (
        error.code in {"article_timeout", "upstream_network_error", "image_timeout", "image_network_error", "scrape_timeout", "scrape_busy", "ocr_busy"}
        or (error.upstream_status is not None and 500 <= error.upstream_status <= 599))


async def retry(operation, config, sleep=asyncio.sleep):
    for attempt in range(config.retry_attempts):
        try:
            return await operation()
        except ArticleFetchError as exc:
            if not transient(exc) or attempt == config.retry_attempts - 1:
                raise
            delay = min(20, config.retry_base_seconds * 2 ** attempt)
            await sleep(delay + random.uniform(0, delay * 0.1))


class DailyRunner:
    def __init__(self, config: DailyConfig, store=None, discover=discover_articles, scrape=None, sleep=asyncio.sleep):
        self.config = config
        self.store = store or FirestoreStore(config.firebase_project_id, config.firestore_database)
        self.discover, self.scrape, self.sleep = discover, scrape, sleep
        self.task = None
        self.timer = None
        self.error_code = None
        self.retry_not_before = 0
        self.completed_day = None

    def trigger(self, now=None):
        now = now or datetime.now(timezone.utc)
        if not self.config.enabled:
            return "disabled"
        if self.config.due_date(now) is None:
            return "not_due"
        if self.completed_day == self.config.due_date(now):
            return "already_completed"
        if self.task and not self.task.done():
            return "already_running"
        if time.monotonic() < self.retry_not_before:
            return "cooldown"
        self.task = asyncio.create_task(self.run_due(now))
        return "accepted"

    async def run_due(self, now):
        if not self.config.enabled or self.config.due_date(now) is None:
            return "disabled" if not self.config.enabled else "not_due"
        stats = {"discovered": 0, "saved": 0, "failed": 0, "skipped": 0}
        session, run_id = None, None
        try:
            async with self.store.run_session() as session:
                if session is None:
                    return "already_running"
                run_id = await session.start(self.config.due_date(now))
                if run_id is None:
                    self.completed_day = self.config.due_date(now)
                    return "already_completed"
                try:
                    # Retain pending/failed URLs even if discovery fails today.
                    discovery_error = None
                    urls = []
                    try:
                        urls = await retry(self.discover, self.config, self.sleep)
                        stats["discovered"] = len(urls)
                        stats["skipped"] += await session.enqueue(urls) or 0
                    except ArticleFetchError as exc:
                        discovery_error = exc.code
                    pending = await session.pending(self.config.max_articles, preferred_urls=urls)
                    for item in pending:
                        url = item["url"]
                        async def process():
                            await session.check_connection()
                            return await process_and_store(self.store, url, self.scrape)
                        try:
                            _, saved = await retry(process, self.config, self.sleep)
                            stats["saved" if saved else "skipped"] += 1
                        except ArticleFetchError as exc:
                            await session.failed(url, exc.code)
                            stats["failed"] += 1
                            if exc.code in {"blocked_page", "image_blocked"}:
                                break
                        await self.sleep(self.config.request_gap_seconds)
                    stats["remaining"] = await session.remaining()
                    state = "failed" if discovery_error and not pending else "partial" if stats["failed"] or discovery_error else "completed"
                    await session.finish(run_id, state, stats, discovery_error)
                    self.completed_day = self.config.due_date(now)
                    self.error_code = None
                    return state
                except asyncio.CancelledError:
                    await session.finish(run_id, "interrupted", stats, "interrupted")
                    raise
                except Exception as exc:
                    await session.finish(run_id, "failed", stats, "job_failed")
                    logger.error("Daily job failed (%s)", type(exc).__name__)
                    self.error_code = "job_failed"
                    self.completed_day = self.config.due_date(now)
                    return "failed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log database URLs, credentials, content, or exception text.
            logger.error("Daily storage unavailable (%s)", type(exc).__name__)
            self.error_code = "storage_unavailable"
            self.retry_not_before = time.monotonic() + 300
            return "failed"

    async def _loop(self):
        while True:
            self.trigger()
            await asyncio.sleep(30)

    def start(self):
        if self.config.enabled and self.config.in_process:
            self.timer = asyncio.create_task(self._loop())

    async def stop(self):
        tasks = [task for task in (self.timer, self.task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(self.store, FirestoreStore):
            await self.store.close()
