# Chosun English Lesson API

Minimal FastAPI skeleton for a future service that extracts Korean and English
lesson text from images embedded in Chosun.com articles. Scraping, downloading,
and OCR are **not implemented**. Valid extraction requests currently return HTTP
501; they do not access the network or write files.

## Run locally

Use Python 3.12 and run these commands from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

The API runs at http://127.0.0.1:8000. Interactive API documentation is available
at http://127.0.0.1:8000/docs, with the JSON schema at `/openapi.json`.

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl -i -X POST http://127.0.0.1:8000/api/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"article_url":"https://www.chosun.com/example/"}'
```

The scrape example uses a placeholder article path. Its response is HTTP 501:

```json
{
  "error": {
    "code": "not_implemented",
    "message": "Article scraping and OCR are not implemented yet.",
    "details": []
  }
}
```

Requests require an absolute HTTP(S) `article_url` on `chosun.com` or one of its
subdomains, without embedded credentials. Missing fields, malformed URLs,
unsupported hosts, extra fields, and malformed JSON return HTTP 422 with an
`error.code` of `validation_error`. Each issue includes its `location`, `message`,
and `type`. URL validation does not establish that an article exists or contains
a lesson image.

The future success response is defined by `ScrapeResponse`:

- `article`: source URL, optional title and publication timestamp.
- `lesson_image`: image URL, optional alt text, content type, width, and height.
- `ocr`: raw text, optional Korean text, and optional English text. Optional
  language fields mean unavailable, rather than guaranteeing sentence alignment.

## Structure

```text
app/
  main.py              # ASGI app and shared exception handlers
  api.py               # Health route and thin scrape route
  schemas.py           # Request, success, and error schemas
  services/
    scrape.py          # Extraction service stub
tests/
  test_api.py           # Endpoint, validation, delegation, and schema checks
```

Future article parsing, image downloading, and OCR modules belong under
`app/services/`, orchestrated by `scrape.py`; HTTP handlers should only delegate.

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use an in-process client and a mocked success service; they require no
article access, OCR engine, or external credentials. Runtime dependencies are
pinned in `requirements.txt`; development dependencies are separate.

## Render Free

Deploy as a Python Web Service using Python 3.12, with:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check path: `/health`

Use the Free instance type and omit `--reload` in deployment. The skeleton has no
database, background workers, OCR models, or runtime storage requirements.

Render's filesystem is ephemeral: files can disappear on restart, redeployment,
or idle spin-down. Future downloads must use bounded in-memory data or unique,
request-scoped temporary files cleaned up on success and failure. Requests must
never depend on downloads from an earlier request or return local file paths.
Persisting results would require a separate durable store.

Keep one application worker to avoid duplicating future OCR memory use. Before
adding OCR, measure memory usage and latency on the Free instance and bound
download sizes, decoded pixels, execution time, and concurrent OCR work. The
current health response only indicates that the API is running.
