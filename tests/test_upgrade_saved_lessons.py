from urllib.error import HTTPError

import pytest
from scripts import upgrade_saved_lessons as migration


@pytest.mark.parametrize("apply", [False, True])
def test_upgrade_is_explicit_and_skips_already_migrated_records(monkeypatch, apply):
    calls = []

    def request(base, path, *, post=False):
        calls.append((path, post))
        if path.startswith('/api/v1/articles?'):
            return {"items": [{"id": key, "article": {"title": "[스피킹이 강해지는 영어] expression"}}
                              for key in ['legacy', 'ai']], "total": 2}
        if post:
            return {"ocr": {"engine": "ollama/gemma4:31b"}, "lesson": {"status": "structured"}}
        return {"payload": {"ocr": {"engine": "tesseract" if path.endswith('legacy') else "ollama/gemma4:31b"}}}

    monkeypatch.setattr(migration, 'request', request)
    migration.upgrade('https://example.com', apply=apply)
    writes = [path for path, post in calls if post]
    assert writes == (['/_dev/test-site/lessons/legacy/transcribe'] if apply else [])


def test_failed_upgrade_is_reported_without_retrying_or_blocking_other_records(monkeypatch):
    writes = []

    def request(base, path, *, post=False):
        if path.startswith('/api/v1/articles?'):
            return {"items": [{"id": key, "article": {"title": "[스피킹이 강해지는 영어] expression"}}
                              for key in ['failed', 'good']], "total": 2}
        if post:
            writes.append(path)
            if 'failed' in path:
                raise HTTPError(path, 502, 'provider failed', {}, None)
            return {"ocr": {"engine": "ollama/gemma4:31b"}, "lesson": {"status": "structured"}}
        return {"payload": {"ocr": {"engine": "tesseract"}}}

    monkeypatch.setattr(migration, 'request', request)
    with pytest.raises(RuntimeError, match='failed IDs: failed'):
        migration.upgrade('https://example.com', apply=True)
    assert len(writes) == 2
