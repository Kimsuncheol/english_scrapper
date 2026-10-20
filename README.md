# Chosun English Lesson API

FastAPI service that safely fetches Chosun.com articles and extracts article
metadata and lesson-image URLs, then downloads and validates the selected image
in memory. Tesseract recognizes Korean and English text and returns word regions
with confidence and pixel bounding boxes. A conservative layout parser organizes
supported labeled sections into lesson fields. No request files are written at runtime.
Manual and scheduled scraping use the same processing/persistence service.
Results are saved in external PostgreSQL and can be retrieved through the API.

The lesson graphic is a raster image: this service locates and downloads it,
then recognizes its pixels with OCR. It does **not** extract the graphic as
native/selectable text from the article HTML. Page metadata comes from HTML/page
data, while lesson text is an OCR estimate that can contain errors.

## Saved article API

Configure `DATABASE_URL` even when `SCRAPE_ENABLED=false` if using manual
scraping. There is no ephemeral/local storage fallback. `/health` does not need
PostgreSQL or OCR initialization.

| Endpoint | Response |
| --- | --- |
| `POST /api/v1/scrape` | Submit `{"article_url":"https://www.chosun.com/.../"}`; return the complete `ScrapeResponse` and a `Location: /api/v1/articles/{article_id}` header |
| `GET /api/v1/jobs/status` | Enabled state, schedule/timezone, `last_run`, `next_scheduled_at` (null when disabled), and `latest_run_result` with status, counts, error code and finish time |
| `GET /api/v1/articles?limit=20&offset=0` | Paginated `{items, total, limit, offset}`; items contain stable ID, canonical URL, article metadata, processing status and creation/update timestamps |
| `GET /api/v1/articles/{article_id}` | `{id, canonical_url, payload, created_at, updated_at}`; `payload` includes image metadata, OCR regions/raw text, structured lesson fields and processing status |

Manual and scheduled requests call the same fetch → parse → image download → OCR
→ lesson parsing pipeline and persistence service. PostgreSQL article locks stop
overlapping work for the same normalized URL. Known successful duplicates return
the first saved response with `200`, without another download or OCR call. The
canonical URL is unique across all saved results; an unseen alias may need one
processing pass to discover its canonical URL, but cannot overwrite the first
saved result or create a second record. Successful alias mappings survive restarts.
Partial/empty OCR results are also retained for review and reused on submission.

An in-flight duplicate receives `503 scrape_busy`; retry after the first request
finishes. Failed processing returns its existing concise application error and
records the URL/error for a later submission or daily retry. Database outages
return `503 storage_unavailable`; a response never claims a successful save before
the database commit. Unknown saved IDs return `404 article_not_found`; invalid URL
or pagination inputs return `422 validation_error`. The API never returns database
credentials, upstream exception strings, image bytes or article HTML.

```sh
BASE_URL=http://127.0.0.1:8000
curl --fail "$BASE_URL/api/v1/jobs/status"
curl --fail "$BASE_URL/api/v1/articles?limit=10&offset=0"
# Use an ID from items[].id or the manual response's Location header.
curl --fail "$BASE_URL/api/v1/articles/ARTICLE_ID"
```

Pagination uses `limit` (1–100, default 20) and `offset` (nonnegative, default 0).
Results are ordered by creation time descending, with ID as the tie-breaker. Each
page's total/items use a consistent database snapshot; newly added records between
requests can shift offsets. No saved records returns `200` with an empty `items`
array and `total: 0`.

The existing `/jobs/daily`, `/lessons`, and `/lessons/{lesson_id}` routes remain
available for compatibility; `/lessons` retains its original array response.
Article/status reads and manual scraping currently have no per-user access
controls. The existing global scrape/OCR admission limits apply; this is not a
public multi-user rate limiter. Keep request frequency cautious and add access
controls before exposing an unrestricted OCR submission service.

## Optional daily scrape workflow

Before the daily workflow, the repository had no database or persistent storage.
Both manual and scheduled processing now use PostgreSQL persistence;
there is deliberately **no SQLite/local-file fallback** on Render Free. Models
remain Docker image contents; request images are never saved to disk.

### Configuration

Set these environment variables before starting FastAPI (see `.env.example`,
which is documentation only; the app does not automatically load dotenv files):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPE_ENABLED` | `false` | Enable the application timer/daily runner |
| `SCRAPE_TIMEZONE` | `Asia/Seoul` | IANA schedule timezone |
| `SCRAPE_HOUR` | `6` | Local hour, 0–23 |
| `SCRAPE_MINUTE` | `0` | Local minute, 0–59 |
| `DATABASE_URL` | unset | Persistent PostgreSQL URL; required for manual scraping, saved results, and enabled scheduling |
| `SCRAPE_TRIGGER_TOKEN` | unset | Secret Bearer token for external triggers |
| `SCRAPE_DAILY_MAX_ARTICLES` | `5` | Maximum queued articles attempted in one daily batch, supported range 1–20 |
| `SCRAPE_RETRY_ATTEMPTS` | `3` | Total attempts per transient operation, supported range 1–5 |
| `SCRAPE_RETRY_BASE_SECONDS` | `2` | Exponential backoff base; delays cap at 20 seconds plus up to 10% jitter |
| `SCRAPE_REQUEST_GAP_SECONDS` | `2` | Pause between article processing attempts/batches |

Only PostgreSQL URLs are accepted. Remote connections default to TLS if no
`sslmode` was supplied; use your provider's recommended certificate validation
(e.g. `sslmode=verify-full`) where available. Supply a **direct connection or
session-pooling endpoint**, never a transaction-pooling/PgBouncer transaction URL:
the session-level advisory lock must stay on the same backend connection for the
entire run. Use a dedicated database/schema and a role that can create the three
application tables on initial use. Secrets belong in deployment settings, not Git.

No database is created or purchased by `render.yaml`. Choose a durable external
PostgreSQL service and its retention/backup plan explicitly. Render's Free
Postgres expires after 30 days, so it is not a long-term archive by itself.
[Render Free database limitations](https://render.com/docs/free).

### Discovery and retained results

The fixed discovery source is the publisher's
[English-section page](https://www.chosun.com/national/nie/english/). It is fetched
once per daily run (with bounded retries) using the same HTTPS, public-IP pinning,
redirect, timeout and 2 MiB controls as article fetching. Links come from actual
HTML anchors or safely parsed Fusion `contentCache`/`globalContent` story objects.
Only titles beginning with `스피킹이 강해지는 영어` and approved article URLs under
`/national/nie/english/` are accepted. IDs/dates are never guessed and JavaScript is
never executed. A local live discovery check found 20 matching listed links;
this does not prove future coverage or that OCR worked on those articles.

The section is a rolling listing, not a historical archive API. Initial enabling
can discover earlier listed lessons; five are processed per day by default, with
remaining URLs queued persistently. New/missed articles that have already rolled
off the listing cannot be recovered automatically. Empty discovery may mean no
matching entries; monitor the persisted `discovered` count if it stays zero.
Challenge/error pages are recorded as failures instead of bypassed.

The job creates idempotent tables on first database use:

- `daily_runs`: one local scheduled-date key, start/finish timestamps, attempts,
  status, counts and concise error code.
- `daily_articles`: normalized article URL, pending/processing/failed/succeeded
  state, attempts, last-attempt timestamp, error code and saved-lesson ID.
- `saved_lessons`: stable ID, unique canonical URL, complete `ScrapeResponse` JSON,
  and creation/update timestamps. This includes article metadata, original/final
  image URLs and dimensions, raw OCR text/regions, structured fields and processing
  status; it excludes HTML and image bytes.

Tracking URLs remove query/fragment tracking and unify `chosun.com` with
`www.chosun.com`. Successful URLs are not processed again on the next day or after
restarts. Canonical-result uniqueness prevents duplicate saved lessons. Partial or
empty OCR responses are saved with their reviewable status rather than invented
text. Manual `POST /api/v1/scrape` uses the same persistence boundary and keeps
its existing `ScrapeResponse` body, with a `Location` header for the saved record.

### Locking, failures and catch-up

Every runner must acquire a **PostgreSQL session advisory lock** before discovery
or processing; a unique scheduled-date row adds durable daily idempotency. This
protects against overlapping deploys, external triggers, and multiple FastAPI
instances. The lock uses a dedicated live connection and is released when the
connection closes, including process exit. All instances should use the same
schedule configuration/database. Keep Render Free at one worker/instance; the
cross-instance database guard is not a reason to increase OCR memory use.
[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS).

Transient connection/read failures, upstream 5xx responses, and temporary busy
responses get bounded exponential retries. Upstream 404, invalid inputs, image
validation errors, and OCR installation failures are not retried immediately.
Publisher block/rate-limit responses stop the current batch without immediate
retries. Failed URLs remain queued and can be reconsidered by a later daily run;
existing failed/pending URLs are still processed when that day's discovery fails.
Attempts and errors are committed before/after processing so progress survives
restarts. A terminal daily run is not repeated on the same local date; a crashed
`running`/`interrupted` run can resume under a newly acquired lock.

Like other crash-recoverable jobs, this is at-least-once work with idempotent
saved results: a crash between OCR and the database commit can repeat a download
or OCR, but cannot create another canonical saved lesson. A database connection
loss aborts further work on that session. No local filesystem lock is used.

The in-process asyncio timer checks every 30 seconds **while the service is
awake**, launching at/after the configured local time once per date. On a late
wake-up after today's scheduled time, it catches up today's run rather than
creating a historical run for every missed date. UTC timestamps are stored; local
dates define the daily key. Scheduling does not block `/health` or initialize OCR
until a lesson actually reaches that stage. Database outage/reconnect attempts
are throttled locally; `/health` remains liveness, not database readiness.

### Render Free and external wake-up setup

Render Free sleeps after inactivity, so **an in-process timer alone cannot run
reliably at a daily wall-clock time**. The included GitHub Actions workflow supplies
an external wake-up at the default `06:00 Asia/Seoul`, calls the authenticated
trigger, and polls status every 30 seconds only while waiting for that batch. It
does not send continuous keep-alive pings between daily jobs. No paid Render Cron
Job/background-worker service is silently added.

1. Provision/select durable PostgreSQL. In Render Environment set `DATABASE_URL`
   to a direct/session connection URL, and set a strong `SCRAPE_TRIGGER_TOKEN`
   (for example generate one locally with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).
2. Set Render `SCRAPE_ENABLED=true` and review timezone/hour/minute. The Blueprint
   deliberately defaults it to false. Restart/redeploy so the frozen schedule
   config is reloaded. Keep the existing one-worker command and resource limits.
3. In GitHub repository **Settings → Secrets and variables → Actions**, add
   repository variables `SCRAPE_ENABLED=true` and `SCRAPE_BASE_URL` (the HTTPS
   Render service origin), and a repository secret `SCRAPE_TRIGGER_TOKEN` with
   the same token. The workflow receives no database password.
4. Enable **Daily Chosun lesson scrape** under Actions. The workflow must be on the
   default branch for scheduled execution. It uses `cron: "0 6 * * *"` with
   `timezone: Asia/Seoul`; GitHub schedule expressions cannot read application
   environment variables. **If changing SCRAPE_TIMEZONE/HOUR/MINUTE, also align
   those two workflow schedule fields**, or configure another trusted external
   scheduler to call the trigger at/after the new due time.
5. Use **Run workflow** for an initial check after the due time. Inspect
   `/api/v1/jobs/daily` and `/api/v1/lessons` for persisted results. Before the
   due time the application returns `not_due`; the external script flags a
   mismatched/early wake-up instead of silently pretending a scrape occurred.

GitHub scheduling is best-effort: it can be delayed, and public-repository
schedules can be disabled after 60 days without activity. Review Actions status
and usage/billing for your repository plan. The workflow has a 35-minute ceiling;
the client waits up to 30 minutes after wake-up, and reports partial/failed jobs
as workflow failures. Raising the five-article default can exceed Free CPU or this
polling budget. There is no claim that a job will run at the exact second or that
external scheduler/database provisioning has been performed.
[GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[Render sleep behavior](https://render.com/docs/free).

### Local schedule and API usage

Create a **local development PostgreSQL database** or use a separate hosted dev
DB, install `requirements.txt`, then export environment variables in the terminal
that starts Uvicorn. This example uses a local development connection; replace it
with a database you created and can access:

```sh
export DATABASE_URL='postgresql://localhost/english_scrapper'
export SCRAPE_ENABLED=true
export SCRAPE_TIMEZONE=Asia/Seoul
export SCRAPE_HOUR=6
export SCRAPE_MINUTE=0
# Set this privately if testing the trigger; do not commit the value.
export SCRAPE_TRIGGER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep this process running for the local timer. Starting after the due time can
immediately discover/process the rolling backlog. Leave `SCRAPE_ENABLED=false`
to disable scheduling during development. Manual processing still needs
`DATABASE_URL`. Without it, `/health` and disabled job status work, while manual
scraping and saved-result routes return `503 storage_unavailable`.
The schema is initialized automatically on first daily/status/result database use.

```sh
curl --fail http://127.0.0.1:8000/api/v1/jobs/daily
curl --include --request POST http://127.0.0.1:8000/api/v1/jobs/daily/trigger \
  --header "Authorization: Bearer $SCRAPE_TRIGGER_TOKEN"
curl --fail 'http://127.0.0.1:8000/api/v1/lessons?limit=20&offset=0'
# Replace LESSON_ID with an ID from the list response.
curl --fail http://127.0.0.1:8000/api/v1/lessons/LESSON_ID
```

`GET /api/v1/jobs/daily` exposes schedule configuration, next scheduled time,
last-run start/finish/status/counts/error, and up to 20 failed URLs with attempt
counts and timestamps. No token or database credentials are returned. Triggering
requires the Bearer token; saved lesson/status reads currently have no per-user
access controls, appropriate only when these public-article results are intended
to be readable by service users. The trigger respects enabled/due/daily-lock rules;
it cannot force a second completed run on the same date.

### Tests for daily processing

```sh
.venv/bin/python -m pytest -q
# Optional: use an isolated test database, NEVER production.
TEST_DATABASE_URL='postgresql://localhost/english_scrapper_test' \
  .venv/bin/python -m pytest tests/test_daily_postgres.py -q
```

PostgreSQL integration tests create fresh schemas and do not drop/truncate existing
data. They check the actual session lock, cross-connection exclusion, duplicate
URLs, interrupted-run recovery, saved JSON surviving a new repository instance,
and result/status APIs. Without `TEST_DATABASE_URL` those integration tests skip;
schedule/discovery/retry tests still run using test doubles. Local validation for
this change used an isolated temporary PostgreSQL 16 cluster plus mocked OCR/HTTP,
and a separate live series-link discovery check. A production database, GitHub
workflow, hosted OCR job, and Render deployment remain unconfigured/unverified.

## Run locally

Use Python 3.12. Install the native runtime and Korean/English language data
before installing Python requirements. On Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install libtesseract-dev libleptonica-dev g++ pkg-config tesseract-ocr-eng tesseract-ocr-kor
export TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
```

On macOS, `brew install tesseract tesseract-lang pkgconf` supplies the runtime,
headers, and languages (the language formula includes more than the two needed).
Set `TESSDATA_PREFIX` to `$(brew --prefix)/share/tessdata` when using Homebrew.
Alternatively, provision only `kor.traineddata` and `eng.traineddata` from the
linked `tessdata_fast` repository in a local directory and point
`TESSDATA_PREFIX` at that directory. Do this during setup, never per request.

Then, from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# Set a durable dev PostgreSQL connection before scraping.
export DATABASE_URL='postgresql://localhost/english_scrapper'
.venv/bin/python -m uvicorn app.main:app --reload
```

The API runs at http://127.0.0.1:8000. Interactive documentation is at `/docs`
and the OpenAPI schema at `/openapi.json`.

```sh
# For Render, replace this URL with your assigned https://...onrender.com URL.
BASE_URL=http://127.0.0.1:8000
curl --fail --show-error "$BASE_URL/health"
# {"status":"ok"}

# Replace the placeholder path with a real approved Chosun lesson article.
# -i includes HTTP status/headers so errors or a Render wake-up page are visible.
curl --include --show-error --max-time 180 \
  --request POST "$BASE_URL/api/v1/scrape" \
  --header 'Content-Type: application/json' \
  --data '{"article_url":"https://www.chosun.com/example/"}'
```

The placeholder is not a real lesson URL and is not expected to succeed. The
180-second client timeout leaves room for platform wake-up plus the app's default
90-second pipeline budget; it does not change the server timeout. Inspect the
status and Content-Type before decoding JSON after a Render cold start.

## Response

A successful scrape returns HTTP 200:

- `article`: canonical `url`, `title`, `published_at`, `section`, and `language`.
  Unavailable optional fields are `null`.
- `lesson_image`: selected original `url`, dimensions, alt text, `resized_urls`,
  `source`, `is_fallback`, and `content_element_path`. After downloading,
  `content_type`, `byte_size`, `format`, `download_url`, and measured dimensions
  describe the verified image. `url` retains the original selected URL.
- `processing`: overall `completed` or `partial` status, completed stage names,
  OCR/lesson status, elapsed milliseconds, and concise warnings.
- `article_images`: all valid image URL candidates in body content order,
  including those below selection size thresholds. Paths such as `[2]` or
  `[0, 1]` identify indices in body or nested gallery `content_elements`.
- `ocr`: combined `raw_text`, word `regions`, `mean_confidence`, `status`, `engine`,
  and `warnings`. Each region has `text`, `confidence` (0–1), `low_confidence`,
  and `bounding_box: {x, y, width, height}` in original-image pixels from the top
  left. Regions retain Tesseract reading order; words on the same line are joined.
  `korean_text`/`english_text` remain `null`: recognition uses both languages
  together. Structured lesson sections are provided separately in `lesson`.

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

### Example success response

HTTP **200**. This is a **synthetic, schema-validated example**, not a live article
extraction or a verified Render response. Only expression and Korean meaning are
present, so the other optional lesson fields remain null. A `structured` lesson
means the observed labeled sections were assigned, not that all seven exist.
The example timing and byte count are illustrative, not benchmark results.

```json
{
  "article": {
    "url": "https://www.chosun.com/example/",
    "title": "Illustrative English lesson",
    "published_at": "2026-10-21T06:00:00+09:00",
    "section": "Education",
    "language": "ko"
  },
  "lesson_image": {
    "url": "https://images.chosun.com/example-lesson.png",
    "alt_text": null,
    "content_type": "image/png",
    "width": 1000,
    "height": 1500,
    "resized_urls": {

    },
    "source": "content_elements",
    "is_fallback": false,
    "content_element_path": [0],
    "byte_size": 54321,
    "format": "PNG",
    "download_url": "https://images.chosun.com/example-lesson.png"
  },
  "article_images": [
    {
      "url": "https://images.chosun.com/example-lesson.png",
      "alt_text": null,
      "content_type": null,
      "width": 1000,
      "height": 1500,
      "resized_urls": {

      },
      "source": "content_elements",
      "is_fallback": false,
      "content_element_path": [0],
      "byte_size": null,
      "format": null,
      "download_url": null
    }
  ],
  "ocr": {
    "raw_text": "Expression\nDon't sweat it.\n뜻\n걱정하지 마.",
    "korean_text": null,
    "english_text": null,
    "engine": "tesseract",
    "status": "ok",
    "mean_confidence": 0.97,
    "regions": [
      {"text": "Expression", "confidence": 0.97, "bounding_box": {"x": 40, "y": 30, "width": 120, "height": 24}, "low_confidence": false},
      {"text": "Don't", "confidence": 0.97, "bounding_box": {"x": 40, "y": 70, "width": 60, "height": 24}, "low_confidence": false},
      {"text": "sweat", "confidence": 0.97, "bounding_box": {"x": 110, "y": 70, "width": 60, "height": 24}, "low_confidence": false},
      {"text": "it.", "confidence": 0.97, "bounding_box": {"x": 180, "y": 70, "width": 30, "height": 24}, "low_confidence": false},
      {"text": "뜻", "confidence": 0.97, "bounding_box": {"x": 40, "y": 125, "width": 30, "height": 24}, "low_confidence": false},
      {"text": "걱정하지", "confidence": 0.97, "bounding_box": {"x": 40, "y": 165, "width": 90, "height": 24}, "low_confidence": false},
      {"text": "마.", "confidence": 0.97, "bounding_box": {"x": 140, "y": 165, "width": 30, "height": 24}, "low_confidence": false}
    ],
    "warnings": []
  },
  "lesson": {
    "status": "structured",
    "fields": {
      "expression": {"text": "Don't sweat it.", "region_indices": [1, 2, 3], "confidence": 0.97, "bounding_box": {"x": 40, "y": 70, "width": 170, "height": 24}},
      "korean_meaning": {"text": "걱정하지 마.", "region_indices": [5, 6], "confidence": 0.97, "bounding_box": {"x": 40, "y": 165, "width": 130, "height": 24}},
      "korean_explanation": null,
      "english_dialogue": null,
      "korean_dialogue_translation": null,
      "vocabulary_notes": null,
      "source_credit": null
    },
    "reasons": [],
    "unassigned_region_indices": [],
    "method": "labeled_layout_v1"
  },
  "processing": {
    "status": "completed",
    "completed_stages": ["url_validation", "article_fetch", "article_parse", "image_download", "ocr", "lesson_parse"],
    "ocr_status": "ok",
    "lesson_status": "structured",
    "elapsed_ms": 142.5,
    "warnings": []
  }
}
```

### Common error responses

All failures use `error.code`, a concise `error.message`, and a `details` list.
Only request validation normally populates `details`; an upstream article 404
becomes HTTP 502 because the API's upstream fetch failed.

HTTP **422**, malformed or unsupported article URL:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed. Check the indicated fields.",
    "details": [
      {
        "location": [
          "body",
          "article_url"
        ],
        "message": "Value error, article_url must be an absolute HTTPS URL",
        "type": "value_error"
      }
    ]
  }
}
```

HTTP **422**, no usable article image or OG fallback:

```json
{"error":{"code":"no_lesson_image","message":"No suitable article image or usable og:image fallback was found.","details":[]}}
```

HTTP **403**, publisher denied or rate-limited access (including upstream 429):

```json
{"error":{"code":"blocked_page","message":"The publisher denied or rate-limited article access.","details":[]}}
```

HTTP **502**, article host returned an error:

```json
{"error":{"code":"upstream_http_error","message":"The publisher returned HTTP 404.","details":[]}}
```

HTTP **503**, the default single-request slot is occupied:

```json
{"error":{"code":"scrape_busy","message":"The service is processing another article. Retry later.","details":[]}}
```

HTTP **503**, native libraries or Korean/English model files are unavailable:

```json
{"error":{"code":"ocr_unavailable","message":"OCR initialization failed. Check server libraries and Korean/English models.","details":[]}}
```

HTTP **504**, overall processing deadline:

```json
{"error":{"code":"scrape_timeout","message":"The scrape exceeded its processing deadline.","details":[]}}
```

Individual stages can instead return `article_timeout`, `image_timeout`, or
`ocr_recognition_failed`. HTTP 200 with `processing.status: partial` is also a
valid result: inspect `ocr.status`, `lesson.status`, and their warnings/reasons
before trusting the structured fields. OCR mistakes are not necessarily HTTP
errors.

## End-to-end endpoint behavior

The route delegates to `services/scrape.py`, which validates the URL, fetches
HTML, parses metadata/image candidates, downloads and validates the selected
image, runs the process-wide OCR service, and classifies lesson fields. Article
and lesson parsing run in worker threads; network calls are asynchronous.
Successful responses require typed `ocr`, `lesson`, and `processing` objects.

`processing.completed_stages` lists `url_validation`, `article_fetch`,
`article_parse`, `image_download`, `ocr`, and `lesson_parse`. A completed stage
means the step ran successfully, not that OCR found text or every field exists.
Overall `processing.status` is `partial` when OCR/lesson classification is
uncertain, warnings remain, or the selected image is an OG fallback. Raw OCR and
available metadata remain in HTTP 200 responses for these reviewable outcomes.

Failures stop downstream stages and return the existing concise `error` envelope:

| HTTP status | Meaning |
| --- | --- |
| 422 | Invalid URL, invalid article/image, no lesson image, or oversized decoded/OCR dimensions |
| 403 | Publisher access denied, rate-limited, or a recognized challenge page |
| 502 | Upstream HTTP/network failure, unsupported transfer encoding, redirect failure, or downloaded bytes exceeding the configured limit |
| 504 | Article/image timeout, native OCR failure/deadline, or overall `scrape_timeout` |
| 503 | `scrape_busy`, `ocr_busy`, or `ocr_unavailable`; retry after in-flight work finishes, or fix the OCR installation |
| 500 | Unexpected processing failure (`processing_failed`) or native OCR error (`ocr_failed`) |

The default one-request admission gate bounds active pipeline memory on Render
Free and rejects excess work before any outbound HTTP call. The OCR service
retains its independent lock even when request concurrency is increased. Locks
are released on normal completion and errors. Cancellation does not release the
native OCR lock until its worker actually finishes, preventing shared state
corruption when a timed-out HTTP request leaves native work briefly in flight.

Application logs contain fixed stage names and exception class names, never full
HTML, image buffers, OCR content, or arbitrary dependency exception messages.
No image bytes or local paths are serialized into the API response.

### Request frequency and retries

There is **no per-client/per-IP requests-per-minute rate limiter**. The default
`SCRAPE_MAX_CONCURRENT_REQUESTS=1` is process-wide admission control: excess
simultaneous requests get `503 scrape_busy`. The OCR engine also returns
`503 ocr_busy` if its own lock is occupied. There is no request queue, automatic
retry loop, or `Retry-After` header, and these controls do not establish any
publisher-approved crawling frequency.

Use the service for individual lessons: wait for one scrape to finish before
starting another, leave a short pause between requests, and avoid parallel/bulk
crawling. For busy responses or transient timeouts, use bounded increasing
backoff with jitter rather than rapid retries. A client timeout can leave bounded
native OCR work briefly in flight, so do not immediately issue a duplicate.
Stop on publisher `blocked_page`/`image_blocked` responses instead of trying to
bypass or repeatedly retry the access restriction. Fix invalid URLs/inputs before
retrying 422 responses. Public multi-user use needs an appropriate gateway quota
or application rate limiter in addition to the existing concurrency guard.

### Server-controlled limits

Set environment variables before starting the server; none are accepted from
request bodies. Article, image and pipeline settings are checked during startup.
Invalid values fail startup rather than silently disabling a limit. OCR config is
initialized once per process; restart after changing settings. Python callers can
also pass `ArticleFetchConfig` / `ImageDownloadConfig` directly to those services.

| Environment variable | Default |
| --- | --- |
| `SCRAPE_TIMEOUT_SECONDS` | 90 |
| `SCRAPE_MAX_CONCURRENT_REQUESTS` | 1 |
| `ARTICLE_MAX_BYTES` | 2097152 (2 MiB; may be lowered, cannot exceed the parser's hard cap) |
| `ARTICLE_CONNECT_TIMEOUT_SECONDS` | 5 |
| `ARTICLE_READ_TIMEOUT_SECONDS` | 10 |
| `ARTICLE_TOTAL_TIMEOUT_SECONDS` | 30 |
| `ARTICLE_MAX_REDIRECTS` | 3 |
| `IMAGE_MAX_BYTES` | 10485760 (10 MiB) |
| `IMAGE_MAX_PIXELS` | 16000000 |
| `IMAGE_MAX_DIMENSION` | 20000 |
| `IMAGE_CONNECT_TIMEOUT_SECONDS` | 5 |
| `IMAGE_READ_TIMEOUT_SECONDS` | 10 |
| `IMAGE_TOTAL_TIMEOUT_SECONDS` | 30 |
| `IMAGE_MAX_REDIRECTS` | 3 |
| `OCR_MAX_BYTES` | 10485760 (10 MiB) |
| `OCR_MAX_PIXELS` | 4000000 |
| `OCR_TIMEOUT_MS` | 30000 |
| `OCR_MIN_CONFIDENCE` | 0.5 |

The overall deadline is cooperative: a native OCR call or CPU worker cannot be
hard-killed safely from its Python request. Such work retains its lock until it
returns; native recognition also has its own deadline. Keep the default request
concurrency and one Uvicorn worker on Free. Increasing limits does not increase
Render resources and can cause memory exhaustion or CPU deadline failures.
Byte, pixel, parser, and OCR limits all apply; raising the downloader limit alone
does not raise the parser/OCR cap.

`tests/test_endpoint_pipeline.py` exercises the entire route using mocked DNS,
HTTP transports and OCR outputs, while retaining real URL/HTML/image validation,
metadata parsing, and lesson classification. It covers complete and partial
responses, stage failures, environment limits, safe logging, admission cleanup,
and two simultaneous requests against one shared mock OCR engine.

## Structured lesson fields

`POST /api/v1/scrape` adds a `lesson` object alongside the **unchanged** `ocr`
object. `ocr.raw_text`, `ocr.regions`, confidence scores, and original region order
remain available for correction/debugging even if parsing fails.

All seven entries in `lesson.fields` are optional and default to `null`:
`expression`, `korean_meaning`, `korean_explanation`, `english_dialogue`,
`korean_dialogue_translation`, `vocabulary_notes`, and `source_credit`.
A populated field contains:

```json
{
  "text": "Don't sweat it.",
  "region_indices": [1, 2, 3],
  "confidence": 0.97,
  "bounding_box": {"x": 40, "y": 65, "width": 142, "height": 28}
}
```

This is an illustrative field, not a screenshot extraction. Region indices refer
to the original `ocr.regions` array. Field confidence is the minimum supporting
region/heading confidence, not a calibrated classification probability. Inline
headings may share a region with field content; its original box/index is retained.

The pure `parse_lesson(ocr, image_width, image_height, config)` service:

1. Reconstructs lines using vertical overlap and limited horizontal word gaps.
   Large horizontal gaps remain separate so parallel columns are not interleaved.
2. Recognizes explicit English/Korean section labels such as `Expression`, `뜻`,
   `설명`, `대화`, `대화 해석`, `Vocabulary`, and `Source:`. Labels must be whole
   headings or colon-delimited prefixes, not incidental words in prose.
3. Uses heading alignment to infer columns, then assigns only nearby content
   below its heading and before the next label in that column. Large vertical
   gaps and unsupported heading/credit markers stop assignment. Ambiguous column
   ownership and repeated section labels cause conservative rejection.
4. Checks confidence, language and dialogue speaker evidence. English dialogue
   requires a speaker marker such as `A:`, `B:`, `A.`, or `B)`; following wrapped
   lines stay in that section. Korean meaning/translation and English expression
   cannot be inferred just from being adjacent. Mixed ambiguous sections are not
   split or completed automatically.

Straight/curly apostrophes, punctuation, Korean text, speaker labels, and line
breaks are retained. The parser does not translate, invent missing fields, repair
OCR spelling, or align English/Korean dialogue turns. Missing source credit is
left `null`. Raw OCR always remains the authority for manual correction.

| `lesson.status` | Meaning |
| --- | --- |
| `structured` | All observed supported sections were assigned reliably; this does **not** require every optional field to exist. |
| `partial` | Some fields were assigned; other regions or sections remain ambiguous. |
| `unparsed` | Reliable classification was unavailable; fields remain null and OCR remains available. |
| `empty` | Neither raw text nor OCR regions are available. |

`lesson.reasons` explains rejected/uncertain classification, and
`lesson.unassigned_region_indices` locates unresolved raw regions. These statuses
are successful HTTP 200 responses; they do not hide OCR or discard image metadata.

`LessonParserConfig` controls aliases, minimum content confidence (0.6), heading
confidence (0.75), line-overlap/gap thresholds, column separation, and a
5,000-region processing cap. Layout processing runs off the async event loop,
requires no model/service/storage, and adds no dependencies. Unknown unlabeled
layouts intentionally return `unparsed`; geometric position alone cannot prove
that a large English title is the target expression.

No lesson screenshots were attached or available in the repository during this
implementation. The three `lesson_ocr_*.json` fixtures are explicitly **synthetic**
single-column, two-column, and ambiguous layouts. They verify mechanics, not
accuracy on an actual publisher screenshot template. Add verified OCR exports
from the intended screenshots and tune aliases/gaps before claiming that coverage.

## Image selection

Only `type: image` entries in body `content_elements` and nested
`gallery`/`container`/`group` elements are considered. Promo and related-story
images are excluded. Original URLs and provided resize variants are retained;
the parser does not synthesize URLs or download image candidates. The service
downloads only the selected candidate. Available `additional_properties`
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

Image URLs are untrusted metadata. Being present in an article does not approve
a host for download; the downloader applies the explicit policy below.

## Structure

```text
app/
  main.py              # ASGI app and shared exception handlers
  api.py               # Thin HTTP routes
  schemas.py           # Request, article, image, OCR, and error contracts
  services/
    scrape.py          # Fetch/parse/download/OCR orchestration
    article.py         # Bounded async HTML fetching and article checks
    parsing.py         # Pure metadata parsing and configurable image selection
    images.py          # In-memory download, format and pixel validation
    ocr.py             # Replaceable engine interface and process-wide OCR service
    lesson.py          # Conservative layout-aware lesson post-processing
    daily.py           # Daily runner, retries and in-process timer
    saved.py           # Shared idempotent processing and persistence
    discovery.py       # Publisher-listed English-series links
    storage.py         # PostgreSQL history, queue, results and advisory lock
    urls.py            # HTTPS article-host policy
    errors.py          # Shared application errors
.github/workflows/daily-scrape.yml  # External daily wake-up for sleeping Free service
render.yaml                        # Free Docker service, scheduler disabled by default
scripts/trigger_daily.py           # Authenticated trigger and bounded status polling
tests/
  fixtures/            # Synthetic representative HTML samples
  test_api.py
  test_article.py
  test_parsing.py
  test_images.py
  test_ocr.py
  test_lesson.py
  test_daily.py
  test_daily_postgres.py
  test_saved_api.py
```

## Available checks

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
git diff --check
sh -n scripts/start.sh
```

No formatter or linter is configured: there is no Ruff/Black/Flake8/isort config,
pre-commit setup, or lint command. No formatting/lint success is claimed, and no
new tool was installed merely for this pass. `git diff --check` checks whitespace,
not Python formatting or lint rules. Render YAML syntax was also parsed locally
with Ruby's YAML parser; this is not validation by Render.


Tests use local fixtures and mocked DNS/HTTP transport. No tests access the live
publisher. Fixture provenance and covered page shapes are documented in
`tests/fixtures/README.md`. Pillow verifies downloaded image bytes; image tests
generate small test images in memory.

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

## Lesson-image downloads

`download_lesson_image(parsed.lesson_image)` returns a `DownloadedImage` containing
`data: bytes` and updated `metadata: LessonImageMetadata`, entirely in memory.
The HTTP API exposes metadata and OCR results, not raw image bytes or a local
file path. `article_images` retains the original unverified candidate metadata.
The downloaded bytes are passed directly to OCR and released after the request.

The initial URL comes from the parser-selected article image, including an
explicitly marked OG fallback. Only HTTPS on port 443 is allowed, with no embedded
credentials. The explicit approved hosts in `app/services/images.py` are:

- `images.chosun.com`
- `cloudfront-ap-northeast-1.images.arcpublishing.com`, restricted to `/chosun/`
  paths because the CDN is shared between publishers.

Article data cannot add new hosts to the allowlist. Extend it only after review;
there are no wildcard approvals for arbitrary Chosun subdomains or Arc CDN tenants.
Every redirect is revalidated, even on the same host, and every connection uses a
checked public IP with the original Host/TLS hostname. Proxy settings from the
environment are ignored. Unsafe destinations fail without retrying another image
or bypassing publisher access controls.

`ImageDownloadConfig` configures these defaults per service call:

| Setting | Default |
| --- | --- |
| `max_bytes` | 10 MiB, enforced against Content-Length and streamed bytes |
| `max_pixels` | 16,000,000 decoded pixels |
| `max_dimension` | 20,000 pixels per side |
| `connect_timeout` / `read_timeout` | 5 / 10 seconds |
| `total_timeout` | 30 seconds for DNS, redirects, and network transfer |
| `max_redirects` | 3 |

For example, pass `config=ImageDownloadConfig(max_bytes=5 * 1024 * 1024)`
to `download_lesson_image`. Limits are server-controlled, never accepted from
request bodies. The API uses these defaults unless overridden by the environment
settings below.

Only JPEG, PNG, WebP, and nonanimated GIF responses are supported. Content-Type
must agree with the detected format. Pillow verifies the file, then reopens and
fully decodes it; corrupt, truncated, animated, oversized, and mislabeled images
are rejected. Pillow decompression-bomb warnings become errors. HTTP compression
is rejected to avoid decompression expansion before the byte limit. Decoding runs
off the async event loop with one decode operation at a time; its duration is
separate from the network timeout. Byte and pixel limits remain necessary to bound
memory even when a file is small on the wire.

Image errors use the same `error` envelope:

| Status | Codes |
| --- | --- |
| 422 | `unsafe_image_url`, `unsafe_image_destination`, `invalid_image_content_type`, `invalid_image`, `image_dimensions_exceeded` |
| 403 | `image_blocked` |
| 502 | `image_http_error`, `image_network_error`, `image_redirect_error`, `image_too_large`, `image_encoding_error` |
| 504 | `image_timeout` |

## OCR engine comparison and choice

The repository uses async FastAPI handlers, Pillow, and bounded image buffers.
Render Free currently provides 512 MB RAM and 0.1 CPU, no GPU, and ephemeral
runtime storage. Models must be part of the build, not an assumed persistent
runtime cache. [Render compute plans](https://render.com/docs/compute-plans),
[Free service limitations](https://render.com/docs/free).

This is a deployment comparison, not an accuracy benchmark. Disk sizes exclude
frameworks, shared libraries, pixel buffers, and inference working memory. The
startup/RAM comparisons below are expectations from architecture; they are not
measurements on Render.

| Engine | Model size for a practical Korean/English setup | System/runtime requirements | CPU, RAM, startup and Free suitability |
| --- | --- | --- | --- |
| PaddleOCR | A documented PP-OCRv5 mobile option uses a 4.7 MB detector plus a 14 MB Korean recognizer that also supports English; newer/model variants differ. | PaddlePaddle native inference runtime, NumPy/OpenCV and platform-dependent shared libraries; no GPU required for CPU mode. Optional orientation/unwarping adds models. | Compact weights do not imply compact total RSS. Framework import, detector/recognizer initialization, and activations add CPU/RAM and cold-start work. Possible with careful tuning, but not the smallest deployment here and not validated in 512 MB. |
| EasyOCR | Published CRAFT ZIP is 73.7 MB plus `korean_g2` ZIP at 14.2 MB (about 88 MB compressed); extracted weights are larger. This excludes PyTorch and other dependencies. | PyTorch/torchvision, OpenCV-headless, NumPy/SciPy and other native wheels; CPU mode uses `gpu=False`. Pre-provision weights and disable downloads for deployment. | Two neural networks plus the PyTorch runtime add memory/import/model-load cost. CPU inference is supported, but a 512 MB service is a risky default without profiling. |
| **Tesseract (chosen)** | `tessdata_fast`: Korean about 1.6 MB + English about 3.92 MB (about 5.5 MB total language data). | Native Tesseract/Leptonica plus the small `tesserocr` binding; existing Pillow supplies in-memory pixels. No Torch/Paddle, CUDA, or detector checkpoint. | Smallest practical total dependency/model footprint for this repository; one CPU engine and modest initialization work. CPU time and layout accuracy still depend on image size. Most plausible Free baseline, not a measured performance guarantee. |

Sources: [PaddleOCR model tables](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html),
[EasyOCR model hub](https://jaided.ai/easyocr/modelhub/),
[CRAFT release assets](https://github.com/JaidedAI/EasyOCR/releases/expanded_assets/pre-v1.1.6),
[Korean G2 release assets](https://github.com/JaidedAI/EasyOCR/releases/expanded_assets/v1.3),
[EasyOCR dependencies](https://github.com/JaidedAI/EasyOCR/blob/master/requirements.txt),
[Tesseract Korean fast data](https://github.com/tesseract-ocr/tessdata_fast/blob/main/kor.traineddata),
[Tesseract English fast data](https://github.com/tesseract-ocr/tessdata_fast/blob/main/eng.traineddata),
[fast-model tradeoffs](https://tesseract-ocr.github.io/tessdoc/tessdata_fast.html).

Tesseract is selected for deployment size and operational simplicity, not a claim
of superior Korean recognition. Neural engines may handle complex graphics or
rotated text better. Evaluate representative lesson graphics before making an
accuracy claim. Keep original regions and confidence so uncertain text can be
reviewed. Confidence is a native engine score normalized to 0–1, not a calibrated
probability of correctness.

`tesserocr.PyTessBaseAPI` is used instead of a per-request CLI wrapper so both
language models are loaded **once per process**. The `OCREngine` protocol exposes
`recognize(bytes) -> OCRText` and `close()`; a replacement engine can be supplied
to `OCRService(factory=...)`. The Tesseract adapter accepts only verified raster
formats, passes Pillow pixel buffers in memory, and clears request images/results
while retaining its language models. It creates no image or TSV files.
[tesserocr API and reuse](https://github.com/sirfz/tesserocr).

## OCR lifecycle, limits and results

FastAPI startup validates server limits but does not initialize OCR language
models. The first scrape initializes the process-wide engine lazily under its
lock; subsequent requests reuse it. Initialization failures
are logged and cached, never retried or silently downgraded to English-only.
Scraping returns `503 ocr_unavailable`; restart after fixing libraries/model paths.
`/health` remains a liveness check, not a guarantee that OCR is available.
The binding is imported on the main ASGI thread because its `cysignals` dependency
registers signal handlers; model initialization and OCR run in worker threads.
Native resources are released at process exit.

OCR runs in a worker thread, with one engine call at a time. The default
whole-request admission limit rejects a competing scrape with `503 scrape_busy`
before fetching. If request concurrency is explicitly raised, competing OCR
calls still receive `503 ocr_busy` rather than sharing the engine concurrently. `OMP_THREAD_LIMIT=1`
limits native OpenMP threads in the supplied image. State is cleared between
images to avoid carrying recognition adaptation across requests.

| Setting | Default | Behavior |
| --- | --- | --- |
| `TESSDATA_PREFIX` | Auto-detect known system paths; explicit in Docker | Directory containing both `kor.traineddata` and `eng.traineddata` |
| `OCR_MAX_PIXELS` | 4,000,000 | Reject larger inputs with `422 ocr_image_too_large` before native OCR |
| `OCR_MIN_CONFIDENCE` | 0.5 | Flag uncertain regions and low average confidence |
| `OCR_TIMEOUT_MS` | 30,000 | Native cooperative recognition deadline |

The OCR byte cap defaults to 10 MiB via `OCR_MAX_BYTES` / `OCRConfig.max_bytes`. The OCR pixel limit is
intentionally lower than the downloader's 16-million-pixel decode limit: native
OCR needs additional image and segmentation buffers. Large lessons may be
rejected; increasing this limit without profiling can exhaust Free memory. The
service does not resize images silently, so returned boxes use original pixels.
The native deadline is cooperative and does not hard-kill the process; recognition
failure or deadline cancellation returns `504 ocr_recognition_failed`.

Low-confidence regions are retained and flagged. `status: low_confidence` means
the mean valid word confidence is below the threshold; individual low scores may
also appear with overall `status: ok`, accompanied by a warning. No valid words
returns HTTP 200 with `status: empty`, `raw_text: ""`, empty regions, null mean
confidence, and a warning. These outcomes preserve article/image metadata.
Malformed native output or unexpected engine errors return `500 ocr_failed`;
invalid OCR inputs return `422 ocr_invalid_image`.

Normalization tests use mocked TSV/API outputs and require no downloaded models.
They cover Korean/English words, boxes, confidence filtering, empty output,
initialization caching, request cleanup, and concurrent admission. A real local
macOS smoke check also recognized `안녕하세요 Hello English`
from a generated image on two successive calls through the same native engine.
This confirms bilingual engine wiring, not accuracy on publisher graphics. Render
peak RSS/latency and lesson accuracy still require deployment profiling.

## Render Free deployment

Deploy this repository as a **Docker Web Service**, not Render's native Python
runtime. The existing OCR binding needs system libraries and language files that
`pip install` alone cannot supply. `render.yaml` describes this same Docker
approach with `plan: free`, `/health`, and conservative environment settings. It
creates one web service with no disk, provisioned database, separate worker, or
GPU. Manual/scheduled persistence requires an external PostgreSQL URL. The
optional daily scheduler is disabled in the Blueprint until configured.

### Python, dependencies and system packages

The builder and runtime both use **Python 3.12 on Debian Bookworm** via
`python:3.12-slim-bookworm`. The minor-series image tag intentionally receives
Python/security patch updates; it is not an immutable image digest. All Python
runtime dependencies, including transitive dependencies, are pinned exactly in
`requirements.txt` (notably `tesserocr==2.11.0` and `pillow==13.0.0`). Development
dependencies remain separate. Debian packages track Bookworm security updates;
the build is not bit-for-bit reproducible against a frozen apt snapshot.

The multi-stage `Dockerfile`:

1. Installs **builder-only** `g++`, `pkg-config`, `libtesseract-dev`, and
   `libleptonica-dev`. It builds Python wheels, compiling `tesserocr` against the
   Debian libraries with `--no-binary=tesserocr`.
2. Installs **runtime** `libtesseract5`, `liblept5`, `tesseract-ocr-eng`, and
   `tesseract-ocr-kor` using `--no-install-recommends`. The language packages
   supply fast data under `/usr/share/tesseract-ocr/5/tessdata`.
3. Installs the wheels and copies the application/start script. A build-time
   assertion checks that both Korean and English models can initialize. A missing
   binding, library, or model fails the image build rather than downloading data
   on startup.
4. Runs as an unprivileged user. The compiler/headers remain in the builder stage.

No image or model is downloaded during service startup or from a health request.
Application requests download only the submitted article and selected lesson
image. OCR weights are immutable Docker image contents, not a writable cache.
Request HTML and image buffers stay in memory and need no persistent filesystem.
Daily-job OCR/results are stored in PostgreSQL when scheduling is enabled; the
manual endpoint does not automatically save results. Redeploys/restarts can discard all writable files safely.

### Start command and lightweight health

Docker runs `sh /app/scripts/start.sh`, which executes:

```sh
python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
```

Render supplies `PORT`; do not hard-code it in the dashboard. The 8000 fallback
is only for local runs. `exec` forwards shutdown signals to Python. There is no
`--reload`, Gunicorn, model-fetch command, or runtime package installation.
One worker and `SCRAPE_MAX_CONCURRENT_REQUESTS=1` are deliberate: memory use has
not been measured on Render, so multiple worker copies are not justified.

`GET /health` returns `200 {"status":"ok"}` without loading OCR language models,
performing network calls, decoding images, or acquiring the OCR lock. It remains
a **liveness** check even when OCR model files are absent. Importing the small
native binding still happens on the main thread for signal-handler compatibility;
model loading happens only when the first scrape reaches OCR. A first scrape can
therefore take longer, or return `503 ocr_unavailable` for a broken installation,
while health stays responsive.

### Dashboard setup: Blueprint (recommended)

1. Commit and push the deployment files to the repository branch you want Render
   to deploy. The configured remote is
   [Kimsuncheol/english_scrapper](https://github.com/Kimsuncheol/english_scrapper).
2. Open the [Render dashboard](https://dashboard.render.com/), select **New →
   Blueprint**, and connect your GitHub account/repository if prompted.
3. Select `english_scrapper` and the branch containing these changes. Use the
   root `render.yaml` as the Blueprint path.
4. Review the proposed `english-scrapper` service: runtime **Docker**, plan
   **Free**, Dockerfile `./Dockerfile`, context `.`, health path `/health`.
   Do not change to a paid instance or add a disk to make the example work.
5. Confirm the environment values below and configure a durable `DATABASE_URL`
   for manual scraping and saved results. For daily scraping, also complete the
   storage/wake-up setup above. No external database is provisioned automatically.
6. Follow the build/deploy logs. Once live, open the assigned `onrender.com` URL
   with `/health`; it should return `{"status":"ok"}`. Open `/docs` and submit a
   real approved Chosun article URL to verify the OCR pipeline separately.

The Blueprint omits a Docker command override so Render uses the image CMD.
[Render Blueprint reference](https://render.com/docs/blueprint-spec).

### Dashboard setup: manual Web Service alternative

Use this route **instead of** creating a Blueprint service to avoid duplicates.

1. Select **New → Web Service → Git Provider** and connect the same repository.
2. Set **Name** to `english-scrapper`; choose your deployment branch and an
   available region. Leave **Root Directory** blank (repository root).
3. Select **Docker** as the runtime/language. Use **Dockerfile Path**
   `./Dockerfile` and build context `.` if these fields are shown.
4. Select **Free** as the instance type. Leave **Docker Command** blank so the
   image's CMD runs. Do not enter a Python build command or pre-deploy command.
5. In **Environment**, review/add the table's six explicit values. They match
   Docker/application defaults, so no secret values need to be invented.
6. Set **Health Check Path** to `/health` under Advanced or the service's
   Settings after creation. Do not attach a persistent disk.
7. Select **Create Web Service**, watch build/deploy logs, then check `/health`
   and a separate real scrape as described above.

[Render Docker deployment](https://render.com/docs/docker),
[first-deploy instructions](https://render.com/docs/your-first-deploy).

### Required environment and safe defaults

The Blueprint sets the following explicitly. The same defaults are already in
the Docker image/application. Manual scraping and saved results additionally
require a durable `DATABASE_URL`; no OCR API key is needed. External daily
triggers also require `SCRAPE_TRIGGER_TOKEN` as described above.

| Variable | Value | Supplied by |
| --- | --- | --- |
| `DATABASE_URL` | Durable direct/session PostgreSQL URL | Set privately in Render Environment; required for scraping and saved results |
| `PORT` | Render-selected port | Render automatically; leave unset |
| `TESSDATA_PREFIX` | `/usr/share/tesseract-ocr/5/tessdata` | Docker image and Blueprint |
| `OMP_THREAD_LIMIT` | `1` | Docker image and Blueprint |
| `SCRAPE_MAX_CONCURRENT_REQUESTS` | `1` | Application default and Blueprint |
| `SCRAPE_TIMEOUT_SECONDS` | `90` | Application default and Blueprint |
| `OCR_MAX_PIXELS` | `4000000` | Application default and Blueprint |
| `OCR_TIMEOUT_MS` | `30000` | Application default and Blueprint |

Do not set `PYTHON_VERSION` expecting it to alter this deployment: the Docker base
image controls Python. Do not add `WEB_CONCURRENCY` or change the start command to
more than one worker without measured memory headroom. Optional article/image,
OCR confidence, byte and timeout overrides are listed in **Server-controlled
limits** above. Restart after changing settings; increasing limits does not add
Free resources.

### Sleep, cold starts and runtime limits

Render Free currently sleeps after **15 minutes without inbound traffic**. The
next request wakes it; Render documents spin-up as taking **about one minute**,
which is separate from application processing time. A client's first scrape can
therefore be slow even when the app is healthy. Clients should allow time for
wake-up, inspect HTTP status/content type before parsing JSON, and retry failed
wake-up attempts; a browser may initially see Render's loading page.
[Free-service sleep behavior](https://render.com/docs/free).

After wake-up, the first scrape also loads Korean/English models once into the
new process. Fast model files total roughly **5.5 MB** (Korean 1.6 MB, English
3.92 MB); this is disk size, **not** total inference RAM. Native library imports,
model initialization, image decoding and recognition add latency. A local
bilingual smoke test succeeded, but no trustworthy Render cold-start duration or
peak RSS has been measured. Do not interpret local timings as a Render estimate.

Free runtime resources are **512 MB RAM and 0.1 CPU**. Image/OCR intermediate
buffers can greatly exceed model file size. Defaults cap downloads at 10 MiB,
decoding at 16 million pixels, OCR at 4 million pixels, and admission at one
request. These are safeguards, not a memory-fit guarantee. Large or complex
lessons may be rejected or hit OCR/overall deadlines. Extra workers duplicate the
model/runtime footprint. [Render compute plans](https://render.com/docs/compute-plans).

Writable storage is ephemeral and Free services cannot attach persistent disks.
This application needs neither persistent image files nor runtime model caches.
Free runtime hours, bandwidth and pipeline usage also have workspace limits;
check the dashboard rather than assuming unlimited service availability.
[Free-service limitations](https://render.com/docs/free).

### Build limits and troubleshooting

The **build pipeline is separate from the Free web-service runtime**. Render
currently documents Starter pipeline workers as 2 CPU / 8 GB RAM, with a 16 GB
build disk limit and 120-minute build timeout. Hobby workspaces use this tier and
currently include 500 Starter pipeline minutes per month. These are build
resources, not RAM/CPU available to running OCR. Source-compiling `tesserocr`,
apt/pip network access and an uncached image build can consume several minutes;
there is no measured build-time promise here. Review billing/spend-limit settings
and remaining pipeline minutes before repeated rebuilds. This repository does
not request the paid Performance pipeline tier.
[Render build pipeline](https://render.com/docs/build-pipeline).

If building fails, inspect the package/compiler output: the multi-stage build
must use the same Debian/Python base in both stages and install the listed dev
headers before compiling. If the OCR initialization assertion fails, verify the
runtime packages and `TESSDATA_PREFIX`; do not compensate with startup model
downloads. At runtime, `ocr_unavailable` requires fixing/rebuilding the image or
its model path; `scrape_busy`/`ocr_busy` can be retried after active work completes.
Out-of-memory restarts or repeated deadlines require smaller inputs/lower limits
or separately hosted OCR; adding workers makes the Free memory problem worse.

### Local container verification

```sh
docker build -t english-scrapper .
docker run --rm -p 8000:8000 -e PORT=8000 english-scrapper
curl --fail http://127.0.0.1:8000/health
```

Docker is unavailable on the implementation host, so the Linux container and
actual Render deployment have not been built/run here. The start script was
smoke-tested locally with a custom `PORT` and nonexistent OCR model directory:
`/health` returned 200 without initializing OCR. The Blueprint YAML was parsed
locally, but has not been submitted to Render's Blueprint validator. The image
build includes its own native/model initialization assertion. Complete the
container build and a real lesson scrape before treating the service as verified.

## Render readiness and remaining limitations

Prepared locally:

- Docker build with pinned Python runtime packages, native OCR system libraries,
  and build-time Korean/English models; no runtime model downloads or persistent
  request files.
- Explicit Free Blueprint, `$PORT` binding on `0.0.0.0`, one worker, one default
  active scrape, bounded network/image/OCR resources, and lightweight `/health`.
- Complete typed API pipeline with raw OCR, optional lesson fields, processing
  status, concise errors, and tests using mocked HTTP/OCR.
- Exact Blueprint/manual dashboard steps and environment values above.

Still unverified or limited:

- **No actual Render deployment was performed or verified.** Docker is unavailable
  on this host, so the Linux container build also remains unverified. Local shell,
  test and health checks do not establish that a remote deployment succeeded.
- Real publisher article fetching/image-host coverage, real screenshot layout
  accuracy, Render build time, peak memory, and inference/cold-start latency need
  end-to-end checks. Parser fixtures are synthetic; the local bilingual OCR smoke
  image is not an accuracy benchmark for lesson screenshots.
- Free sleep/wake-up delays, limited CPU/RAM, cooperative timeouts and input caps
  can produce slow/rejected requests. `/health` reports liveness, not OCR readiness.
- Tall-image selection and labeled-layout classification are heuristics. OG images
  may be thumbnails; uncertain/unknown layouts return partial or unparsed results.
  No missing text, translation, or dialogue alignment is invented.
- The API has concurrency protection but no per-client request quota. OCR/model
  state is process-local and not shared between multiple workers or instances.
