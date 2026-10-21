"""Real SDK transactions on a loopback emulator with a demo project, never prod."""

import asyncio
import os
from uuid import uuid4

import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import app
from app.daily_config import DailyConfig
from app.services.daily import DailyRunner
from app.services.storage import FirestoreStore
from app.services import scrape as scrape_service
from test_daily import ONE, NOW, result

pytestmark = pytest.mark.skipif(not os.getenv("FIRESTORE_EMULATOR_HOST"), reason="Set FIRESTORE_EMULATOR_HOST for emulator-only integration tests")


async def make_store():
    # Emulator only, never use ADC or a production project. No data is deleted.
    host = os.environ["FIRESTORE_EMULATOR_HOST"]
    if not host.startswith(("127.0.0.1:", "localhost:")):
        raise ValueError("Integration tests require a loopback emulator")
    return FirestoreStore("demo-english-scrapper", prefix="test_" + uuid4().hex)


def test_real_firestore_lock_release_and_calendar_uniqueness():
    async def run():
        store = await make_store()
        await store.initialize()
        async with store.run_session() as first:
            assert first is not None
            run_id = await first.start(NOW.date())
            async with store.run_session() as second:
                assert second is None
            await first.finish(run_id, "completed", {"saved": 0})
        async with store.run_session() as next_session:
            assert next_session is not None
            assert await next_session.start(NOW.date()) is None
        status = await store.status()
        assert status["last_run"]["status"] == "completed"
        assert status["last_run"]["finished_at"] is not None
    asyncio.run(run())


def test_real_firestore_persistence_duplicate_urls_and_failed_backlog():
    async def run():
        store = await make_store()
        async with store.run_session() as session:
            run_id = await session.start(NOW.date())
            await session.enqueue([ONE, ONE + "?utm=1"])
            pending = await session.pending(5)
            assert pending == [{"url": ONE}]
            await session.attempt(ONE)
            await session.failed(ONE, "article_timeout")
            await session.finish(run_id, "partial", {"failed": 1})
        replacement = FirestoreStore(store.project, prefix=store.prefix)
        async with replacement.run_session() as session:
            assert (await session.pending(5))[0]["url"] == ONE
            await session.attempt(ONE)
            assert await session.save(ONE, result())
            assert not await session.save(ONE, result())
            assert not await session.pending(5)
        summaries = await replacement.list_lessons()
        assert len(summaries) == 1
        saved = await replacement.lesson(summaries[0]["id"])
        assert saved["payload"] == result().model_dump(mode="json")
        assert saved["created_at"] is not None
        assert (await replacement.status())["failures"] == []
    asyncio.run(run())


def test_interrupted_run_can_resume_after_lock_connection_closes():
    async def run():
        store = await make_store()
        async with store.run_session() as session:
            first_id = await session.start(NOW.date())
            await session.enqueue([ONE])
            await session.attempt(ONE)
            # Simulate process exit: leave running history, close lock session.
        replacement = FirestoreStore(store.project, prefix=store.prefix)
        async with replacement.run_session() as session:
            assert await session.start(NOW.date()) == first_id
            assert (await session.pending(1))[0]["url"] == ONE
            await session.finish(first_id, "completed", {})
        assert (await replacement.status())["last_run"]["attempts"] == 2
    asyncio.run(run())


def test_latest_discovered_article_precedes_older_persistent_backlog():
    async def run():
        store = await make_store()
        older = ONE.replace("/ONE/", "/OLDER/")
        async with store.run_session() as session:
            await session.enqueue([older])
            await session.enqueue([ONE])
            assert await session.pending(1, preferred_urls=[ONE, older]) == [{"url": ONE}]
    asyncio.run(run())


def test_job_status_and_saved_lesson_routes_use_persistent_results(monkeypatch):
    async def seed():
        store = await make_store()
        cfg = DailyConfig(enabled=True, firebase_project_id=store.project, trigger_token="test-trigger-token", request_gap_seconds=0)
        runner = DailyRunner(cfg, store, AsyncMock(return_value=[ONE]), AsyncMock(return_value=result()))
        assert await runner.run_due(NOW) == "completed"
        await store.close()
        return runner
    runner = asyncio.run(seed())
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    with TestClient(app) as client:
        app.state.daily_runner = runner
        response = client.get("/api/v1/jobs/daily")
        assert response.status_code == 200
        assert response.json()["last_run"]["status"] == "completed"
        assert response.json()["last_run"]["stats"]["saved"] == 1
        assert response.json()["last_run"]["started_at"]
        listing = client.get("/api/v1/lessons")
        assert listing.status_code == 200
        lesson_id = listing.json()[0]["id"]
        saved = client.get("/api/v1/lessons/" + lesson_id)
        assert saved.status_code == 200
        assert saved.json()["payload"] == result().model_dump(mode="json")
        assert client.get("/api/v1/lessons/unknown").status_code == 404
        assert client.get("/api/v1/lessons?limit=101").status_code == 422
        assert client.get("/health").status_code == 200


def test_manual_scrape_round_trip_idempotency_and_scheduled_reuse(monkeypatch):
    store = asyncio.run(make_store())
    processor = AsyncMock(return_value=result())
    monkeypatch.setattr(scrape_service, "scrape_article", processor)
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    with TestClient(app) as client:
        runner = DailyRunner(DailyConfig(firebase_project_id=store.project), store)
        app.state.daily_runner = runner
        first = client.post("/api/v1/scrape", json={"article_url": ONE + "?tracking=x"})
        duplicate = client.post("/api/v1/scrape", json={"article_url": ONE.replace("www.chosun.com", "chosun.com")})
        assert first.status_code == duplicate.status_code == 200
        assert first.json() == duplicate.json()
        processor.assert_awaited_once()
        article_id = first.headers["location"].rsplit("/", 1)[-1]
        listing = client.get("/api/v1/articles?limit=1&offset=0")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["id"] == article_id
        assert client.get("/api/v1/articles?limit=1&offset=1").json()["items"] == []
        # A new repository instance reads the same persisted result.
        runner.store = FirestoreStore(store.project, prefix=store.prefix)
        saved = client.get(first.headers["location"])
        assert saved.json()["payload"] == first.json()
        assert saved.json()["created_at"]
        assert client.get("/api/v1/articles/unknown").status_code == 404

        scheduled = DailyRunner(DailyConfig(enabled=True, firebase_project_id=store.project),
            runner.store, AsyncMock(return_value=[ONE]), processor)
        assert client.portal.call(scheduled.run_due, NOW) == "completed"
        processor.assert_awaited_once()
        app.state.daily_runner = scheduled
        status = client.get("/api/v1/jobs/status").json()
        assert status["enabled"] and status["next_scheduled_at"]
        assert status["last_run"]["status"] == "completed"
        assert status["latest_run_result"]["stats"]["skipped"] == 1
        assert status["latest_run_result"]["finished_at"]


def test_concurrent_manual_and_scheduled_article_work_uses_firestore_lock(monkeypatch):
    async def run():
        store = await make_store()
        entered, release = asyncio.Event(), asyncio.Event()
        async def process(url):
            entered.set()
            await release.wait()
            return result()
        processor = AsyncMock(side_effect=process)
        monkeypatch.setattr(scrape_service, "scrape_article", processor)
        runner = DailyRunner(DailyConfig(firebase_project_id=store.project), store)
        monkeypatch.setattr(app.state, "daily_runner", runner, raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/v1/scrape", json={"article_url": ONE}))
            await asyncio.wait_for(entered.wait(), 3)
            try:
                busy = await client.post("/api/v1/scrape", json={"article_url": ONE + "?utm=x"})
                assert busy.status_code == 503
                assert busy.json()["error"]["code"] == "scrape_busy"
                scheduled = DailyRunner(DailyConfig(enabled=True, firebase_project_id=store.project,
                    retry_attempts=1, request_gap_seconds=0), store,
                    AsyncMock(return_value=[ONE]), processor)
                assert await scheduled.run_due(NOW) == "partial"
                assert (await store.status())["last_run"]["stats"]["failed"] == 1
            finally:
                release.set()
            first = await first
            assert first.status_code == 200
            repeated = await client.post("/api/v1/scrape", json={"article_url": ONE})
            assert repeated.json() == first.json()
            processor.assert_awaited_once()
            assert (await store.articles())["total"] == 1
    asyncio.run(run())


def test_expired_lease_takeover_fences_old_owner_and_preserves_successor():
    from datetime import datetime, timedelta, timezone
    from app.services.errors import ArticleFetchError

    async def run():
        store = await make_store()
        original_context = store.run_session()
        original = await original_context.__aenter__()
        closed = False
        try:
            await original.enqueue([ONE])
            lock = store.collection("locks").document("daily")
            await lock.update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
            async with store.run_session() as successor:
                assert successor is not None
                with pytest.raises(ArticleFetchError, match="lease expired"):
                    await original.failed(ONE, "stale_owner")
                await original_context.__aexit__(None, None, None)
                closed = True
                # Stale cleanup actually ran while the successor was active.
                assert (await lock.get()).to_dict()["owner"] == successor.owner
        finally:
            if not closed:
                await original_context.__aexit__(None, None, None)
        await store.close()
    asyncio.run(run())


def test_concurrent_aliases_commit_only_one_canonical_result():
    from test_daily import TWO
    async def run():
        store = await make_store()
        calls = 0
        ready = asyncio.Event()
        async def scrape(url):
            nonlocal calls
            calls += 1
            if calls == 2:
                ready.set()
            await ready.wait()
            return result()
        from app.services.saved import process_and_store
        values = await asyncio.gather(
            process_and_store(store, ONE, scrape), process_and_store(store, TWO, scrape))
        assert sum(created for _, created in values) == 1
        assert values[0][0] == values[1][0]
        assert (await store.articles())["total"] == 1
        await store.close()
    asyncio.run(run())


def test_oversized_result_records_failure_without_partial_save():
    from app.services.saved import process_and_store
    from app.services.errors import ArticleFetchError
    async def run():
        store = await make_store()
        large = result()
        large.ocr.raw_text = "가" * 300_000
        with pytest.raises(ArticleFetchError) as error:
            await process_and_store(store, ONE, AsyncMock(return_value=large))
        assert error.value.code == "result_too_large"
        assert error.value.status_code == 413
        assert (await store.articles())["total"] == 0
        assert (await store.status())["failures"][0]["error_code"] == "result_too_large"
        await store.close()
    asyncio.run(run())


def test_series_pagination_orders_issue_dates_before_late_import_time():
    from test_daily import TWO
    async def run():
        store = await make_store()
        async with store.run_session() as session:
            for url in [ONE, TWO]:
                value = result(url)
                value.article.title = "[스피킹이 강해지는 영어] Synthetic lesson"
                await session.enqueue([url])
                await session.save(url, value)
        first = await store.series_articles(limit=1, offset=0)
        second = await store.series_articles(limit=1, offset=1)
        assert first["total"] == second["total"] == 2
        assert first["items"][0]["canonical_url"] == ONE
        assert second["items"][0]["canonical_url"] == TWO
        from app.services.series import saved_expressions
        full = await saved_expressions(store, limit=1)
        assert str(full.items[0].issue_date) == "2026-10-21"
        assert full.items[0].ocr.raw_text == "안녕 Hello"
        assert full.next_offset == 1
        await store.close()
    asyncio.run(run())


def test_archive_page_replay_durable_pagination_and_first_day_lookup():
    from datetime import date
    from app.services.archive import sync_archive_page, archive_page, parse_feed
    from test_archive import fixture
    async def run():
        store = await make_store()
        first = parse_feed(fixture(1), 1)
        last = parse_feed(fixture(143), 143)
        await sync_archive_page(store, 1, fetch=AsyncMock(return_value=first))
        await sync_archive_page(store, 143, fetch=AsyncMock(return_value=last))
        await sync_archive_page(store, 143, fetch=AsyncMock(return_value=last))
        # A synthetic partial index cannot be published as a complete archive.
        from app.services.errors import ArticleFetchError
        with pytest.raises(ArticleFetchError) as error:
            await store.publish_archive()
        assert error.value.code == "archive_incomplete"
        await store.collection("archive_state").document("series").update({"publisher_total": 15})
        assert await store.publish_archive() == 15
        await store.close()
        replacement = FirestoreStore(store.project, prefix=store.prefix)
        page = await archive_page(replacement, 2)
        assert page.total == 15 and page.indexed_pages == 2
        assert page.expected_pages == 143 and page.publisher_total == 15
        assert page.items[-1].issue_date == date(2021, 1, 1)
        day = await archive_page(replacement, 1, date(2021, 1, 1))
        assert day.total == 1 and day.items[0].expression == 'Original example 143-4'
        assert (await archive_page(replacement, 1, date(2021, 1, 2))).total == 0
        await replacement.close()
    asyncio.run(run())
