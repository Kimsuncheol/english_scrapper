# Chosun English Lesson API

Minimal FastAPI skeleton for a future service that extracts Korean and English
lesson text from images embedded in Chosun.com articles. Safe article HTML fetching is implemented. Lesson image downloading and OCR
are **not implemented**. After a successful article fetch, extraction requests
return HTTP 501. Requests write no files.

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

The scrape example uses a placeholder article path. A successful article fetch returns HTTP 501 (the placeholder itself may fail
article validation):

```json
{
  "error": {
    "code": "not_implemented",
    "message": "Lesson image extraction and OCR are not implemented yet.",
    "details": []
  }
}
```

Requests require an absolute HTTPS `article_url` on `chosun.com` or
`www.chosun.com`, using port 443 and without embedded credentials. Other
subdomains are not approved; extend `APPROVED_HOSTS` only after review. Missing fields, malformed URLs,
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
    scrape.py          # Fetch orchestration; image/OCR stub
    article.py         # Bounded async HTML fetching and article checks
    urls.py            # Shared HTTPS and approved-host policy
    errors.py          # Application fetch errors
tests/
  test_api.py           # Endpoint, validation, delegation, and schema checks
```

Future lesson image selection, downloading, and OCR modules belong under
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

## Article fetching policy

The async HTTPX fetcher uses a descriptive User-Agent, a 5-second connection
and 10-second read timeout, and a 30-second total budget including DNS and up to
three redirects. Every hop must pass the same HTTPS/host/port policy. DNS must
resolve exclusively to global public addresses. Connections target a validated
IP directly, while the original Host and TLS SNI hostname retain certificate
verification. Environment proxies and automatic redirects are disabled.

HTML is streamed with a 2 MiB limit, checked both against Content-Length and
actual received bytes. The service requests uncompressed HTML and rejects
unexpected compression, avoiding decompression memory spikes. HTML must have a
nonempty title plus `og:type=article` or supported Article/NewsArticle JSON-LD.
These are conservative article checks, not proof that a lesson image exists.
Publisher challenges are reported without browser automation or bypass attempts.

Fetch errors use the existing `error` envelope:

| Status | Codes |
| --- | --- |
| 422 | `validation_error`, `unsafe_url`, `unsafe_destination`, `invalid_article` |
| 403 | `blocked_page` (including publisher 401/403/429 and recognized challenges) |
| 502 | `upstream_http_error`, `upstream_network_error`, `redirect_error`, `html_too_large`, `unsupported_encoding` |
| 504 | `article_timeout` |
| 501 | `not_implemented`, after a valid HTML fetch |

Fetch tests mock DNS and HTTP transport; no tests access the live publisher.
