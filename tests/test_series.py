"""Deterministic series discovery and development UI API; no network or real OCR."""

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from app.dependencies import get_store
from app.main import create_app
from app.schemas import LessonField
from app.services import discovery, scrape, series
from app.services.errors import ArticleFetchError
from test_daily import MemoryStore, ONE, TWO, result

PREFIX = "/_dev/test-site/series"


def lesson(url=ONE, expression="Make some room."):
    value = result(url)
    value.article.title = "[스피킹이 강해지는 영어] " + expression
    value.lesson.fields.expression = LessonField(text=expression, region_indices=[], confidence=0.99,
        bounding_box={"x": 10, "y": 10, "width": 100, "height": 20})
    return value


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("ENABLE_TEST_SITE", "true")
    monkeypatch.setenv("SCRAPE_ENABLED", "false")
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.setattr("httpx.AsyncClient.send", AsyncMock(side_effect=AssertionError("No real HTTP")))
    monkeypatch.setattr("app.services.ocr.ocr_service.initialize", AsyncMock(side_effect=AssertionError("No real OCR")))
    source = AsyncMock(return_value=[ONE, TWO])
    processor = AsyncMock(return_value=lesson())
    monkeypatch.setattr(series, "discover_articles", source)
    monkeypatch.setattr(scrape, "scrape_article", processor)
    store = MemoryStore()
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as client:
        yield client, store, source, processor


def test_real_tag_shape_discovery_keeps_publisher_order_and_filters():
    html = (Path(__file__).parent / "fixtures/series_tag.html").read_text()
    assert discovery.SERIES_PAGE == "https://www.chosun.com/tag/daily-speaking-english/"
    assert discovery.discover_from_html(html) == [ONE, TWO]


def test_new_then_duplicate_check_uses_shared_pipeline_and_discovery_cache(site):
    client, store, source, processor = site
    assert client.get(PREFIX + "/saved").json() == {"items": []}
    first = client.post(PREFIX + "/check")
    assert first.status_code == 200
    data = first.json()
    assert data["status"] == "new" and data["latest"]["expression"] == "Make some room."
    assert data["latest"]["processing_status"] == "partial"
    assert not data["discovery_cached"] and data["listed_count"] == 2
    duplicate = client.post(PREFIX + "/check").json()
    assert duplicate["status"] == "up_to_date" and duplicate["discovery_cached"]
    source.assert_awaited_once()
    processor.assert_awaited_once_with(ONE)  # Older listing is not batch-imported.
    saved = client.get(PREFIX + "/saved")
    assert saved.headers["cache-control"] == "no-store"
    assert saved.json()["items"][0]["expression"] == "Make some room."
    assert store.items[ONE]["state"] == "succeeded"
    # Existing manual submission is idempotent with the series check.
    assert client.post("/api/v1/scrape", json={"article_url": ONE}).status_code == 200
    processor.assert_awaited_once()


def test_discovery_cache_expiry_detects_newer_post(site):
    client, store, source, processor = site
    assert client.post(PREFIX + "/check").json()["status"] == "new"
    newer = ONE.replace("/ONE/", "/NEW/")
    client.app.state.series_checker.expires_at = 0
    source.return_value = [newer, ONE]
    processor.return_value = lesson(newer, "Try a new path.")
    checked = client.post(PREFIX + "/check").json()
    assert checked["status"] == "new"
    assert checked["latest"]["expression"] == "Try a new path."
    assert source.await_count == processor.await_count == 2


def test_empty_and_blocked_discovery_do_not_run_ocr_or_claim_up_to_date(site):
    client, store, source, processor = site
    source.return_value = []
    data = client.post(PREFIX + "/check").json()
    assert data["status"] == "empty" and data["latest"] is None
    processor.assert_not_awaited()
    client.app.state.series_checker.expires_at = 0
    source.side_effect = ArticleFetchError("blocked_page", "Publisher denied access.", 403)
    blocked = client.post(PREFIX + "/check")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "blocked_page"
    assert not store.saved


def test_failed_new_scrape_can_retry_and_missing_expression_stays_empty(site):
    client, store, source, processor = site
    processor.side_effect = ArticleFetchError("article_timeout", "Timed out fetching the article.", 504)
    assert client.post(PREFIX + "/check").status_code == 504
    assert store.items[ONE]["state"] == "failed"
    value = lesson()
    value.lesson.fields.expression = None
    processor.side_effect = None
    processor.return_value = value
    data = client.post(PREFIX + "/check").json()
    assert data["status"] == "new" and data["latest"]["expression"] is None
    assert source.await_count == 1 and processor.await_count == 2


def test_saved_panel_is_read_only_and_sorts_by_publication(site):
    client, store, source, processor = site
    older = lesson(TWO)
    newer = lesson()
    from test_daily import NOW
    older.article.published_at = NOW - timedelta(days=1)
    newer.article.published_at = NOW
    store.saved[TWO] = older
    store.saved[ONE] = newer
    store.saved[ONE.replace("/ONE/", "/UNRELATED/")] = result()
    items = client.get(PREFIX + "/saved").json()["items"]
    assert [item["article"]["url"] for item in items] == [ONE, TWO]
    source.assert_not_awaited()
    processor.assert_not_awaited()


def test_concurrent_checks_return_clear_busy_error(monkeypatch):
    async def run():
        checker = series.SeriesChecker()
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover():
            entered.set()
            await release.wait()
            return []
        monkeypatch.setattr(series, "discover_articles", discover)
        first = asyncio.create_task(checker.check(MemoryStore()))
        await entered.wait()
        try:
            with pytest.raises(ArticleFetchError) as exc:
                await checker.check(MemoryStore())
            assert exc.value.code == "series_check_busy"
        finally:
            release.set()
            assert (await first).status == "empty"
    asyncio.run(run())
