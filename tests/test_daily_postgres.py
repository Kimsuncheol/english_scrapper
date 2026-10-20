"""Real lock/persistence tests. Use an isolated TEST_DATABASE_URL, never prod."""

import asyncio
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
import httpx
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import app
from app.daily_config import DailyConfig
from app.services.daily import DailyRunner
from app.services.storage import PostgresStore
from app.services import scrape as scrape_service
from test_daily import ONE, NOW, result

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Set an isolated TEST_DATABASE_URL for PostgreSQL integration tests")


async def make_store():
    url = os.environ["TEST_DATABASE_URL"]
    schema = "test_daily_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    # Each test creates a fresh schema; no existing data is dropped or truncated.
    return PostgresStore(url, schema)


def test_real_postgres_lock_release_and_calendar_uniqueness():
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


def test_real_postgres_persistence_duplicate_urls_and_failed_backlog():
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
        replacement = PostgresStore(store.url, store.schema)
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
        replacement = PostgresStore(store.url, store.schema)
        async with replacement.run_session() as session:
            assert await session.start(NOW.date()) == first_id
            assert (await session.pending(1))[0]["url"] == ONE
            await session.finish(first_id, "completed", {})
        assert (await replacement.status())["last_run"]["attempts"] == 2
    asyncio.run(run())


def test_job_status_and_saved_lesson_routes_use_persistent_results(monkeypatch):
    async def seed():
        store = await make_store()
        cfg = DailyConfig(enabled=True, database_url=store.url, trigger_token="test-trigger-token", request_gap_seconds=0)
        runner = DailyRunner(cfg, store, AsyncMock(return_value=[ONE]), AsyncMock(return_value=result()))
        assert await runner.run_due(NOW) == "completed"
        return runner
    runner = asyncio.run(seed())
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        runner = DailyRunner(DailyConfig(database_url=store.url), store)
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
        runner.store = PostgresStore(store.url, store.schema)
        saved = client.get(first.headers["location"])
        assert saved.json()["payload"] == first.json()
        assert saved.json()["created_at"]
        assert client.get("/api/v1/articles/unknown").status_code == 404

        scheduled = DailyRunner(DailyConfig(enabled=True, database_url=store.url),
            runner.store, AsyncMock(return_value=[ONE]), processor)
        assert asyncio.run(scheduled.run_due(NOW)) == "completed"
        processor.assert_awaited_once()
        app.state.daily_runner = scheduled
        status = client.get("/api/v1/jobs/status").json()
        assert status["enabled"] and status["next_scheduled_at"]
        assert status["last_run"]["status"] == "completed"
        assert status["latest_run_result"]["stats"]["skipped"] == 1
        assert status["latest_run_result"]["finished_at"]


def test_concurrent_manual_and_scheduled_article_work_uses_postgres_lock(monkeypatch):
    async def run():
        store = await make_store()
        entered, release = asyncio.Event(), asyncio.Event()
        async def process(url):
            entered.set()
            await release.wait()
            return result()
        processor = AsyncMock(side_effect=process)
        monkeypatch.setattr(scrape_service, "scrape_article", processor)
        runner = DailyRunner(DailyConfig(database_url=store.url), store)
        monkeypatch.setattr(app.state, "daily_runner", runner, raising=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/v1/scrape", json={"article_url": ONE}))
            await asyncio.wait_for(entered.wait(), 3)
            try:
                busy = await client.post("/api/v1/scrape", json={"article_url": ONE + "?utm=x"})
                assert busy.status_code == 503
                assert busy.json()["error"]["code"] == "scrape_busy"
                scheduled = DailyRunner(DailyConfig(enabled=True, database_url=store.url,
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
