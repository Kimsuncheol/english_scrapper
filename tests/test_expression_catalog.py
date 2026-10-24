import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.daily_api import router
from app.dependencies import get_store
from app.services.storage import FirestoreStore, StorageUnavailable


def test_public_catalog_does_not_require_dev_site_or_run_ocr():
    store = SimpleNamespace(expression_catalog=AsyncMock(return_value={
        'items': [{'expression': 'catalog title without OCR'}], 'total': 1}))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_store] = lambda: store
    response = TestClient(app).get('/api/v1/expression-catalog')
    assert response.status_code == 200
    assert response.json()['total'] == 1
    store.expression_catalog.assert_awaited_once()


@pytest.mark.parametrize('incomplete,changed', [(False, False), (True, False), (False, True)])
def test_snapshot_reads_all_published_pages_and_rejects_inconsistent_content(incomplete, changed):
    state = {'published_total': 11, 'published_revision': 4}
    ref = SimpleNamespace(get=AsyncMock(side_effect=[
        SimpleNamespace(to_dict=lambda: state),
        SimpleNamespace(to_dict=lambda: {**state, 'published_revision': 5 if changed else 4})]))
    async def stream(**kwargs):
        for number in [2, 1, 3]:  # Firestore document order is not numeric page order.
            items = [number] if number == 2 else [number] * 10
            if incomplete and number == 2:
                items = []
            yield SimpleNamespace(id=str(number), to_dict=lambda items=items: {'items': items})
    store = FirestoreStore('test')
    store.initialize = AsyncMock()
    store.ensure_archive_published = AsyncMock(return_value=False)
    store.collection = lambda name: (
        SimpleNamespace(document=lambda _: ref) if name == 'archive_state'
        else SimpleNamespace(stream=stream))
    if incomplete or changed:
        with pytest.raises(StorageUnavailable):
            asyncio.run(store.expression_catalog())
    else:
        result = asyncio.run(store.expression_catalog())
        assert result == {'items': [1] * 10 + [2], 'total': 11}
