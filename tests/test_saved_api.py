"""API contracts, durable-result semantics, and safe failure responses."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import psycopg
import pytest

from app.main import app
from app.services import scrape
from app.services.errors import ArticleFetchError
from app.services.saved import process_and_store
from test_daily import ONE, TWO, NOW, DailyRunner, config, result


def test_manual_duplicates_and_canonical_alias_return_first_result(monkeypatch, persisted_store):
    processor = AsyncMock(side_effect=[result(), result().model_copy(update={"ocr": result().ocr.model_copy(update={"raw_text": "different OCR"})})])
    monkeypatch.setattr(scrape, "scrape_article", processor)
    client = TestClient(app)
    first = client.post("/api/v1/scrape", json={"article_url": ONE + "?utm=x"})
    assert first.status_code == 200
    duplicate = client.post("/api/v1/scrape", json={"article_url": ONE.replace("www.chosun.com", "chosun.com")})
    assert duplicate.json() == first.json()
    assert duplicate.headers["location"] == first.headers["location"]
    assert processor.await_count == 1
    # An unseen alias needs processing to learn the canonical URL, but cannot
    # overwrite its first saved result or create a second article record.
    alias = client.post("/api/v1/scrape", json={"article_url": TWO})
    assert alias.json() == first.json()
    assert processor.await_count == 2
    repeated_alias = client.post("/api/v1/scrape", json={"article_url": TWO})
    assert repeated_alias.json() == first.json()
    assert processor.await_count == 2
    listing = client.get("/api/v1/articles").json()
    assert listing["total"] == 1
    article_id = listing["items"][0]["id"]
    saved = client.get(first.headers["location"])
    assert saved.status_code == 200
    assert saved.json()["id"] == article_id
    assert saved.json()["payload"] == first.json()
    assert "ocr" in saved.json()["payload"] and "lesson" in saved.json()["payload"]


def test_article_pagination_and_not_found(monkeypatch, persisted_store):
    monkeypatch.setattr(scrape, "scrape_article", AsyncMock(side_effect=[result(), result(TWO)]))
    client = TestClient(app)
    for url in [ONE, TWO]:
        assert client.post("/api/v1/scrape", json={"article_url": url}).status_code == 200
    pages = [client.get(f"/api/v1/articles?limit=1&offset={offset}").json() for offset in (0, 1, 2)]
    assert [len(page["items"]) for page in pages] == [1, 1, 0]
    assert all(page["total"] == 2 and page["limit"] == 1 for page in pages)
    assert pages[0]["items"][0]["id"] != pages[1]["items"][0]["id"]
    for query in ["limit=0", "limit=101", "offset=-1", "limit=bad"]:
        response = client.get("/api/v1/articles?" + query)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
    missing = client.get("/api/v1/articles/unknown")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "article_not_found"


def test_manual_failure_is_retryable_and_not_cached(monkeypatch, persisted_store):
    processor = AsyncMock(side_effect=[ArticleFetchError("article_timeout", "Timed out fetching the article.", 504), result()])
    monkeypatch.setattr(scrape, "scrape_article", processor)
    client = TestClient(app)
    first = client.post("/api/v1/scrape", json={"article_url": ONE})
    assert first.status_code == 504
    assert persisted_store.items[ONE]["state"] == "failed"
    assert client.get("/api/v1/articles").json()["total"] == 0
    assert client.post("/api/v1/scrape", json={"article_url": ONE}).status_code == 200
    assert persisted_store.items[ONE]["attempts"] == 2


def test_storage_and_unexpected_errors_hide_details(monkeypatch, persisted_store, caplog):
    secret = "PRIVATE_DATABASE_PASSWORD_AND_IMAGE_BYTES"
    monkeypatch.setattr(scrape, "scrape_article", AsyncMock(side_effect=RuntimeError(secret)))
    client = TestClient(app)
    response = client.post("/api/v1/scrape", json={"article_url": ONE})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "processing_failed"
    assert secret not in response.text and secret not in caplog.text
    assert persisted_store.items[ONE]["error_code"] == "processing_failed"

    @asynccontextmanager
    async def unavailable(url):
        raise psycopg.OperationalError(secret)
        yield
    persisted_store.article_session = unavailable
    response = client.post("/api/v1/scrape", json={"article_url": ONE})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert secret not in response.text and secret not in caplog.text


def test_disabled_scheduler_and_missing_database(monkeypatch):
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        status = client.get("/api/v1/jobs/status")
        assert status.status_code == 200
        assert status.json()["enabled"] is False
        assert status.json()["last_run"] is None
        assert status.json()["latest_run_result"] is None
        assert status.json()["next_scheduled_at"] is None
        assert "database_url" not in status.json() and "trigger_token" not in status.json()
        for method, path, kwargs in [
            ("post", "/api/v1/scrape", {"json": {"article_url": ONE}}),
            ("get", "/api/v1/articles", {}), ("get", "/api/v1/articles/unknown", {}),
        ]:
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "storage_unavailable"
        assert client.get("/health").status_code == 200
        assert client.post("/api/v1/scrape", json={"article_url": "http://localhost/"}).status_code == 422


def test_read_endpoints_sanitize_database_failures(monkeypatch, persisted_store):
    secret = "postgresql://private-user:private-password@private-host/db"
    persisted_store.articles = AsyncMock(side_effect=psycopg.OperationalError(secret))
    persisted_store.lesson = AsyncMock(side_effect=psycopg.OperationalError(secret))
    persisted_store.status = AsyncMock(side_effect=psycopg.OperationalError(secret))
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app) as client:
        app.state.daily_runner = DailyRunner(config(), persisted_store)
        for path in ["/api/v1/articles", "/api/v1/articles/unknown", "/api/v1/jobs/status"]:
            response = client.get(path)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "storage_unavailable"
            assert secret not in response.text


def test_manual_result_is_skipped_by_scheduled_pipeline(monkeypatch, persisted_store):
    async def run():
        processor = AsyncMock(return_value=result())
        saved, created = await process_and_store(persisted_store, TWO, processor)
        assert created
        # Discovery may later list the canonical URL rather than the alias.
        daily = DailyRunner(config(), persisted_store, AsyncMock(return_value=[ONE]), processor)
        assert await daily.run_due(NOW) == "completed"
        assert persisted_store.runs[NOW.date()]["stats"]["skipped"] == 1
        assert persisted_store.items[ONE]["state"] == "succeeded"
        processor.assert_awaited_once()
        repeated, created = await process_and_store(persisted_store, saved.canonical_url, processor)
        assert not created and repeated == saved
    asyncio.run(run())


def test_new_routes_have_explicit_openapi_models():
    schema = TestClient(app).get("/openapi.json").json()
    for path, model in [("/api/v1/jobs/status", "DailyStatusResponse"),
        ("/api/v1/articles", "ArticlePage"), ("/api/v1/articles/{article_id}", "SavedLessonResponse")]:
        responses = schema["paths"][path]["get"]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]["$ref"].endswith(model)
        assert responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorResponse")
