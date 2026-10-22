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
    assert client.get(PREFIX + "/saved").json() == {"items": [], "offset": 0, "next_offset": None}
    first = client.post(PREFIX + "/check")
    assert first.status_code == 200
    data = first.json()
    assert data["status"] == "new" and data["latest"]["expression"] == "Make some room."
    assert data["latest"]["processing_status"] == "partial"
    assert not data["discovery_cached"] and data["listed_count"] == 2
    assert [(item["article_url"], item["issue_date"]) for item in data["listed_days"]] == [(ONE, "2026-10-21"), (TWO, "2026-10-20")]
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


def test_full_daily_lesson_preserves_all_fields_raw_text_and_geometry(site):
    import json
    from app.schemas import OCRText
    from app.services.lesson import parse_lesson
    client, store, source, processor = site
    fixture = json.loads((Path(__file__).parent / "fixtures/lesson_ocr_original_six_sections.json").read_text())
    value = lesson()
    value.ocr = OCRText.model_validate(fixture["ocr"])
    value.lesson = parse_lesson(value.ocr, fixture["image"]["width"], fixture["image"]["height"])
    # URL lesson day differs from its publisher's previous-evening timestamp.
    from test_daily import NOW
    value.article.published_at = NOW - timedelta(days=1)
    store.saved[ONE] = value
    card = client.get(PREFIX + "/saved").json()["items"][0]
    assert card["issue_date"] == "2026-10-21"
    assert card["ocr"] == value.ocr.model_dump(mode="json")
    assert card["lesson"] == value.lesson.model_dump(mode="json")
    assert card["lesson_image"] == value.lesson_image.model_dump(mode="json")
    for name, expected in fixture["expected"]["fields"].items():
        assert card["lesson"]["fields"][name]["text"] == expected["text"]
    source.assert_not_awaited()
    processor.assert_not_awaited()


def test_all_saved_days_are_reachable_beyond_the_old_twenty_record_limit(site):
    client, store, source, processor = site
    for index in range(24):
        url = ONE.replace("/ONE/", f"/DAY{index}/")
        store.saved[url] = lesson(url)
    offset, ids = 0, []
    while offset is not None:
        page = client.get(PREFIX + f"/saved?limit=5&offset={offset}").json()
        assert len(page["items"]) <= 5
        ids.extend(item["id"] for item in page["items"])
        offset = page["next_offset"]
    assert len(ids) == len(set(ids)) == 24
    source.assert_not_awaited()
    processor.assert_not_awaited()


def test_non_series_page_does_not_hide_later_series_days(site):
    client, store, source, processor = site
    unrelated = ONE.replace("/21/ONE/", "/22/UNRELATED/")
    store.saved[unrelated] = result(unrelated)
    store.saved[ONE] = lesson()
    first = client.get(PREFIX + "/saved?limit=1&offset=0").json()
    assert first["items"] == [] and first["next_offset"] == 1
    second = client.get(PREFIX + "/saved?limit=1&offset=1").json()
    assert second["items"][0]["issue_date"] == "2026-10-21"
    assert second["next_offset"] is None


@pytest.mark.parametrize("query", ["limit=0", "limit=11", "offset=-1", "offset=invalid"])
def test_full_lesson_pagination_validation(site, query):
    client, *_ = site
    assert client.get(PREFIX + "/saved?" + query).status_code == 422


def test_load_an_older_day_returns_complete_lesson_and_is_idempotent(site):
    client, store, source, processor = site
    processor.return_value = lesson(TWO)
    loaded = client.post(PREFIX + "/day", json={"article_url": TWO})
    assert loaded.status_code == 200
    data = loaded.json()
    assert data["issue_date"] == "2026-10-20"
    assert data["ocr"]["raw_text"] == "안녕 Hello"
    assert "lesson_image" in data and "fields" in data["lesson"]
    assert client.post(PREFIX + "/day", json={"article_url": TWO}).json() == data
    processor.assert_awaited_once_with(TWO)
    source.assert_not_awaited()  # An explicitly submitted URL uses the normal pipeline.


@pytest.mark.parametrize("url", ["http://localhost/test", "https://127.0.0.1/test", "https://example.test/test"])
def test_day_loading_keeps_public_url_validation(site, url):
    client, store, source, processor = site
    assert client.post(PREFIX + "/day", json={"article_url": url}).status_code == 422
    processor.assert_not_awaited()


def test_old_day_import_does_not_displace_latest_from_first_page(site):
    client, store, *_ = site
    store.saved[ONE] = lesson()
    for day in range(1, 10):
        url = ONE.replace("/21/ONE/", f"/{day:02d}/OLDER/")
        store.saved[url] = lesson(url)
    first = client.get(PREFIX + "/saved?limit=5").json()
    assert first["items"][0]["issue_date"] == "2026-10-21"
    assert first["next_offset"] == 5


def test_dedicated_lesson_page_is_linked_to_home_and_reuses_saved_result(site):
    import json
    import re
    client, store, source, processor = site
    saved = client.post(PREFIX + '/day', json={'article_url': ONE}).json()
    response = client.get('/_dev/test-site/lessons/' + saved['id'] + '?page=143')
    assert response.status_code == 200
    assert '/_dev/test-site/?page=143#archive-heading' in response.text
    assert 'lesson-reader.js' in response.text and 'lesson.js' in response.text
    assert 'script-src \'self\'' in response.headers['content-security-policy']
    assert response.headers['cache-control'] == 'no-store'
    payload = re.search(r'<script type="application/json" id="lesson-data">(.*?)</script>', response.text, re.S).group(1)
    data = json.loads(payload)
    assert data['id'] == saved['id'] and data['ocr'] == saved['ocr']
    assert data['lesson'] == saved['lesson']
    assert client.get('/_dev/test-site/').status_code == 200
    processor.assert_awaited_once()
    source.assert_not_awaited()


def test_lesson_page_escapes_ocr_and_titles_as_inert_json(site):
    import json
    import re
    client, store, source, processor = site
    processor.return_value.article.title = '[스피킹이 강해지는 영어] </title><img src=x onerror=alert(1)>'
    processor.return_value.ocr.raw_text = '</script><script>alert(1)</script> & <hello>'
    saved = client.post(PREFIX + '/day', json={'article_url': ONE}).json()
    response = client.get('/_dev/test-site/lessons/' + saved['id'])
    assert response.status_code == 200
    assert '<img src=x' not in response.text and '<script>alert(1)</script>' not in response.text
    payload = re.search(r'<script type="application/json" id="lesson-data">(.*?)</script>', response.text, re.S).group(1)
    assert json.loads(payload)['ocr']['raw_text'] == processor.return_value.ocr.raw_text


def test_missing_lesson_page_has_clear_error_and_home_link(site):
    client, store, source, processor = site
    response = client.get('/_dev/test-site/lessons/unknown?page=2')
    assert response.status_code == 404 and 'Lesson not found' in response.text
    assert '/_dev/test-site/?page=2#archive-heading' in response.text
    assert client.get('/_dev/test-site/lessons/bad.id').status_code == 422
    assert client.get('/_dev/test-site/lessons/unknown?page=401').status_code == 422
    processor.assert_not_awaited()


def test_lesson_pages_and_reader_assets_disabled_by_default(monkeypatch):
    monkeypatch.setenv('ENABLE_TEST_SITE', 'false')
    with TestClient(create_app()) as client:
        for path in ('/lessons/example', '/assets/lesson-reader.js', '/assets/lesson.js'):
            assert client.get('/_dev/test-site' + path).status_code == 404


def test_home_uses_compact_links_and_separate_reader_asset(site):
    client, *_ = site
    script = client.get('/_dev/test-site/assets/scrape.js').text
    assert 'window.location.assign(lessonPageHref(item.id))' in script
    assert 'Read full lesson →' in script
    assert 'function renderOCRReadingView' not in script
    reader = client.get('/_dev/test-site/assets/lesson-reader.js')
    assert reader.status_code == 200 and 'function renderLessonCards' in reader.text
    assert 'innerHTML' not in reader.text
