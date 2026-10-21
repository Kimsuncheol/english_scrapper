# Chosun English Lesson API reference

API version: `0.1.0`; URL prefix: `/api/v1` (except `/health`).
This document describes the repository's implemented HTTP contract, including
optional development routes. It does not assert that a particular deployment has
every optional feature enabled.

The service finds an article's lesson **image**, downloads and validates it in
memory, then runs Korean/English OCR. It does not retrieve the graphic's text as
native article HTML. Article metadata and archive titles come from publisher
page/feed data; lesson text is an OCR estimate and may require correction.

## Base URLs and interactive documentation

| Environment | Base URL |
| --- | --- |
| Standard local Uvicorn command | `http://127.0.0.1:8000` |
| Repository's `sh scripts/start-local.sh` | `http://127.0.0.1:8765` |
| Project's Render service | `https://english-scrapper.onrender.com` |

Append these paths to the selected base URL:

- `/docs`: FastAPI's interactive Swagger UI.
- `/redoc`: generated reference viewer.
- `/openapi.json`: live OpenAPI specification; authoritative for that running build.

An importable repository snapshot is [openapi.json](openapi.json). It contains
public routes only. Development routes deliberately use `include_in_schema=False`
and remain absent from OpenAPI even when enabled. The generated specification
models the trigger's `Authorization` as a header parameter: enter the complete
`Bearer …` value. Custom HTTPS/host validation is stricter than the general URL
format shown by JSON Schema.

Use `Content-Type: application/json` for scrape requests. JSON responses use
UTF-8, including Korean text. Timestamps are ISO 8601 with timezone information;
date-only values use `YYYY-MM-DD`. Treat returned IDs as opaque strings.

## Endpoint overview

| Method | Path | Success | Authentication | Purpose |
| --- | --- | --- | --- | --- |
| GET | `/health` | 200 | None | Lightweight process health |
| POST | `/api/v1/scrape` | 200 | None | Scrape one article and save its result |
| GET | `/api/v1/articles` | 200 | None | Paginate saved OCR result summaries |
| GET | `/api/v1/articles/{article_id}` | 200 | None | Retrieve one full saved result |
| GET | `/api/v1/jobs/status` | 200 | None | Inspect scheduling and persisted job state |
| POST | `/api/v1/jobs/daily/trigger` | 202 | Bearer token | Ask the server to process today's due job |
| GET | `/api/v1/jobs/daily` | 200 | None | Alias of `/jobs/status` |
| GET | `/api/v1/lessons` | 200 | None | Legacy summary list, without a total wrapper |
| GET | `/api/v1/lessons/{lesson_id}` | 200 | None | Legacy saved-result retrieval |

Only the daily trigger currently requires a token. The trigger token does **not**
protect manual scraping, saved records, job status, Swagger, or explicitly enabled
development routes. The application has no configured browser CORS middleware or
per-user/per-IP requests-per-minute rate limiter. Same-origin browser requests
work; a separately hosted browser client needs its own appropriate integration.

## Quick start

Start the app using the [repository setup instructions](../README.md). Manual
scraping and saved-result retrieval require configured Firestore credentials;
`/health` does not contact Firestore or initialize OCR.

```sh
BASE_URL=http://127.0.0.1:8765

curl --fail-with-body "$BASE_URL/health"

curl --fail-with-body -i "$BASE_URL/api/v1/scrape" \
  -H 'Content-Type: application/json' \
  --data '{"article_url":"https://www.chosun.com/national/nie/english/2026/10/21/I2PQNZFBDZHH7PTL7LILUZRDHM/"}'

curl --fail-with-body "$BASE_URL/api/v1/articles?limit=5&offset=0"
```

The scrape response's `Location` header identifies the saved-result endpoint.
Use its returned ID for subsequent retrieval; do not derive IDs yourself.

## GET /health

Response, HTTP 200:

```json
{"status":"ok"}
```

This is process health, not an upstream, database, OCR-library or model readiness
check. A healthy process can still return `storage_unavailable` or
`ocr_unavailable` from a scrape. OCR is initialized lazily once per process.

## POST /api/v1/scrape

Request model: `ScrapeRequest`. The only accepted property is required
`article_url`; unknown properties return HTTP 422.

```json
{
  "article_url": "https://www.chosun.com/national/nie/english/2026/10/21/I2PQNZFBDZHH7PTL7LILUZRDHM/"
}
```

URL requirements:

- Absolute HTTPS URL on exactly `chosun.com` or `www.chosun.com`, port 443.
- No embedded credentials, whitespace, control characters or backslashes.
- Localhost, IP literals, private/reserved destinations, other subdomains and
  non-Chosun hosts are rejected. DNS destinations are checked and pinned.
- Redirect destinations must satisfy the same policy. Image downloading has its
  own approved Chosun/CDN host and namespace validation.
- A syntactically valid approved URL can still fail if it is not an accessible
  article or does not contain a usable lesson image. This endpoint does not
  require the article title to match the Speaking English series.

Processing: URL validation → HTML fetch → metadata/image selection → validated
image download → OCR → lesson classification → Firestore save. The full article
image in `content_elements` is preferred over `og:image`; a fallback is marked.

Success is HTTP **200**, including newly created records, with `ScrapeResponse`
and a relative header such as:

```text
Location: /api/v1/articles/RETURNED_ARTICLE_ID
```

A complete, original synthetic response is available in
[examples/scrape-response.json](examples/scrape-response.json). It includes all
six labeled lesson sections and bounding boxes. Its URLs, timing and image
metadata are illustrative; `engine: "mock"` identifies fixture data, not a real
publisher OCR result. Production OCR uses `engine: "tesseract"`.

### Idempotency and concurrent requests

The normalized/canonical article URL is the persistence key. Query strings and
fragments are removed for saved-URL identity, and the approved bare host is
normalized to `www.chosun.com`. Known saved submissions return the first saved
payload with HTTP 200 and skip image download/OCR. A previously unseen canonical
alias may require processing once to discover its identity; the first committed
canonical result wins. There is no `Idempotency-Key` header or force-refresh flag.

A saved `partial` result is also reused: resubmitting the URL does not improve
its OCR or reclassify it. Concurrent work for the same article is protected by a
renewable Firestore lease. The default process admission limit is one active
scrape; contention returns HTTP 503 (`scrape_busy` or `ocr_busy`). OCR state is
serialized and retained per process.

### ScrapeResponse models

| Top-level property | Type | Meaning |
| --- | --- | --- |
| `article` | `ArticleMetadata` | Canonical article metadata |
| `lesson_image` | `LessonImageMetadata` | Selected, downloaded and validated graphic |
| `article_images` | array of `LessonImageMetadata` | Extracted candidates; not all are downloaded |
| `ocr` | `OCRText` | Raw recognized regions and combined text |
| `lesson` | `LessonParsingResult` | Conservative structured fields and parsing notes |
| `processing` | `ProcessingStatus` | Overall status, stages, timing and warnings |

`ArticleMetadata`:

| Property | Type | Meaning |
| --- | --- | --- |
| `url` | URL string | Canonical article URL |
| `title` | string or null | Publisher headline |
| `published_at` | timestamp or null | Original publication time; may be the preceding evening |
| `section` | string or null | Section when available |
| `language` | string or null | Publisher language metadata when available |

The public article model does not include an issue-date field. Development
archive/cards expose `issue_date` separately, derived from the actual dated
article URL, rather than treating `published_at` as the lesson day.

`LessonImageMetadata`:

| Property | Type | Meaning |
| --- | --- | --- |
| `url` | URL string | Original selected/candidate URL |
| `alt_text` | string or null | Available image description |
| `content_type` | string or null | Validated response MIME type when downloaded |
| `width`, `height` | positive integer or null | Image dimensions; selected image is verified by decoding |
| `resized_urls` | object of URL strings | Available publisher resize variants |
| `source` | `content_elements` or `og:image` | Selection source |
| `is_fallback` | boolean | Whether the selected image came from the OG fallback |
| `content_element_path` | integer array or null | Path through nested content-element arrays |
| `byte_size` | nonnegative integer or null | Downloaded byte count |
| `format` | string or null | Decoded image format |
| `download_url` | URL string or null | Final validated download URL after redirects |

`OCRText`:

| Property | Type | Meaning |
| --- | --- | --- |
| `raw_text` | string | Combined OCR text with line breaks; not publisher-native text |
| `korean_text`, `english_text` | string or null | Optional language-specific outputs; current normalizer leaves these null |
| `engine` | string | `tesseract` in the real implementation |
| `status` | `ok`, `low_confidence`, `empty` | Recognition outcome |
| `mean_confidence` | number 0–1 or null | Mean recognized region confidence; null when empty |
| `regions` | array of `OCRRegion` | Normalized word regions in engine traversal order |
| `warnings` | string array | Recognition limitations requiring review |

Each `OCRRegion` contains `text`, `confidence` (0–1), `bounding_box` and
`low_confidence` (boolean). A bounding box is
`{"x":48,"y":100,"width":120,"height":24}`: pixels from the decoded image's
top-left, x increasing rightward and y downward. Width/height are positive,
x/y nonnegative. The current engine does not resize the input before recognition.
`raw_text` joins words within OCR lines and joins lines with newlines. “Raw” here
means retained normalized OCR, not an unmodified Tesseract TSV export. Low
confidence words remain available. An `ok` mean does not guarantee every word is
reliable.

`LessonParsingResult` contains:

- `status`: `structured`, `partial`, `unparsed` or `empty`.
- `fields`: the seven properties below, each a `LessonField` or null.
- `reasons`: explanatory strings.
- `unassigned_region_indices`: zero-based positions in `ocr.regions` that were
  not assigned to a field.
- `method`: parser identifier, `labeled_layout_v1` for labeled layouts or
  `series_title_verified_v1` for a verified expression heading with other fields
  left unassigned.

| Field key | Meaning |
| --- | --- |
| `expression` | English expression |
| `korean_meaning` | Korean meaning |
| `korean_explanation` | Korean explanation |
| `english_dialogue` | English dialogue, including speaker labels and line breaks |
| `korean_dialogue_translation` | Korean dialogue translation |
| `vocabulary_notes` | Vocabulary notes |
| `source_credit` | Recognized credit, if present |

Each non-null `LessonField` has `text`, `region_indices`, `confidence` and
`bounding_box`. Region indices refer to the original `ocr.regions` array, not a
separately sorted array; layout parsing uses bounding boxes and reading order.
Missing or unreliable text is not invented. Clients must accept null fields and
show `ocr.raw_text`/`ocr.regions` for review, even when classification fails.

`ProcessingStatus` contains `status` (`completed` or `partial`),
`completed_stages`, `ocr_status`, `lesson_status`, `elapsed_ms` (nonnegative number)
and `warnings` (string array). Stages are `url_validation`, `article_fetch`,
`article_parse`, `image_download`, `ocr`, `lesson_parse`. HTTP 200 with `partial`
is a usable saved result, not an HTTP failure. Empty OCR is also returned cleanly
as an OCR/lesson status rather than automatically becoming a server error.
Warnings, an image fallback, uncertain OCR or incomplete classification can make
the overall result partial. `elapsed_ms` measures pipeline processing, not the
entire HTTP/Firestore round trip; a cached response retains the original timing.

## GET /api/v1/articles

Query parameters:

| Parameter | Default | Constraints |
| --- | --- | --- |
| `limit` | 20 | Integer, 1–100 |
| `offset` | 0 | Nonnegative integer |

Returns `ArticlePage`: `items`, `total`, `limit`, `offset`. Items are
`SavedLessonSummary` objects containing `id`, `canonical_url`, `article`,
`processing_status`, `created_at`, `updated_at`. Results are ordered by saved
creation time descending, with a document-ID tie-breaker, not lesson issue date.
The list contains saved OCR results, not all discovered archive titles.

```json
{"items":[],"total":0,"limit":20,"offset":0}
```

Advance by `offset + items.length` until reaching `total`. Inserts between requests
can shift offset pages; deduplicate by `id` when combining results. Empty/out-of-
range pages return 200 with an empty `items` array. Large offsets incur Firestore
skipped-document reads; use modest pagination for this endpoint.

## GET /api/v1/articles/{article_id}

Returns `SavedLessonResponse` with `id`, `canonical_url`, `payload`, `created_at`
and `updated_at`. `payload` is the complete `ScrapeResponse`. No article/image
fetch or OCR is performed. An unknown or invalid identifier returns HTTP 404
`article_not_found`.

```sh
ARTICLE_ID=REPLACE_WITH_RETURNED_ID
curl --fail-with-body "$BASE_URL/api/v1/articles/$ARTICLE_ID"
```

There are no implemented delete, edit, re-OCR or bulk-import HTTP endpoints.

## GET /api/v1/jobs/status

Returns `DailyStatusResponse`. `/api/v1/jobs/daily` returns the same model.

| Property | Meaning |
| --- | --- |
| `enabled` | `SCRAPE_ENABLED`; defaults false |
| `in_process_enabled` | Whether the opt-in local timer is actually enabled |
| `timezone`, `hour`, `minute` | Configured due time, defaults Asia/Seoul 06:00 |
| `storage_configured` | Whether a Firebase project ID is configured; not proof credentials work |
| `next_scheduled_at` | Next configured wall-clock due time, or null when disabled |
| `last_run` | Latest persisted `DailyRunRecord`, or null |
| `latest_run_result` | Compact latest result (`status`, `stats`, `error_code`, `finished_at`), or null |
| `failures` | Up to 20 latest failed URL records, newest attempt first |
| `local_error_code` | Local runner error code or null |

A disabled, unconfigured server can return:

```json
{
  "enabled": false,
  "in_process_enabled": false,
  "timezone": "Asia/Seoul",
  "hour": 6,
  "minute": 0,
  "storage_configured": false,
  "next_scheduled_at": null,
  "last_run": null,
  "failures": [],
  "local_error_code": null,
  "latest_run_result": null
}
```

`DailyRunRecord`: `id`, `scheduled_date`, `status`, `started_at`, `finished_at`,
`attempts`, `stats` (object of integer counters), `error_code`. Current run states
include `running`, `completed`, `partial`, `failed`, `interrupted`.
Counters include `discovered`, `saved`, `failed`, `skipped` and, when available,
`remaining`. A failure entry contains `url`, `attempts`, `error_code` and
`last_attempt_at`. Historical state can remain visible while scheduling is disabled.
A configured but unreachable Firestore store returns 503 rather than fabricated
empty history.

`next_scheduled_at` describes configuration, not a reserved/guaranteed execution.
With `SCRAPE_IN_PROCESS=false`, an external scheduler must actually call the
trigger. A sleeping/restarting Render Free web process does not guarantee a daily
timer. See the [deployment and scheduler setup](../README.md).

## POST /api/v1/jobs/daily/trigger

Required header: `Authorization: Bearer <SCRAPE_TRIGGER_TOKEN>`.
No request body is required. The token is a server environment secret; do not
commit it, put it in a URL, or forward it through redirects.

```sh
# Set SCRAPE_TRIGGER_TOKEN securely in your calling environment first.
# Use HTTPS for the hosted service; do not add curl --location.
curl --fail-with-body -X POST \
  'https://english-scrapper.onrender.com/api/v1/jobs/daily/trigger' \
  -H "Authorization: Bearer $SCRAPE_TRIGGER_TOKEN"
```

HTTP 202 response example:

```json
{"status":"accepted"}
```

| Response `status` | Meaning |
| --- | --- |
| `accepted` | An asynchronous attempt was scheduled; processing is not yet complete |
| `already_running` | This process already has an active job task |
| `already_completed` | This process already handled the due date |
| `disabled` | Daily scraping is disabled |
| `not_due` | Today's configured local due time has not arrived |
| `cooldown` | A recent local storage failure delayed another attempt |

All these trigger dispositions use HTTP 202. Poll `/api/v1/jobs/status` and
inspect today's `scheduled_date`, final status, counts and failures; 202 alone
is not proof of a successful scrape. No token configured returns 503
`trigger_unavailable`; a missing/wrong token when configured returns 401
`authentication_required`. These application errors do not include a
`WWW-Authenticate` or `Retry-After` header.

The job uses a database lease and one persisted run per local scheduled date.
Re-triggering cannot force a second terminal run for that date: `completed`,
`partial` and `failed` runs are terminal for the day. Interrupted unfinished work
can resume through later triggers after the lease expires/releases. Failed URLs
remain in persistent backlog for subsequent daily jobs and may be submitted
manually in the meantime. The default daily batch is five articles; discovery
prioritizes newest listed URLs before older pending backlog.

Transient article/image network failures and upstream 5xx failures get bounded
exponential retries in scheduled work (default three attempts, two-second base,
20-second delay cap plus jitter). Access-control blocks are not bypassed. Manual
scraping does not inherit this daily retry loop. The supplied external client,
`python -m scripts.trigger_daily`, separately wakes/polls a hosted service with
bounded retries; see the README for its environment variables and time budget.

## Legacy retrieval endpoints

`GET /api/v1/lessons?limit=20&offset=0` accepts the same bounds as `/articles` but
returns **only an array of summaries**, without `total`, `limit` or `offset`.
`GET /api/v1/lessons/{lesson_id}` returns `SavedLessonResponse`, like article
retrieval, but uses `lesson_not_found` for 404. Prefer `/articles` for new clients.

## Error contract

Application and request-validation errors use `ErrorResponse`:

```json
{
  "error": {
    "code": "no_lesson_image",
    "message": "No suitable article image or usable og:image fallback was found.",
    "details": []
  }
}
```

A missing required request field produces HTTP 422, for example:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed. Check the indicated fields.",
    "details": [
      {"location":["body","article_url"],"message":"Field required","type":"missing"}
    ]
  }
}
```

Malformed/disallowed URLs also produce `validation_error`, with the offending
field in `details`. Network-time checks can instead return `unsafe_url` or
`unsafe_destination`. Use `error.code` for program logic; exact validation text
and human-readable messages can vary.

| HTTP | Representative codes | Client action |
| --- | --- | --- |
| 401 | `authentication_required` | Supply the correct trigger Bearer token |
| 403 | `blocked_page`, `image_blocked` | Publisher denied/rate-limited access; do not bypass it |
| 404 | `article_not_found`, `lesson_not_found` | Check the saved ID |
| 413 | `result_too_large` | OCR payload exceeds the storage cap |
| 422 | `validation_error`, `unsafe_url`, `unsafe_destination`, `invalid_article`, `invalid_article_data`, `no_lesson_image` | Correct the request or review the unsupported article |
| 422 | `unsafe_image_url`, `unsafe_image_destination`, `invalid_image_content_type`, `invalid_image`, `image_dimensions_exceeded`, `ocr_image_too_large`, `ocr_invalid_image` | Review selected image/type/dimensions; not all images fit OCR limits |
| 500 | `ocr_failed`, `processing_failed` | Server-side processing failure; inspect safe server logs |
| 502 | `upstream_http_error`, `upstream_network_error`, `redirect_error`, `unsupported_encoding`, `html_too_large` | Upstream fetch/response failure; back off when transient |
| 502 | `image_http_error`, `image_network_error`, `image_redirect_error`, `image_encoding_error`, `image_too_large`, `discovery_invalid` | Upstream image/feed failure |
| 503 | `storage_unavailable`, `ocr_unavailable`, `trigger_unavailable` | Check server storage, OCR installation or trigger configuration |
| 503 | `scrape_busy`, `ocr_busy`, `series_check_busy` | Retry later with bounded backoff |
| 504 | `article_timeout`, `image_timeout`, `scrape_timeout`, `ocr_recognition_failed` | Processing deadline exceeded; retry cautiously |

`image_too_large`/`html_too_large` currently use 502, not 413; 413 specifically
covers the saved OCR payload limit. Publisher HTTP 429 is mapped to 403 access
blocking, not a public per-client API rate limit. The original upstream status
is not a public error-envelope property. Generic framework errors, such as an
unknown route (404) or unsupported method (405), retain FastAPI's `{"detail":…}`
shape rather than the application envelope.

## Limits and deployment behavior

These are default server settings, not request-body options:

| Environment variable | Default | Effect |
| --- | --- | --- |
| `ARTICLE_MAX_BYTES` | 2097152 | HTML/feed response byte cap; maximum parser cap is 2 MiB |
| `ARTICLE_CONNECT_TIMEOUT_SECONDS` | 5 | Article/feed connection timeout |
| `ARTICLE_READ_TIMEOUT_SECONDS` | 10 | Article/feed read timeout |
| `ARTICLE_TOTAL_TIMEOUT_SECONDS` | 30 | Article/feed total fetch deadline |
| `ARTICLE_MAX_REDIRECTS` | 3 | Article/feed redirect cap |
| `IMAGE_MAX_BYTES` | 10485760 | Downloaded image byte cap |
| `IMAGE_MAX_PIXELS` | 16000000 | Image decoder pixel cap |
| `IMAGE_MAX_DIMENSION` | 20000 | Maximum decoded width or height |
| `IMAGE_CONNECT_TIMEOUT_SECONDS`, `IMAGE_READ_TIMEOUT_SECONDS`, `IMAGE_TOTAL_TIMEOUT_SECONDS` | 5 / 10 / 30 | Image network deadlines |
| `IMAGE_MAX_REDIRECTS` | 3 | Image redirect cap |
| `OCR_MAX_BYTES` | 10485760 | OCR input byte cap |
| `OCR_MAX_PIXELS` | 4000000 | OCR input pixel cap; stricter than image decoding |
| `OCR_TIMEOUT_MS` | 30000 | Native recognition timeout |
| `OCR_MIN_CONFIDENCE` | 0.5 | Low-confidence threshold |
| `SCRAPE_TIMEOUT_SECONDS` | 90 | Pipeline deadline; excludes some persistence/HTTP overhead |
| `SCRAPE_MAX_CONCURRENT_REQUESTS` | 1 | Process-wide active scrape admission limit |

The serialized stored JSON payload must be at most **800,000 UTF-8 bytes**.
These caps have different stages: an image can pass download validation and still
exceed the stricter OCR pixel cap. Supported image MIME types are JPEG, PNG,
WebP and GIF; animated images are rejected.

Use one worker for the existing Render Free setup. Models are installed at build
time and loaded lazily per process; requests do not download models. Image bytes
are transient memory data, not durable files. Firestore holds metadata, source
URLs and OCR/structured output, not the original image bytes. Render sleep,
wake-up and OCR initialization can make the first request slow; client timeouts
must allow for that independently of the server's pipeline deadline. Neither
`/health` nor opening a saved result initializes OCR.

Scraping is a side-effecting operation. Call it sequentially by default, reuse
saved results, back off on busy/transient errors and avoid repeated tight-loop
publisher requests. The concurrency guard is not a multi-user rate limiter.

## Optional development-only HTTP API

These routes exist only when `ENABLE_TEST_SITE=true` **before app creation**.
The default is false. They are omitted from OpenAPI and require no additional
API authentication if explicitly enabled. Enabling them does not relax production
URL validation: public/manual endpoints still reject local fixture URLs.

Base path below: `/_dev/test-site/series`.

| Method and suffix | Parameters/body | Response |
| --- | --- | --- |
| GET `/saved` | `limit=5` (1–10), `offset=0` (nonnegative) | `SeriesSavedResponse`: `items`, `offset`, nullable `next_offset` |
| POST `/check` | No body | `SeriesCheckResponse`; discover newest listed lesson and scrape/save or reuse it |
| POST `/day` | Same `ScrapeRequest` as public scrape | Full `SeriesExpression` for the submitted day |
| GET `/archive` | `page=1` (1–400), optional `day=YYYY-MM-DD` | `ArchivePage`, ten metadata entries per page |

All four use HTTP 200 on success and `Cache-Control: no-store`. They share the
application error envelope and persistence/URL safeguards. `/check` caches
successful discovery for 60 seconds per process, processes only the newest
listed story, and can return 503 `series_check_busy` for overlap.

`SeriesExpression`: `id`, `article`, nullable `expression` and `korean_meaning`,
`processing_status`, `lesson_status`, `saved_at`, nullable `issue_date`,
`lesson_image`, `lesson`, `ocr`. The latter models are the same full output as
public scraping. `/saved` may return fewer matching series items than its limit;
follow `next_offset`, including when an intermediate `items` array is empty.

`SeriesCheckResponse`: `status` (`new`, `up_to_date`, `empty`), `source_url`,
`checked_at`, `discovery_cached`, `listed_count`, nullable `latest`
(`SeriesExpression`) and `listed_days` (each with `article_url`, nullable
`issue_date`). “New” means newly saved by this scraper, not necessarily published
since the client's previous visit. Its newest-page list is not the complete
historical archive.

`ArchivePage`: `items`, `total`, `page`, `pages`, nullable `publisher_total`,
`indexed_pages`, `expected_pages`, nullable `checked_at`. Each `ArchiveEntry`
contains `article_url`, `title`, `expression` and `issue_date`. An entry's
expression is **publisher headline metadata**, not proof OCR has run. Archive
entries need not have saved OCR result IDs; submit the entry's URL to `/day` or
`/api/v1/scrape` to get a full persisted lesson.

```sh
curl --fail-with-body "$BASE_URL/_dev/test-site/series/archive?page=143"
curl --fail-with-body "$BASE_URL/_dev/test-site/series/archive?day=2021-01-01"
curl --fail-with-body "$BASE_URL/_dev/test-site/series/saved?limit=5&offset=0"
curl --fail-with-body -X POST "$BASE_URL/_dev/test-site/series/check"
```

Archive pagination reads a published Firestore metadata snapshot sorted by issue
date descending with a document-ID tie-breaker. `total` is its saved entry count,
not the number already OCR-processed. `publisher_total` is the publisher's recorded
count; `indexed_pages`/`expected_pages` show import progress. Exact-date lookup
queries catalog entries directly and can find a newly saved day before the
paginated snapshot is rebuilt. Publication timestamps and issue dates can differ.
The operator command `python -m scripts.index_archive` refreshes all source pages
and publishes the snapshot; `--resume` finishes an interrupted import. There is
no HTTP bulk-index trigger. See the README for Firestore credentials and limits.

The HTML playground is at `/_dev/test-site/`; four original synthetic fixture
pages remain accessible through its navigation at `/articles/one-tall-image`,
`/articles/thumbnail-and-tall`, `/articles/no-lesson-image` and
`/articles/malformed-data`, relative to that playground path. Fixture assets and
pure parser debugging do not invoke external HTTP or real OCR. The local E2E
harness routes logical approved requests to fixtures and mocks OCR; it does not
add an HTTP endpoint that allows public localhost scraping.

## Updating this reference

Routes/models live in `app/api.py`, `app/daily_api.py`, `app/dev_site/__init__.py`
and `app/schemas.py`. Regenerate the public OpenAPI snapshot after contract changes:

```sh
ENABLE_TEST_SITE=false SCRAPE_ENABLED=false .venv/bin/python - <<'PY'
import json
from pathlib import Path
from app.main import create_app
Path('docs/openapi.json').write_text(
    json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
PY
```

This generation does not start the app, connect to Firestore, fetch publisher
pages or initialize OCR. The example JSON is validated against `ScrapeResponse`;
keep examples synthetic and do not copy complete publisher lesson content into
repository documentation.
