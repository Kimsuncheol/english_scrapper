"""Resolve requested dates from publisher archive links, never guessed IDs."""
import asyncio
from app.services.archive import fetch_archive_page, PAGE_SIZE
from app.services.errors import ArticleFetchError


async def resolve_issue_date(day):
    try:
        async with asyncio.timeout(25):
            for page in range(1, 11):
                entries, total = await fetch_archive_page(page)
                matches = [entry.article_url for entry in entries if entry.issue_date == day]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise ArticleFetchError('ambiguous_issue_date', 'Multiple articles match this date. Submit an article URL.', 422)
                if page * PAGE_SIZE >= total:
                    raise ArticleFetchError('issue_date_not_found', 'No publisher article was found for this date.', 404)
    except TimeoutError as exc:
        raise ArticleFetchError('date_lookup_timeout', 'Publisher date lookup timed out. Retry later.', 504) from exc
    raise ArticleFetchError('date_search_limit', 'Date is outside the first 100 archive entries. Submit its article URL.', 422)
