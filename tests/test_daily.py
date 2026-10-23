import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from app.daily_config import DailyConfig
from app.main import app
from app.schemas import ScrapeResponse
from app.services.daily import DailyRunner, retry
from app.services.discovery import discover_from_html, stable_url
from app.services.errors import ArticleFetchError

NOW = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
ONE = "https://www.chosun.com/national/nie/english/2026/10/21/ONE/"
TWO = "https://www.chosun.com/national/nie/english/2026/10/20/TWO/"


def result(url=ONE):
    return ScrapeResponse(article={"url": url, "title": "Lesson"},
        lesson_image={"url": "https://images.chosun.com/test.png", "width": 1000, "height": 1500},
        ocr={"raw_text": "안녕 Hello"}, lesson={"status": "unparsed", "reasons": ["Missing section labels"]},
        processing={"status": "partial", "completed_stages": ["url_validation", "article_fetch", "article_parse", "image_download", "ocr", "lesson_parse"],
            "ocr_status": "ok", "lesson_status": "unparsed", "elapsed_ms": 1})


def config(**kwargs):
    return DailyConfig(enabled=True, firebase_project_id="demo-test", request_gap_seconds=0,
        retry_base_seconds=0, **kwargs)


class MemoryStore:
    """Test double for runner behavior; Firestore leases have separate emulator tests."""
    def __init__(self):
        self.locked = False
        self.runs = {}
        self.items = {}
        self.saved = {}
        self.article_locks = set()

    @asynccontextmanager
    async def article_session(self, url):
        if url in self.article_locks:
            raise ArticleFetchError("scrape_busy", "This article is being processed. Retry later.", 503)
        self.article_locks.add(url)
        try:
            yield self
        finally:
            self.article_locks.remove(url)

    async def cached(self, url):
        canonical = self.items.get(url, {}).get("canonical", url)
        value = self.saved.get(canonical)
        if value is None:
            return None
        return {"id": str(uuid5(NAMESPACE_URL, canonical)), "canonical_url": canonical,
            "payload": value.model_dump(mode="json"), "created_at": NOW, "updated_at": NOW}

    async def complete(self, url, lesson_id):
        canonical = next(key for key in self.saved if str(uuid5(NAMESPACE_URL, key)) == lesson_id)
        self.items[url].update(state="succeeded", canonical=canonical, error_code=None)

    async def check_connection(self):
        pass

    async def lesson(self, lesson_id):
        for url in self.saved:
            record = await self.cached(url)
            if record["id"] == lesson_id:
                return record
        return None

    async def articles(self, limit=20, offset=0):
        items = []
        for url, value in self.saved.items():
            record = await self.cached(url)
            record.pop("payload")
            items.append({**record, "article": value.article, "processing_status": value.processing.status})
        return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}

    async def series_articles(self, limit=5, offset=0):
        page = await self.articles(limit=len(self.saved) or 1)
        items = [row for row in page["items"] if row["canonical_url"].startswith("https://www.chosun.com/national/nie/english/")]
        items.sort(key=lambda row: row["canonical_url"], reverse=True)
        return {"items": items[offset:offset + limit], "total": len(items)}

    @asynccontextmanager
    async def run_session(self):
        if self.locked:
            yield None
            return
        self.locked = True
        try:
            yield self
        finally:
            self.locked = False

    async def start(self, day):
        if self.runs.get(day, {}).get("status") in {"completed", "partial", "failed"}:
            return None
        self.runs[day] = {"status": "running"}
        return day

    async def enqueue(self, urls):
        for url in urls:
            self.items.setdefault(stable_url(url), {"state": "pending", "attempts": 0})

    async def pending(self, limit, preferred_urls=()):
        pending = [url for url, value in self.items.items() if value["state"] != "succeeded"]
        preferred = list(dict.fromkeys(stable_url(url) for url in preferred_urls))
        pending.sort(key=lambda url: preferred.index(url) if url in preferred else len(preferred))
        return [{"url": url} for url in pending[:limit]]

    async def remaining(self):
        return sum(value["state"] != "succeeded" for value in self.items.values())

    async def attempt(self, url):
        self.items[url]["attempts"] += 1
        self.items[url]["state"] = "processing"

    async def save(self, url, response, *, replace_existing=False):
        canonical = stable_url(response.article.url)
        is_new = canonical not in self.saved
        if replace_existing:
            if not is_new:
                self.revisions = getattr(self, "revisions", []) + [self.saved[canonical]]
            self.saved[canonical] = response
        else:
            self.saved.setdefault(canonical, response)
        self.items[url].update(state="succeeded", canonical=canonical)
        return is_new

    async def failed(self, url, code):
        self.items[url].update(state="failed", error_code=code)

    async def finish(self, day, state, stats, code=None):
        self.runs[day] = {"status": state, "stats": dict(stats), "error_code": code}


def test_schedule_defaults_and_timezone_boundaries(monkeypatch):
    cfg = DailyConfig.from_env()
    assert not cfg.enabled
    assert not cfg.in_process
    assert (cfg.timezone, cfg.hour, cfg.minute) == ("Asia/Seoul", 6, 0)
    assert cfg.due_date(datetime(2026, 10, 20, 20, 59, tzinfo=timezone.utc)) is None
    assert str(cfg.due_date(datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc))) == "2026-10-21"
    assert cfg.next_due(datetime(2026, 10, 20, 20, 59, tzinfo=timezone.utc)).hour == 21
    monkeypatch.setenv("SCRAPE_TIMEZONE", "UTC")
    monkeypatch.setenv("SCRAPE_HOUR", "12")
    monkeypatch.setenv("SCRAPE_MINUTE", "30")
    assert DailyConfig.from_env().due_date(datetime(2026, 10, 21, 12, 29, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize("name,value", [("SCRAPE_ENABLED", "maybe"), ("SCRAPE_IN_PROCESS", "maybe"), ("SCRAPE_TIMEZONE", "invalid"), ("SCRAPE_HOUR", "24"), ("SCRAPE_MINUTE", "60"), ("FIRESTORE_DATABASE", "invalid/path"), ("SCRAPE_RETRY_ATTEMPTS", "20")])
def test_invalid_schedule_config(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises((ValueError, KeyError)):
        DailyConfig.from_env()


def test_enabled_requires_real_storage(monkeypatch):
    monkeypatch.setenv("SCRAPE_ENABLED", "true")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    with pytest.raises(ValueError, match="FIREBASE_PROJECT_ID"):
        DailyConfig.from_env()


def test_discovery_filters_real_listed_series_links():
    html = (Path(__file__).parent / "fixtures/daily_discovery.html").read_text()
    assert discover_from_html(html) == [ONE, TWO]
    assert stable_url(ONE + "?utm_source=x#part") == ONE
    assert stable_url(ONE.replace("www.chosun.com", "chosun.com")) == ONE
    # No date/id generation and no executable JavaScript evaluation.
    assert discover_from_html('<title>영어 섹션</title><script>Fusion.contentCache=attack();</script>') == []


def test_challenge_discovery_is_not_silently_empty():
    with pytest.raises(ArticleFetchError) as error:
        discover_from_html('<title>Access Denied</title>')
    assert error.value.code == "blocked_page"


def test_daily_urls_are_not_processed_again_after_restart():
    async def run():
        store = MemoryStore()
        scrape = AsyncMock(return_value=result())
        discover = AsyncMock(return_value=[ONE, ONE + "?tracking=1"])
        for _ in range(2):
            runner = DailyRunner(config(), store, discover, scrape)
            await runner.run_due(NOW)
        assert scrape.await_count == 1
        tomorrow = NOW.replace(day=22)
        await DailyRunner(config(), store, discover, scrape).run_due(tomorrow)
        assert scrape.await_count == 1
        assert len(store.saved) == 1
    asyncio.run(run())


def test_new_daily_lesson_is_not_delayed_by_historical_backlog():
    async def run():
        store = MemoryStore()
        await store.enqueue([TWO])  # Older article queued by an earlier run.
        scrape = AsyncMock(return_value=result())
        runner = DailyRunner(config(max_articles=1), store, AsyncMock(return_value=[ONE, TWO]), scrape)
        assert await runner.run_due(NOW) == "completed"
        scrape.assert_awaited_once_with(ONE)
        assert store.items[ONE]["state"] == "succeeded"
        assert store.items[TWO]["state"] == "pending"
    asyncio.run(run())


def test_overlapping_runners_cannot_run_twice():
    async def run():
        store = MemoryStore()
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover():
            entered.set()
            await release.wait()
            return [ONE]
        scrape = AsyncMock(return_value=result())
        first = DailyRunner(config(), store, discover, scrape)
        second = DailyRunner(config(), store, discover, scrape)
        task = asyncio.create_task(first.run_due(NOW))
        await entered.wait()
        assert await second.run_due(NOW) == "already_running"
        release.set()
        await task
        assert scrape.await_count == 1
        assert not store.locked
    asyncio.run(run())


def test_retry_is_bounded_exponential_and_does_not_retry_404():
    async def run():
        sleep = AsyncMock()
        operation = AsyncMock(side_effect=ArticleFetchError("article_timeout", "timeout", 504))
        cfg = DailyConfig(enabled=True, firebase_project_id="demo-test", retry_base_seconds=2)
        with pytest.raises(ArticleFetchError):
            await retry(operation, cfg, sleep)
        assert operation.await_count == 3
        delays = [call.args[0] for call in sleep.await_args_list]
        assert 2 <= delays[0] <= 2.2 and 4 <= delays[1] <= 4.4
        operation = AsyncMock(side_effect=ArticleFetchError("upstream_http_error", "404", upstream_status=404))
        with pytest.raises(ArticleFetchError):
            await retry(operation, cfg, sleep)
        assert operation.await_count == 1
    asyncio.run(run())


def test_failure_saved_then_retried_next_day_even_if_discovery_misses_it():
    async def run():
        store = MemoryStore()
        scrape = AsyncMock(side_effect=ArticleFetchError("article_timeout", "timeout", 504))
        first = DailyRunner(config(), store, AsyncMock(return_value=[ONE]), scrape)
        assert await first.run_due(NOW) == "partial"
        assert store.items[ONE]["attempts"] == 3
        assert store.items[ONE]["error_code"] == "article_timeout"
        second = DailyRunner(config(), store, AsyncMock(return_value=[]), AsyncMock(return_value=result()))
        assert await second.run_due(NOW.replace(day=22)) == "completed"
        assert store.items[ONE]["state"] == "succeeded"
    asyncio.run(run())


def test_transient_retry_success_and_blocked_page_stop():
    async def run():
        store = MemoryStore()
        scrape = AsyncMock(side_effect=[ArticleFetchError("upstream_network_error", "network"), result()])
        runner = DailyRunner(config(), store, AsyncMock(return_value=[ONE]), scrape)
        assert await runner.run_due(NOW) == "completed"
        assert store.items[ONE]["attempts"] == 2
        store = MemoryStore()
        scrape = AsyncMock(side_effect=ArticleFetchError("blocked_page", "blocked", 403))
        runner = DailyRunner(config(), store, AsyncMock(return_value=[ONE, TWO]), scrape)
        assert await runner.run_due(NOW) == "partial"
        assert scrape.await_count == 1
        assert store.items[TWO]["state"] == "pending"
    asyncio.run(run())


def test_disabled_status_and_authenticated_trigger(monkeypatch):
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.setenv("SCRAPE_TRIGGER_TOKEN", "long-test-token")
    with TestClient(app) as client:
        status = client.get("/api/v1/jobs/daily")
        assert status.status_code == 200
        assert not status.json()["enabled"]
        assert status.json()["last_run"] is None
        assert client.post("/api/v1/jobs/daily/trigger").status_code == 401
        assert client.post("/api/v1/jobs/daily/trigger", headers={"Authorization": "long-test-token"}).status_code == 401
        assert client.post("/api/v1/jobs/daily/trigger", headers={b"Authorization": b"Bearer invalid-\xff"}).status_code == 401
        response = client.post("/api/v1/jobs/daily/trigger", headers={"Authorization": "Bearer long-test-token"})
        assert response.status_code == 202 and response.json()["status"] == "disabled"
        assert client.get("/api/v1/lessons").status_code == 503
        assert client.get("/health").status_code == 200


def test_cancellation_records_interruption_and_releases_lock():
    async def run():
        store = MemoryStore()
        entered = asyncio.Event()
        async def scrape(url):
            entered.set()
            await asyncio.Event().wait()
        runner = DailyRunner(config(), store, AsyncMock(return_value=[ONE]), scrape)
        task = asyncio.create_task(runner.run_due(NOW))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store.locked
        assert store.runs[NOW.date()]["status"] == "interrupted"
        replacement = DailyRunner(config(), store, AsyncMock(return_value=[]), AsyncMock(return_value=result()))
        assert await replacement.run_due(NOW) == "completed"
    asyncio.run(run())


def test_failed_discovery_still_retries_persisted_backlog():
    async def run():
        store = MemoryStore()
        await store.enqueue([ONE])
        discover = AsyncMock(side_effect=ArticleFetchError("article_timeout", "timeout", 504))
        scrape = AsyncMock(return_value=result())
        runner = DailyRunner(config(), store, discover, scrape)
        assert await runner.run_due(NOW) == "partial"
        assert store.items[ONE]["state"] == "succeeded"
        assert store.runs[NOW.date()]["error_code"] == "article_timeout"
    asyncio.run(run())


def test_disabled_or_early_run_has_no_database_or_network_work():
    async def run():
        store = MemoryStore()
        discover = AsyncMock(return_value=[ONE])
        runner = DailyRunner(DailyConfig(), store, discover, AsyncMock())
        assert await runner.run_due(NOW) == "disabled"
        runner = DailyRunner(config(), store, discover, AsyncMock())
        assert await runner.run_due(NOW.replace(hour=20, day=20)) == "not_due"
        discover.assert_not_awaited()
        assert not store.runs
    asyncio.run(run())


def test_local_timer_is_opt_in_and_disabled_by_default():
    async def run():
        discover = AsyncMock(return_value=[])
        runner = DailyRunner(config(), MemoryStore(), discover)
        runner.start()
        await asyncio.sleep(0)
        assert runner.timer is None and runner.task is None
        discover.assert_not_awaited()
        local = DailyRunner(config(in_process=True), MemoryStore(), discover)
        local.start()
        assert local.timer is not None
        await local.stop()
        disabled = DailyRunner(DailyConfig(in_process=True), MemoryStore(), discover)
        disabled.start()
        assert disabled.timer is None
    asyncio.run(run())


def test_enabled_external_mode_does_not_touch_database_on_startup(monkeypatch):
    monkeypatch.setenv("SCRAPE_ENABLED", "true")
    monkeypatch.setenv("SCRAPE_IN_PROCESS", "false")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-unavailable-test")
    connect = AsyncMock(side_effect=AssertionError("No startup database work"))
    monkeypatch.setattr("app.services.storage.FirestoreStore.initialize", connect)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.daily_runner.timer is None
        assert app.state.daily_runner.task is None
    connect.assert_not_awaited()


def test_timer_environment_flag_is_parsed(monkeypatch):
    monkeypatch.setenv("SCRAPE_IN_PROCESS", "true")
    assert DailyConfig.from_env().in_process is True
