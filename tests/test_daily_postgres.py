"""Real lock/persistence tests. Use an isolated TEST_DATABASE_URL, never prod."""

import asyncio
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import app
from app.daily_config import DailyConfig
from app.services.daily import DailyRunner
from app.services.storage import PostgresStore
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
