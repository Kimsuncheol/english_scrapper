from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import pytest
from app.services import dated_lesson
from app.services.errors import ArticleFetchError


def test_resolves_missing_days_using_publisher_links(monkeypatch):
    rows=[SimpleNamespace(issue_date=date(2026,11,d),article_url=f'https://www.chosun.com/article/{d}/') for d in (11,10,9)]
    fetch=AsyncMock(return_value=(rows,3))
    monkeypatch.setattr(dated_lesson,'fetch_archive_page',fetch)
    for d in (9,10,11):
        assert asyncio.run(dated_lesson.resolve_issue_date(date(2026,11,d))).endswith(f'/{d}/')


def test_absent_date_never_substitutes_latest(monkeypatch):
    monkeypatch.setattr(dated_lesson,'fetch_archive_page',AsyncMock(return_value=([],0)))
    with pytest.raises(ArticleFetchError) as e:
        asyncio.run(dated_lesson.resolve_issue_date(date(2026,11,9)))
    assert e.value.code=='issue_date_not_found'
