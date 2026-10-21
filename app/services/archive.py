"""Publisher-paginated, persistent lesson catalog; full OCR stays on demand.

The publisher's HTML ignores ?page=. Its own pagination uses this public
Fusion story-feed with an offset. No JavaScript is executed or URLs guessed.
"""

import asyncio
from datetime import date
import json
import math
from urllib.parse import urlencode, urljoin

from app.schemas import ArchiveEntry, ArchivePage
from app.services.article import fetch_article
from app.services.discovery import SERIES_PAGE, SERIES_TITLE, stable_url
from app.services.errors import ArticleFetchError
from app.services.series import issue_date_from_url

PAGE_SIZE = 10
MAX_PAGES = 400  # Same bound as the publisher's pagination widget.
FEED = "https://www.chosun.com/pf/api/v3/content/fetch/story-feed"


def parse_feed(data, page):
    if not isinstance(data, dict) or not isinstance(data.get("content_elements"), list):
        raise ArticleFetchError("discovery_invalid", "Publisher archive feed is malformed.")
    total = data.get("count")
    if type(total) is not int or not 0 <= total <= MAX_PAGES * PAGE_SIZE:
        raise ArticleFetchError("discovery_invalid", "Publisher archive count is missing or unsupported.")
    rows = data["content_elements"]
    expected = max(0, min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE))
    if len(rows) != expected:
        raise ArticleFetchError("discovery_invalid", "Publisher archive page is incomplete.")
    entries = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ArticleFetchError("discovery_invalid", "Publisher archive entry is malformed.")
        headlines = row.get("headlines")
        title = headlines.get("basic") if isinstance(headlines, dict) else None
        href = row.get("canonical_url") or row.get("website_url")
        try:
            url = stable_url(urljoin(SERIES_PAGE, href)) if isinstance(href, str) else ""
        except ValueError:
            url = ""
        day = issue_date_from_url(url)
        if not day or not isinstance(title, str) or not SERIES_TITLE.match(title):
            raise ArticleFetchError("discovery_invalid", "Publisher archive contains an invalid series entry.")
        entries[url] = ArchiveEntry(article_url=url, title=title.strip(),
            expression=SERIES_TITLE.sub("", title).strip(), issue_date=day)
    if len(entries) != len(rows):
        raise ArticleFetchError("discovery_invalid", "Publisher archive page repeats article URLs.")
    return list(entries.values()), total


async def fetch_archive_page(page):
    if type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError("Archive page must be between 1 and 400")
    query = {"excludeContentTypes": "", "expandRelated": False, "includeAuthors": "",
        "includeContentTypes": "story", "includeTags": "daily-speaking-english",
        "size": PAGE_SIZE, "tagSearchCriterion": "slug", "offset": (page - 1) * PAGE_SIZE}
    url = FEED + "?" + urlencode({"query": json.dumps(query, separators=(",", ":")), "_website": "chosun"})
    # Share DNS pinning, HTTPS/redirect checks, timeouts and the 2 MiB cap.
    fetched = await fetch_article(url, listing=True, json_response=True)
    try:
        data = json.loads(fetched.html)
    except (ValueError, RecursionError) as exc:
        raise ArticleFetchError("discovery_invalid", "Publisher archive feed is malformed.") from exc
    return parse_feed(data, page)


async def sync_archive_page(store, page, *, fetch=fetch_archive_page, sleep=asyncio.sleep):
    """One bounded page per call, resumable across Render restarts.

    Serialize archive refreshes across instances with the existing renewable
    Firestore lease. Never replace/delete older records when page one changes.
    """
    async with store.lease("series-archive") as lease:
        if lease is None:
            raise ArticleFetchError("archive_busy", "Archive refresh is already running. Retry shortly.", 503)
        for attempt in range(3):
            try:
                items, total = await fetch(page)
                break
            except ArticleFetchError as exc:
                transient = exc.code in {"article_timeout", "upstream_network_error"} or (exc.upstream_status or 0) >= 500
                if not transient or attempt == 2:
                    raise
                await sleep(2 ** attempt)
        await store.save_archive_page(page, items, total, lease)
        return {"page": page, "items": len(items), "publisher_total": total,
            "expected_pages": math.ceil(total / PAGE_SIZE)}


async def archive_page(store, page=1, day: date | None = None):
    data = await store.archive_entries(page, day)
    return ArchivePage(**data)
