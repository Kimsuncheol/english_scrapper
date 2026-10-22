"""Original synthetic publisher pagination fixtures; HTTP and OCR never run."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from app.dependencies import get_store
from app.main import create_app
from app.services import archive
from app.services.errors import ArticleFetchError


def fixture(page):
    return json.loads((Path(__file__).parent / f"fixtures/archive_page_{page}.json").read_text())


class Catalog:
    def __init__(self):
        self.entries = {}
        self.done = set()
        self.busy = False

    @asynccontextmanager
    async def lease(self, name):
        assert name == "series-archive"
        yield None if self.busy else self

    async def save_archive_page(self, page, items, total, lease):
        assert lease is self
        self.entries.update({str(row.article_url): row.model_dump(mode="json") for row in items})
        self.done.add(page)
        self.total = total

    async def archive_entries(self, page, day):
        rows = sorted(self.entries.values(), key=lambda x: x['article_url'], reverse=True)
        if day:
            rows = [row for row in rows if row['issue_date'] == day.isoformat()]
        return {'items': rows[(page-1)*10:page*10], 'total': len(rows), 'page': page,
            'pages': (len(rows)+9)//10, 'publisher_total': self.total,
            'indexed_pages': len(self.done), 'expected_pages': 143}


def test_publisher_offset_pagination_including_page_143(monkeypatch):
    fetch = AsyncMock(return_value=SimpleNamespace(html=json.dumps(fixture(143))))
    monkeypatch.setattr(archive, 'fetch_article', fetch)
    items, total = asyncio.run(archive.fetch_archive_page(143))
    assert total == 1425 and len(items) == 5
    assert items[-1].issue_date == date(2021, 1, 1)
    url = fetch.call_args.args[0]
    query = json.loads(parse_qs(urlsplit(url).query)['query'][0])
    assert query['offset'] == 1420 and query['size'] == 10
    assert query['includeTags'] == 'daily-speaking-english'
    assert fetch.call_args.kwargs == {'listing': True, 'json_response': True}


@pytest.mark.parametrize('page', [0, -1, 401, True])
def test_page_bounds(page):
    with pytest.raises(ValueError):
        asyncio.run(archive.fetch_archive_page(page))


@pytest.mark.parametrize('mutation', ['host', 'title', 'count', 'missing', 'duplicate', 'headlines'])
def test_invalid_feed_never_silently_creates_incomplete_archive(mutation):
    data = fixture(1)
    if mutation == 'host': data['content_elements'][0]['canonical_url'] = 'https://localhost/private'
    if mutation == 'title': data['content_elements'][0]['headlines']['basic'] = 'Unrelated story'
    if mutation == 'count': data['count'] = 50000
    if mutation == 'missing': data['content_elements'].pop()
    if mutation == 'duplicate': data['content_elements'][0] = data['content_elements'][1]
    if mutation == 'headlines': data['content_elements'][0]['headlines'] = None
    with pytest.raises(ArticleFetchError, match='Publisher archive'):
        archive.parse_feed(data, 1)


def test_retry_progress_and_duplicate_prevention():
    async def run():
        store = Catalog()
        fetch = AsyncMock(side_effect=[ArticleFetchError('article_timeout', 'Timed out.', 504), archive.parse_feed(fixture(143), 143)])
        sleep = AsyncMock()
        await archive.sync_archive_page(store, 143, fetch=fetch, sleep=sleep)
        assert len(store.entries) == 5 and store.done == {143}
        sleep.assert_awaited_once_with(1)
        fetch = AsyncMock(return_value=archive.parse_feed(fixture(143), 143))
        await archive.sync_archive_page(store, 143, fetch=fetch)
        assert len(store.entries) == 5
        fetch.side_effect = ArticleFetchError('blocked_page', 'Blocked.', 403)
        with pytest.raises(ArticleFetchError):
            await archive.sync_archive_page(store, 1, fetch=fetch, sleep=sleep)
        assert store.done == {143} and len(store.entries) == 5
        store.busy = True
        with pytest.raises(ArticleFetchError) as error:
            await archive.sync_archive_page(store, 1, fetch=fetch)
        assert error.value.code == 'archive_busy'
    asyncio.run(run())


def test_archive_api_pagination_date_lookup_and_production_disabled(monkeypatch):
    monkeypatch.setenv('ENABLE_TEST_SITE', 'true')
    monkeypatch.setenv('SCRAPE_ENABLED', 'false')
    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    monkeypatch.setattr('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('No external HTTP')))
    monkeypatch.setattr('app.services.ocr.ocr_service.initialize', AsyncMock(side_effect=AssertionError('No OCR')))
    store = Catalog()
    asyncio.run(archive.sync_archive_page(store, 1, fetch=AsyncMock(return_value=archive.parse_feed(fixture(1), 1))))
    asyncio.run(archive.sync_archive_page(store, 143, fetch=AsyncMock(return_value=archive.parse_feed(fixture(143), 143))))
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    path = '/_dev/test-site/series/archive'
    with TestClient(app) as client:
        page = client.get(path)
        assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
        assert page.json()['total'] == 15 and page.json()['publisher_total'] == 1425
        assert len(page.json()['items']) == 10
        assert client.get(path + '?page=2').json()['items'][-1]['issue_date'] == '2021-01-01'
        assert client.get(path + '?day=2021-01-01').json()['total'] == 1
        assert client.get(path + '?day=2021-01-02').json()['total'] == 0
        assert client.get(path + '?page=0').status_code == 422
        assert client.get(path + '?day=invalid').status_code == 422
    monkeypatch.setenv('ENABLE_TEST_SITE', 'false')
    with TestClient(create_app()) as client:
        assert client.get(path).status_code == 404


def test_all_143_pages_are_retained_without_a_recent_record_cutoff():
    async def run():
        store = Catalog()
        for page in range(1, 144):
            data = fixture(143 if page == 143 else 1)
            if page < 143:
                for i, row in enumerate(data['content_elements']):
                    row['canonical_url'] = row['canonical_url'].replace(f'SYNTHETIC1_{i}', f'SYNTHETIC{page}_{i}')
            await archive.sync_archive_page(store, page,
                fetch=AsyncMock(return_value=archive.parse_feed(data, page)))
        last = await archive.archive_page(store, 143)
        assert last.total == 1425 and last.pages == last.indexed_pages == 143
        assert last.items[-1].issue_date == date(2021, 1, 1)
    asyncio.run(run())


def test_historical_series_without_english_path_is_retained():
    data = fixture(143)
    data['content_elements'][0]['canonical_url'] = '/national/nie/2021/01/08/ORIGINAL/'
    items, _ = archive.parse_feed(data, 143)
    assert items[0].issue_date == date(2021, 1, 8)


def test_json_feed_uses_existing_network_guards(monkeypatch):
    import httpx
    from app.services import article
    monkeypatch.setattr(article, 'resolve_public_ip', AsyncMock(return_value='93.184.216.34'))
    def handler(request):
        assert request.headers['host'] == 'www.chosun.com'
        assert request.extensions['sni_hostname'] == 'www.chosun.com'
        assert request.headers['accept'] == 'application/json'
        return httpx.Response(200, headers={'content-type':'application/json'}, stream=httpx.ByteStream(b'{"count":0,"content_elements":[]}'))
    response = asyncio.run(article.fetch_article(archive.FEED, transport=httpx.MockTransport(handler), listing=True, json_response=True))
    assert json.loads(response.html)['count'] == 0
    def redirect(request):
        return httpx.Response(302, headers={'location':'http://127.0.0.1/private'})
    with pytest.raises(ArticleFetchError) as error:
        asyncio.run(article.fetch_article(archive.FEED, transport=httpx.MockTransport(redirect), listing=True, json_response=True))
    assert error.value.code == 'unsafe_url'


def test_snapshot_revision_only_rebuilds_changed_catalog_and_retains_retry_state():
    from app.services.storage import FirestoreStore
    async def run():
        store = FirestoreStore('demo-test-not-initialized')
        store.publish_archive = AsyncMock(return_value=11)
        assert not await store.ensure_archive_published({})
        assert not await store.ensure_archive_published({'catalog_revision': 1, 'published_revision': 1})
        store.publish_archive.assert_not_awaited()
        assert await store.ensure_archive_published({'catalog_revision': 2, 'published_revision': 1})
        for code in ('archive_busy', 'archive_incomplete'):
            store.publish_archive.side_effect = ArticleFetchError(code, 'Retry later.', 503)
            assert not await store.ensure_archive_published({'catalog_revision': 2, 'published_revision': 1})
        store.publish_archive.side_effect = ArticleFetchError('storage_unavailable', 'Unavailable.', 503)
        with pytest.raises(ArticleFetchError):
            await store.ensure_archive_published({'catalog_revision': 2, 'published_revision': 1})
    asyncio.run(run())
