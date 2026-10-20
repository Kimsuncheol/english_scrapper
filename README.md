# Chosun English Lesson API

FastAPI service that safely fetches Chosun.com articles and extracts article
metadata and lesson-image URLs. Image downloading and OCR are not implemented:
successful requests return parsed metadata with `ocr: null`. No files are written
at runtime.

## Run locally

Use Python 3.12 from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

The API runs at http://127.0.0.1:8000. Interactive documentation is at `/docs`
and the OpenAPI schema at `/openapi.json`.

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok"}

# Replace the placeholder with a real article URL.
curl -i -X POST http://127.0.0.1:8000/api/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"article_url":"https://www.chosun.com/example/"}'
```

## Response

A successful scrape returns HTTP 200:

- `article`: canonical `url`, `title`, `published_at`, `section`, and `language`.
  Unavailable optional fields are `null`.
- `lesson_image`: selected original `url`, dimensions, alt text, `resized_urls`,
  `source`, `is_fallback`, and `content_element_path`.
- `article_images`: all valid image URL candidates in body content order,
  including those below selection size thresholds. Paths such as `[2]` or
  `[0, 1]` identify indices in body or nested gallery `content_elements`.
- `ocr`: `null` until OCR is implemented. The OCRText schema remains available
  for future raw, Korean, and English output.

Metadata comes from Article/NewsArticle JSON-LD (including arrays and `@graph`)
with Fusion page-data fallbacks. Valid HTML canonical links take priority for
the canonical URL; otherwise JSON-LD and page data are used, then the fetched URL.
Unapproved canonical hosts are ignored. Invalid dates remain `null` unless
another available publication-date field is valid.

`Fusion.globalContent` is read only from a JSON object assignment or
`JSON.parse` of a JSON string. Strings and comments are skipped while locating
assignments. JavaScript is never executed; object literals containing JavaScript
expressions, functions, or `undefined` are not interpreted. Malformed optional
JSON-LD/Fusion data is ignored so other available metadata or OG fallback can
still be used.

## Image selection

Only `type: image` entries in body `content_elements` and nested
`gallery`/`container`/`group` elements are considered. Promo and related-story
images are excluded. Original URLs and provided resize variants are retained;
no URLs are synthesized or downloaded. Available `additional_properties`
original URLs take priority over the element URL.

The default `ImageSelectionConfig` in `app/services/parsing.py` uses minimum
width/height of 300 pixels and a tall height/width ratio of 1.2. Candidates below
known minimum dimensions are excluded. Tall images rank first, then known
dimensions, aspect ratio, and pixel area; ties preserve article order. Unknown
dimensions are allowed by default to prefer an original article image over an
OG thumbnail. Set `allow_unknown_dimensions=False` to require dimensions, or
`prefer_tall_images=False` to rank known images by pixel area instead.

If no suitable article image exists, `og:image` is returned with
`source: "og:image"` and `is_fallback: true`. It may be a thumbnail; the flag does
not claim it is the full lesson graphic. Set `allow_og_fallback=False` to disable
this behavior. If neither source is usable, the API returns `no_lesson_image`.
Selection is a configurable geometric heuristic, not visual confirmation of a
lesson. Pass a config to `parse_article_html(html, source_url, config)` to tune it.

Image URLs are untrusted metadata. They are not a download allowlist; a future
image downloader must apply its own host, redirect, DNS, and byte-size policy.

## Structure

```text
app/
  main.py              # ASGI app and shared exception handlers
  api.py               # Thin HTTP routes
  schemas.py           # Request, article, image, OCR, and error contracts
  services/
    scrape.py          # Fetch/parse orchestration
    article.py         # Bounded async HTML fetching and article checks
    parsing.py         # Pure metadata parsing and configurable image selection
    urls.py            # HTTPS article-host policy
    errors.py          # Shared application errors
tests/
  fixtures/            # Synthetic representative HTML samples
  test_api.py
  test_article.py
  test_parsing.py
```

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use local fixtures and mocked DNS/HTTP transport. No tests access the live
publisher. Fixture provenance and covered page shapes are documented in
`tests/fixtures/README.md`. No new parser dependencies are needed.

## Fetch policy and errors

Article URLs must use HTTPS, port 443, and `chosun.com` or `www.chosun.com`,
without embedded credentials. Other subdomains require explicit approval in
`APPROVED_HOSTS`. Every redirect follows the same policy. DNS must resolve only
to global public addresses; connections are pinned to a checked IP with the
original TLS hostname retained. Environment proxies and automatic redirects are
disabled. The fetcher uses a descriptive User-Agent, a 5-second connection timeout,
10-second read timeout, a 30-second total budget, and at most three redirects.

HTML is streamed with a 2 MiB limit checked against both Content-Length and
actual received bytes. Unexpected compression is rejected. Article recognition
accepts `og:type=article`, Article/NewsArticle JSON-LD, or Fusion story data with
body content and a title. Access-control/challenge pages are reported without
browser automation or bypass attempts.

Errors use an `error` object with `code`, `message`, and `details`:

| Status | Codes |
| --- | --- |
| 422 | `validation_error`, `unsafe_url`, `unsafe_destination`, `invalid_article`, `no_lesson_image` |
| 403 | `blocked_page` |
| 502 | `upstream_http_error`, `upstream_network_error`, `redirect_error`, `html_too_large`, `unsupported_encoding` |
| 504 | `article_timeout` |

## Render Free

Deploy as a Python Web Service using Python 3.12:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check: `/health`

Select the Free instance and omit `--reload`. There is no database, background
worker, OCR model, browser, or runtime storage requirement. Render's filesystem
is ephemeral. Future downloads must use bounded memory or unique request-scoped
temporary files cleaned up on success and failure. Requests must never depend on
files from earlier requests. Keep one worker and measure memory/latency before
adding OCR; bound image sizes, decoded pixels, runtime, and concurrent work.
