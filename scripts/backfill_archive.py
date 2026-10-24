"""Audit or explicitly transcribe the archive with durable, bounded retries.

--apply runs the worker. Hosted runs use a time budget and resume from Firestore;
ordinary reads and mobile navigation never invoke processing.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import monotonic

from app.services.discovery import stable_url
from app.services.errors import ArticleFetchError
from app.services.lesson_readiness import missing_sections
from app.services.ollama_vision import OllamaVisionService
from app.services.saved import process_and_store
from app.services.scrape import scrape_article
from app.services.storage import FirestoreStore, identifier

FATAL = {'ollama_rate_limited', 'ollama_access_denied', 'ollama_not_configured', 'storage_unavailable'}


async def complete_scrape(url):
    # Each concurrent job owns its recognizer. The public web service retains its
    # shared single-inference gate; only the hosted worker opts into concurrency.
    recognizer = OllamaVisionService()
    try:
        result = await scrape_article(url, recognizer=recognizer.recognize)
    finally:
        recognizer.close()
    if missing_sections(result.model_dump(mode='json')):
        raise ArticleFetchError('incomplete_lesson', 'Required lesson sections are missing.', 422)
    return result


def catalog_jobs(catalog, records, priority=(), retries=None):
    known = {stable_url(row['canonical_url']): row for row in records}
    retries = retries or {}
    jobs, seen = [], set()
    for item in catalog['items']:
        url = stable_url(item['article_url'])
        if url in seen:
            continue
        seen.add(url)
        key = identifier(url)
        retry = retries.get(key, {})
        missing = missing_sections(known.get(url))
        jobs.append({'id': key, 'url': url, 'date': item['issue_date'],
                     'expression': item['expression'], 'missing': missing,
                     'attempts': int(retry.get('attempts', 0)),
                     'error': retry.get('error_code') if missing else None,
                     'review_required': False})
    if len(jobs) != catalog['total']:
        raise ValueError('Catalog total does not match its unique article URLs')
    order = {name: index for index, name in enumerate(priority)}
    jobs.sort(key=lambda job: (order.get(job['expression'], len(order)),
                              -int(job['date'].replace('-', '')), job['id']))
    return jobs


async def saved_records(store):
    if hasattr(store, 'saved_content_records'):
        return await store.saved_content_records()
    records, offset = [], 0
    while True:
        page = await store.articles(limit=100, offset=offset)
        for item in page['items']:
            record = await store.lesson(item['id'])
            if record:
                records.append(record)
        offset += len(page['items'])
        if offset >= page['total'] or not page['items']:
            return records


def summary(jobs, status):
    unfinished = [job for job in jobs if job['missing']]
    return {'status': status, 'total': len(jobs), 'ready': len(jobs) - len(unfinished),
            'remaining': len(unfinished), 'attempts': sum(job['attempts'] for job in jobs),
            'failed': sum(bool(job['error']) for job in unfinished),
            'review_required': sum(job['review_required'] for job in unfinished),
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'run_url': (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                        f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                        if os.environ.get('GITHUB_RUN_ID') else None),
            'failures': [{'id': job['id'], 'error': job['error'], 'missing': job['missing'],
                          'review_required': job['review_required']}
                         for job in unfinished if job['error']][:20]}


async def backfill(store, *, apply=False, limit=None, passes=3, delay=1,
                   priority=(), report=None, processor=process_and_store,
                   concurrency=1, max_seconds=None, max_attempts=5,
                   retry_base=2, sleep=asyncio.sleep, clock=monotonic):
    started = clock()
    catalog = await store.expression_catalog()
    retries = await store.backfill_retry_states() if hasattr(store, 'backfill_retry_states') else {}
    jobs = catalog_jobs(catalog, await saved_records(store), priority, retries)
    checkpoint_lock = asyncio.Lock()
    attempted, fatal = 0, None
    stop_reason = None

    async def checkpoint(status):
        async with checkpoint_lock:
            state = {**summary(jobs, status), 'attempts_this_run': attempted,
                     'stop_reason': stop_reason}
            if report:
                target = Path(report)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps({'summary': state, 'lessons': jobs}, ensure_ascii=False, indent=2))
                temporary.replace(target)
            if apply:
                await store.collection('archive_backfill').document('state').set(state)
            return state

    await checkpoint('running' if apply else 'audit')
    if not apply:
        return summary(jobs, 'audit')

    async def consume(queue, pass_number):
        nonlocal attempted, fatal, stop_reason
        while not queue.empty() and fatal is None:
            if max_seconds is not None and clock() - started >= max_seconds:
                stop_reason = 'time_budget'
                return
            if limit is not None and attempted >= limit:
                stop_reason = 'attempt_limit'
                return
            job = queue.get_nowait()
            try:
                current = await store.lesson(job['id'])
            except ArticleFetchError as exc:
                job['error'] = exc.code
                fatal = exc
                await checkpoint('blocked')
                return
            job['missing'] = missing_sections(current)
            if not job['missing']:
                job['error'] = None
                continue
            if job['attempts'] >= max_attempts and job['error'] not in FATAL:
                job['review_required'] = True
                continue
            # Account for other coroutines that may have started while this
            # worker awaited the saved-record read.
            if fatal is not None:
                return
            if limit is not None and attempted >= limit:
                stop_reason = 'attempt_limit'
                return
            if max_seconds is not None and clock() - started >= max_seconds:
                stop_reason = 'time_budget'
                return
            attempted += 1
            job['attempts'] += 1
            try:
                saved, _ = await processor(store, job['url'], scrape=complete_scrape,
                                           reprocess=bool(current))
                job['missing'] = missing_sections(saved.model_dump(mode='json'))
                job['error'] = 'incomplete_lesson' if job['missing'] else None
            except ArticleFetchError as exc:
                job['error'] = exc.code
                if exc.code in FATAL:
                    fatal = exc
            job['review_required'] = bool(job['missing'] and job['attempts'] >= max_attempts
                                          and job['error'] not in FATAL)
            state = await checkpoint('blocked' if fatal else 'running')
            print(json.dumps({'ready': state['ready'], 'total': state['total'],
                              'expression': job['expression'], 'error': job['error'],
                              'pass': pass_number + 1}, ensure_ascii=False), flush=True)
            if fatal is None:
                backoff = min(60, retry_base * 2 ** min(job['attempts'] - 1, 5)) if job['error'] else 0
                await sleep(max(delay, backoff))

    for pass_number in range(passes):
        queue = asyncio.Queue()
        for job in jobs:
            if job['missing'] and not job['review_required']:
                queue.put_nowait(job)
        await asyncio.gather(*(consume(queue, pass_number) for _ in range(concurrency)))
        if fatal:
            await checkpoint('blocked')
            raise fatal
        if all(not job['missing'] for job in jobs):
            return await checkpoint('complete')
        if stop_reason:
            break
    return await checkpoint('needs_review' if any(job['review_required'] for job in jobs) else 'incomplete')


async def main(args):
    store = FirestoreStore(os.environ['FIREBASE_PROJECT_ID'], os.getenv('FIRESTORE_DATABASE', '(default)'))
    try:
        await store.initialize()
        if args.apply:
            async with store.lease('archive-backfill') as lease:
                if lease is None:
                    # A running local/hosted worker owns the handoff. Do not
                    # start another processing loop or overwrite its progress.
                    print(json.dumps({'status': 'already_running'}), flush=True)
                    return 0
                os.environ['SCRAPE_MAX_CONCURRENT_REQUESTS'] = str(args.concurrency)
                if args.refresh_catalog:
                    from app.services.archive import sync_archive_page
                    await sync_archive_page(store, 1)
                    await store.publish_archive()
                state = await backfill(store, apply=True, limit=args.limit, passes=args.passes,
                                       priority=args.priority, report=args.report,
                                       concurrency=args.concurrency, max_seconds=args.max_seconds,
                                       max_attempts=args.max_attempts)
        else:
            state = await backfill(store, report=args.report, priority=args.priority)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        return 0 if (state['remaining'] == 0 or not args.apply or
                     state.get('stop_reason') in {'time_budget', 'attempt_limit'}) else 1
    finally:
        await store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--refresh-catalog', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--passes', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=1, choices=range(1, 5))
    parser.add_argument('--max-seconds', type=float)
    parser.add_argument('--max-attempts', type=int, default=5)
    parser.add_argument('--priority', action='append', default=[])
    parser.add_argument('--report', default='.backfill/archive.json')
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1 or args.passes < 1 or args.max_attempts < 1
            or args.max_seconds is not None and args.max_seconds <= 0):
        parser.error('limits must be positive')
    raise SystemExit(asyncio.run(main(args)))
