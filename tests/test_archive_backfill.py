import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.errors import ArticleFetchError
from app.services.lesson_readiness import CORE_FIELDS, missing_sections
from app.services.storage import identifier
from scripts import backfill_archive as worker


def record(url, *, missing=()):
    return {'id': identifier(url), 'canonical_url': url,
            'payload': {'ocr': {'raw_text': 'original saved lesson'},
                        'lesson': {'fields': {name: None if name in missing else {'text': name}
                                              for name in CORE_FIELDS}}}}


def catalog():
    return {'total': 2, 'items': [
        {'article_url': f'https://www.chosun.com/national/nie/english/2026/10/{day}/ISSUE/',
         'issue_date': f'2026-10-{day}', 'expression': name}
        for day, name in [('23', 'new'), ('22', 'old')]]}


class Store:
    def __init__(self, records=()):
        self.records = {row['id']: row for row in records}
        self.writes = []
    async def expression_catalog(self):
        return catalog()
    async def articles(self, limit, offset):
        return {'items': list(self.records.values())[offset:offset+limit], 'total': len(self.records)}
    async def lesson(self, key):
        return self.records.get(key)
    def collection(self, name):
        assert name == 'archive_backfill'
        return self
    def document(self, key):
        assert key == 'state'
        return self
    async def set(self, data):
        self.writes.append(data)


def test_title_catalog_is_not_equivalent_to_ready_saved_content():
    old = catalog()['items'][1]
    jobs = worker.catalog_jobs(catalog(), [record(old['article_url'])])
    assert len(jobs) == 2
    assert sum(not job['missing'] for job in jobs) == 1
    assert worker.catalog_jobs(catalog(), [], priority=['old'])[0]['expression'] == 'old'


def test_readiness_requires_all_study_sections_and_preserves_optional_absence():
    saved = record(catalog()['items'][0]['article_url'])
    assert missing_sections(saved) == []  # Vocabulary/source credit may be absent in print.
    saved['payload']['lesson']['fields']['korean_meaning'] = {'text': '[unreadable]'}
    assert missing_sections(saved) == ['korean_meaning']
    assert missing_sections(None)


def test_audit_never_transcribes_or_writes_firestore(tmp_path):
    store = Store()
    processor = AsyncMock()
    state = asyncio.run(worker.backfill(store, report=tmp_path/'report.json', processor=processor))
    assert state['ready'] == 0 and state['remaining'] == 2
    assert state['status'] == 'audit'
    processor.assert_not_called()
    assert store.writes == []


def test_backfill_resumes_skips_ready_and_finishes_only_after_saved_content_exists(tmp_path):
    first, second = catalog()['items']
    store = Store([record(first['article_url'])])
    async def process(store, url, *, scrape, reprocess):
        assert not reprocess
        ready = record(url)
        store.records[ready['id']] = ready
        return SimpleNamespace(model_dump=lambda **_: ready), True
    process = AsyncMock(side_effect=process)
    state = asyncio.run(worker.backfill(store, apply=True, delay=0,
                                      report=tmp_path/'report.json', processor=process))
    assert state['status'] == 'complete' and state['ready'] == 2
    process.assert_awaited_once()
    state = asyncio.run(worker.backfill(store, apply=True, delay=0, processor=process))
    assert state['ready'] == 2
    process.assert_awaited_once()


def test_failed_transcription_is_bounded_and_never_reported_as_ready():
    store = Store()
    process = AsyncMock(side_effect=ArticleFetchError('ollama_invalid_response', 'invalid', 502))
    state = asyncio.run(worker.backfill(store, apply=True, delay=0, passes=2, processor=process, sleep=AsyncMock()))
    assert state['status'] == 'incomplete' and state['remaining'] == 2
    assert state['failed'] == 2
    assert process.await_count == 4


def test_provider_quota_stops_processing_and_persists_blocked_status():
    store = Store()
    process = AsyncMock(side_effect=ArticleFetchError('ollama_rate_limited', 'quota', 503))
    with pytest.raises(ArticleFetchError):
        asyncio.run(worker.backfill(store, apply=True, delay=0, processor=process))
    process.assert_awaited_once()
    assert store.writes[-1]['status'] == 'blocked'


def test_incomplete_retry_preserves_previous_payload_and_requests_revisioned_reprocess():
    url = catalog()['items'][0]['article_url']
    old = record(url, missing=['korean_meaning'])
    store = Store([old])
    process = AsyncMock(side_effect=ArticleFetchError('incomplete_lesson', 'missing', 422))
    state = asyncio.run(worker.backfill(store, apply=True, limit=1, delay=0, processor=process, sleep=AsyncMock()))
    assert process.call_args.kwargs['reprocess'] is True
    assert store.records[identifier(url)] is old
    assert state['status'] == 'incomplete'


def test_scrape_guard_rejects_incomplete_candidate_before_storage(monkeypatch):
    url = catalog()['items'][0]['article_url']
    incomplete = record(url, missing=['korean_explanation'])['payload']
    monkeypatch.setattr(worker, 'scrape_article', AsyncMock(return_value=SimpleNamespace(
        model_dump=lambda **_: incomplete)))
    with pytest.raises(ArticleFetchError) as error:
        asyncio.run(worker.complete_scrape(url))
    assert error.value.code == 'incomplete_lesson'


def test_concurrent_processing_respects_global_attempt_budget():
    store = Store()
    active, peak = 0, 0
    async def process(store, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        saved = record(url)
        store.records[saved['id']] = saved
        active -= 1
        return SimpleNamespace(model_dump=lambda **_: saved), True
    process = AsyncMock(side_effect=process)
    state = asyncio.run(worker.backfill(store, apply=True, concurrency=2, limit=1,
                                      delay=0, processor=process))
    assert process.await_count == 1
    assert peak == 1
    assert state['remaining'] == 1 and state['stop_reason'] == 'attempt_limit'


def test_two_workers_can_process_independent_articles_and_finish_consistently(tmp_path):
    store = Store()
    active, peak = 0, 0
    async def process(store, url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        saved = record(url)
        store.records[saved['id']] = saved
        active -= 1
        return SimpleNamespace(model_dump=lambda **_: saved), True
    state = asyncio.run(worker.backfill(store, apply=True, concurrency=2, delay=0,
                                      report=tmp_path/'report.json', processor=process))
    assert peak == 2
    assert state['status'] == 'complete' and state['ready'] == 2
    import json
    assert json.loads((tmp_path/'report.json').read_text())['summary']['ready'] == 2


def test_hosted_time_budget_checkpoints_before_starting_another_inference():
    store = Store()
    ticks = iter([0, 100, 100, 100])
    process = AsyncMock()
    state = asyncio.run(worker.backfill(store, apply=True, max_seconds=10,
                                      clock=lambda: next(ticks), processor=process))
    process.assert_not_called()
    assert state['stop_reason'] == 'time_budget'
    assert state['status'] == 'incomplete'


def test_persisted_attempt_cap_routes_failure_to_review_without_billing_again():
    store = Store()
    store.backfill_retry_states = AsyncMock(return_value={
        identifier(item['article_url']): {'attempts': 5, 'error_code': 'incomplete_lesson'}
        for item in catalog()['items']})
    process = AsyncMock()
    state = asyncio.run(worker.backfill(store, apply=True, max_attempts=5, processor=process))
    process.assert_not_called()
    assert state['status'] == 'needs_review' and state['review_required'] == 2
    assert state['ready'] == 0


def test_retry_backoff_grows_without_removing_successful_saved_data():
    store = Store()
    sleep = AsyncMock()
    process = AsyncMock(side_effect=ArticleFetchError('ollama_invalid_response', 'invalid', 502))
    asyncio.run(worker.backfill(store, apply=True, passes=2, delay=0,
                               retry_base=2, sleep=sleep, processor=process))
    assert [call.args[0] for call in sleep.await_args_list] == [2, 2, 4, 4]
