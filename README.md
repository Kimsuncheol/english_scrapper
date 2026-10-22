# Chosun English Lesson API

FastAPI service that safely fetches Chosun.com articles and extracts article
metadata and lesson-image URLs, then downloads and validates the selected image
in memory. Tesseract recognizes Korean and English text and returns word regions
with confidence and pixel bounding boxes. A conservative layout parser organizes
supported labeled sections into lesson fields. No request files are written at runtime.
Manual and scheduled scraping use the same processing/persistence service.
Results are saved in Cloud Firestore and can be retrieved through the API.

The lesson graphic is a raster image: this service locates and downloads it,
then recognizes its pixels with OCR. It does **not** extract the graphic as
native/selectable text from the article HTML. Page metadata comes from HTML/page
data, while lesson text is an OCR estimate that can contain errors.

API reference: [API documentation](docs/API.md), [OpenAPI specification](docs/openapi.json),
and [synthetic response example](docs/examples/scrape-response.json).

## Saved article API

Configure `FIREBASE_PROJECT_ID` and server credentials even when `SCRAPE_ENABLED=false` if using manual
scraping. There is no ephemeral/local storage fallback. `/health` does not need
Firestore or OCR initialization.

| Endpoint | Response |
| --- | --- |
| `POST /api/v1/scrape` | Submit `{"article_url":"https://www.chosun.com/.../"}`; return the complete `ScrapeResponse` and a `Location: /api/v1/articles/{article_id}` header |
| `GET /api/v1/jobs/status` | Enabled state, schedule/timezone, `last_run`, `next_scheduled_at` (null when disabled), and `latest_run_result` with status, counts, error code and finish time |
| `GET /api/v1/articles?limit=20&offset=0` | Paginated `{items, total, limit, offset}`; items contain stable ID, canonical URL, article metadata, processing status and creation/update timestamps |
| `GET /api/v1/articles/{article_id}` | `{id, canonical_url, payload, created_at, updated_at}`; `payload` includes image metadata, OCR regions/raw text, structured lesson fields and processing status |

Manual and scheduled requests call the same fetch → parse → image download → OCR
→ lesson parsing pipeline and persistence service. Firestore transactional article leases stop
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
page's total and items are separate Firestore reads; concurrent inserts may
change the count or shift offsets between reads. No saved records returns `200` with an empty `items`
array and `total: 0`.

The existing `/jobs/daily`, `/lessons`, and `/lessons/{lesson_id}` routes remain
available for compatibility; `/lessons` retains its original array response.
Article/status reads and manual scraping currently have no per-user access
controls. The existing global scrape/OCR admission limits apply; this is not a
public multi-user rate limiter. Keep request frequency cautious and add access
controls before exposing an unrestricted OCR submission service.

## Optional daily scrape workflow

Before the daily workflow, the repository had no database or persistent storage.
Both manual and scheduled processing now use Cloud Firestore persistence;
there is deliberately **no SQLite/local-file fallback** on Render Free. Models
remain Docker image contents; request images are never saved to disk.

### Configuration

Set these environment variables before starting FastAPI (see `.env.example`,
which is documentation only; the app does not automatically load dotenv files):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPE_ENABLED` | `false` | Allow the protected daily job to run |
| `SCRAPE_IN_PROCESS` | `false` | Opt-in local timer; keep false on Render Free and use an external scheduler |
| `SCRAPE_TIMEZONE` | `Asia/Seoul` | IANA schedule timezone |
| `SCRAPE_HOUR` | `6` | Local hour, 0–23 |
| `SCRAPE_MINUTE` | `0` | Local minute, 0–59 |
| `FIREBASE_PROJECT_ID` | unset | Firebase project ID; required for scraping, saved results, and enabled scheduling |
| `FIRESTORE_DATABASE` | `(default)` | Firestore database ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | unset | Server-only service-account JSON path (or another ADC source) |
| `SCRAPE_TRIGGER_TOKEN` | unset | Secret Bearer token for external triggers |
| `SCRAPE_DAILY_MAX_ARTICLES` | `5` | Maximum queued articles attempted in one daily batch, supported range 1–20 |
| `SCRAPE_RETRY_ATTEMPTS` | `3` | Total attempts per transient operation, supported range 1–5 |
| `SCRAPE_RETRY_BASE_SECONDS` | `2` | Exponential backoff base; delays cap at 20 seconds plus up to 10% jitter |
| `SCRAPE_REQUEST_GAP_SECONDS` | `2` | Pause between article processing attempts/batches |

### Firebase / Cloud Firestore setup

This checkout is configured for Firebase project **`english-scrapper`**. The
Standard `(default)` database is in **Seoul (`asia-northeast3`)**. The backend
service account is `lesson-scraper@english-scrapper.iam.gserviceaccount.com`, with
`roles/datastore.user`. Its local key is in the ignored `secrets/` directory;
never commit or paste its contents into a chat. `.firebaserc` selects this project
for Firebase CLI commands. Other global Google Cloud project settings are unchanged.

Start the connected local test website with:

```sh
sh scripts/start-local.sh
# Open http://127.0.0.1:8765/_dev/test-site/
```

This local-only script sets the project, credential path, and development route
flag; scheduling remains off. It uses installed `.ocr-models` if present and
accepts environment overrides (`LOCAL_PORT`, `FIREBASE_PROJECT_ID`,
`GOOGLE_APPLICATION_CREDENTIALS`, `TESSDATA_PREFIX`, `ENABLE_TEST_SITE`).
Render uses its separate production start script. For Render, mount the same
service-account key privately as `/etc/secrets/service-account.json`; the Blueprint
already specifies the project ID and credential path. The secret itself is not
in Git or the container image.


1. In the [Firebase console](https://console.firebase.google.com/), select/create
   your project. Under **Build → Firestore Database**, create a **Standard edition**
   database in Native mode, ID `(default)`, in a suitable region. Use production
   security rules; the browser never accesses Firestore directly.
2. Set `FIREBASE_PROJECT_ID` to its project ID. `FIRESTORE_DATABASE` defaults to
   `(default)`. The Python backend uses the official async `google-cloud-firestore`
   server client; Firebase Auth, Hosting, Storage, and a browser Firebase API key
   are not required for this change.
3. Give a dedicated backend service account the **Cloud Datastore User** IAM role
   (`roles/datastore.user`) on this project. Supply Application Default Credentials
   (ADC). For local development, keep its JSON key outside the repository and set
   `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json`.
   Never paste the key into HTML, JavaScript, logs, or source control. The ignored
   `secrets/` directory is also excluded from Docker builds.
4. Using an authenticated Firebase CLI, deploy the included rules/indexes to your
   selected database: `firebase deploy --only firestore --project YOUR_PROJECT_ID`.
   This is an explicit setup action, not an application startup operation. The
   configuration targets `(default)`; for a different database, configure the CLI
   database target as well. Wait for indexes to finish building before live use.
   `firestore.rules` denies browser/mobile reads and writes. Server clients use
   IAM and bypass these rules, so keep the service account private.
5. On Render, add **Environment → Secret Files → Add Secret File**, name it
   `service-account.json`, and paste the JSON privately. Set
   `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/service-account.json`,
   `FIREBASE_PROJECT_ID`, and optionally `FIRESTORE_DATABASE` in Environment.
   Keep `FIRESTORE_EMULATOR_HOST` **unset** in production. The Docker image uses
   one Uvicorn worker and needs no local database, persistent disk, or paid worker.

No Firebase project/database, IAM grant, key, or hosted deployment is created by
this repository. Existing PostgreSQL records are not automatically migrated;
retain any old database/exports until a separate data migration is verified.
`DATABASE_URL` is no longer read. Request images remain in memory.

Firestore stores the complete OCR payload as a JSON string in `payload_json`,
then returns the original typed JSON through the API. This preserves nested OCR
arrays and avoids indexing raw OCR. `firestore.indexes.json` includes the queue
indexes and payload/article index exemptions; the app does not create indexes.
Results larger than 800,000 UTF-8 JSON bytes return `413 result_too_large` and
are recorded as failures, leaving room below Firestore's 1 MiB document limit.
No image bytes are saved in Firestore or Firebase Storage.

The default Firestore database has limited free storage and operation quotas;
transactions, retries, lock heartbeats, aggregation queries, and offset pagination
consume operations. Daily scraping is small by default, but this is not a promise
of zero cost. Deep offsets and unbounded public requests can increase usage;
monitor the Firebase usage dashboard. No paid TTL cleanup is required for locks.
See [Firestore limits](https://firebase.google.com/docs/firestore/quotas),
[server security](https://firebase.google.com/docs/firestore/security/overview),
and [index configuration](https://firebase.google.com/docs/firestore/query-data/index-overview).

### Discovery and retained results

The fixed discovery source is the publisher's
[Speaking English tag page](https://www.chosun.com/tag/daily-speaking-english/). It is fetched
once per daily run (with bounded retries) using the same HTTPS, public-IP pinning,
redirect, timeout and 2 MiB controls as article fetching. Links come from actual
HTML anchors or safely parsed Fusion `contentCache`/`globalContent` story objects.
Only titles beginning with `스피킹이 강해지는 영어` and approved article URLs under
`/national/nie/english/` are accepted. IDs/dates are never guessed and JavaScript is
never executed. A local live discovery check found 20 matching listed links;
this does not prove future coverage or that OCR worked on those articles.

Daily discovery reads the newest listing only. Initial enabling
can discover earlier listed lessons; five are processed per day by default, with
remaining URLs queued persistently. Unprocessed articles in the current publisher
listing are attempted in listing order before older queued URLs, so a newly listed
lesson is not held behind a historical backlog. If discovery fails, the stored
backlog remains available for retry. The separate historical archive indexer below reads all publisher pagination pages,
including older articles outside this newest listing. Empty discovery may mean no
matching entries; monitor the persisted `discovered` count if it stays zero.
Challenge/error pages are recorded as failures instead of bypassed.

The job creates documents lazily in these Firestore collections:

- `chosun_runs`: one local scheduled-date key, start/finish timestamps, attempts,
  status, counts and concise error code.
- `chosun_queue`: normalized article URL, pending/processing/failed/succeeded
  state, attempts, last-attempt timestamp, error code and saved-lesson ID.
- `chosun_lessons`: stable ID, unique canonical URL, complete `ScrapeResponse` JSON,
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

Every runner acquires a **Firestore transaction-backed lease** before discovery
or processing. `chosun_locks/daily` prevents overlapping daily runs, while
per-normalized-URL leases prevent simultaneous manual/scheduled work. Each lease
has a random owner token, a 120-second expiry, and a 30-second heartbeat. Every
session mutation checks ownership and expiry in its transaction; a stale owner
cannot save results or remove a successor's lock. A unique scheduled-date document
adds durable daily idempotency. Canonical results use deterministic document IDs;
a transaction atomically saves the first result and maps the submitted alias.

On process exit, normal cleanup releases owned leases. After a crash or sleeping
process, a new invocation can reclaim an expired lease; recovery may wait up to
120 seconds, then the external scheduler must retry. An expired owner receives
`503 storage_lock_lost`. All instances must use the same Firebase project/database
and schedule. Keep Render Free at one worker/instance for OCR memory limits.
Network/OCR work happens outside Firestore transactions; transaction retries never
repeat OCR. [Firestore transactions](https://firebase.google.com/docs/firestore/manage-data/transactions).

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
or OCR, but cannot create another canonical saved lesson. A lost or expired lease
blocks further writes on that session. No local filesystem lock is used.

The default deployment starts **no in-process timer**, even when
`SCRAPE_ENABLED=true`. An external scheduler owns the daily clock and invokes the
protected trigger. The job itself runs in the single FastAPI process only while
that batch is active. If it is interrupted, the next authenticated trigger can
resume the durable run under a new database lock. UTC timestamps are stored;
local dates define the daily key. A late trigger catches up today's run rather
than inventing a historical run for every missed date. Starting FastAPI or calling
`/health` does not start a job or initialize OCR. `/health` is liveness, not
database readiness.

### Render Free and external wake-up setup

Official Render documentation checked **2026-10-21**:

- Native Render Cron Jobs are paid, with a **$1/month minimum per cron service**;
  there is no Free cron plan. [Cron billing](https://render.com/docs/cronjobs#compute-plans-and-billing).
- Free web services sleep after 15 idle minutes, wake on an incoming request
  (typically about a minute), and may restart at any time. They do not support
  one-off jobs or persistent disks. Local filesystem changes are lost on restart,
  deploy or sleep. [Free web service limits](https://render.com/docs/free).
- Free Render Postgres expires after 30 days. Free Key Value loses data on
  restart, so neither is a long-term archive for this workflow without an
  appropriate durable storage plan. [Free datastore limits](https://render.com/docs/free).

Therefore the Free deployment uses the included **GitHub Actions daily workflow**
and Cloud Firestore, with no paid always-on worker or Render Cron resource.
The workflow invokes `scripts/trigger_daily.py` once per day at the default
`06:00 Asia/Seoul`. That client wakes the service, authenticates the trigger, and
polls the saved job state every 30 seconds during the active batch. It re-submits
the idempotent trigger while unfinished so a restarted FastAPI process can resume
without a local timer. It sends no keep-alive pings between daily batches.

1. Configure Firestore and server ADC credentials as above. In Render Environment
   set `FIREBASE_PROJECT_ID` and a strong `SCRAPE_TRIGGER_TOKEN`
   (for example generate one locally with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`).
2. Set Render `SCRAPE_ENABLED=true` and review timezone/hour/minute. The Blueprint
   deliberately defaults it to false. Keep **`SCRAPE_IN_PROCESS=false`**.
   Restart/redeploy so configuration is reloaded. Keep the existing one-worker
   command and resource limits.
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
   `/api/v1/jobs/status` and `/api/v1/articles` for persisted results. Confirm
   `enabled: true` and `in_process_enabled: false`. Before the
   due time the application returns `not_due`; the external script flags a
   mismatched/early wake-up instead of silently pretending a scrape occurred.

GitHub scheduling is best-effort: it can be delayed, and public-repository
schedules can be disabled after 60 days without activity. Review Actions status
and usage/billing for your repository plan. Runs can also be dropped during high
load; this Free design does **not guarantee exact-time or every-day execution**.
The workflow has a 35-minute ceiling; the client has a 30-minute total budget
including wake-up and retries, and reports partial/failed jobs as workflow
failures. Raising the five-article default can exceed Free CPU or this budget.
External scheduler/database provisioning has not been performed.
[GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[Render sleep behavior](https://render.com/docs/free).

#### Sleep, restart and retry behavior

If asleep at the scheduled time, Render receives the client's status request and
starts the web service. Cold-start loading pages, connection failures, and HTTP
502/503/504 are retried at most six times per API call, with 1/2/4/8/16-second
backoff and at most 60 seconds per request, within the overall 30-minute budget.
Redirects are rejected; the Bearer secret is sent only to the configured HTTPS
origin's trigger endpoint. Authentication failures (401/403) fail immediately.

Once awake, `POST /api/v1/jobs/daily/trigger` requires
`Authorization: Bearer $SCRAPE_TRIGGER_TOKEN`; an unset secret disables that
endpoint with 503, and a wrong/missing secret returns 401. The token is read from
environment variables, compared in constant time, never placed in a URL, and
never printed. Store it as a Render environment secret and GitHub Actions secret;
do not commit it, use shell tracing, or echo it in workflow logs. Rotate the value
in both places together.

While a run is active, repeated triggers return `already_running` or encounter the
database lock. After a restart, another trigger resumes `running`/`interrupted`
history; known saved articles skip OCR. Database recovery attempts inside the
server have a five-minute cooldown, and the external client continues bounded
polling/re-submission. Completed/partial/failed runs are terminal for that local
date. Their failed article URLs remain queued for the next day's bounded retries.
No HTTP 202 response alone proves a completed scrape: inspect persisted status.

If Render is suspended, the database remains unavailable, or the external retry
budget expires, Actions fails visibly. A batch may still be running when the
client times out; check status before rerunning the workflow. The stored queue
survives process loss, and a later daily run can process it. Requests do not wake
a service suspended for exhausted quotas. Missed URLs that rolled off the source
listing cannot be recovered by guessing article IDs.

Another trusted external scheduler can run this same standard-library script
once per day with `SCRAPE_BASE_URL` and `SCRAPE_TRIGGER_TOKEN` supplied privately.
The base URL must be the HTTPS service origin with no path, query, or credentials.
Keep the scheduler's local time/timezone aligned with the server's environment
values, and retain its retries/status monitoring. A single fire-and-forget POST
does not provide restart recovery. No Render API key is needed by this design.

### Local schedule and API usage

Configure a separate development Firebase project and server ADC credentials,
install `requirements.txt`, then export environment variables in the terminal
that starts Uvicorn (or use the loopback emulator described below):

```sh
export FIREBASE_PROJECT_ID='your-dev-project-id'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/service-account.json'
export SCRAPE_ENABLED=true
# Optional local convenience only; keep false for external scheduling/Render.
export SCRAPE_IN_PROCESS=true
export SCRAPE_TIMEZONE=Asia/Seoul
export SCRAPE_HOUR=6
export SCRAPE_MINUTE=0
# Set this privately if testing the trigger; do not commit the value.
export SCRAPE_TRIGGER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Keep this process running for the opt-in local timer. Starting after the due time can
immediately discover/process the rolling backlog. Leave `SCRAPE_ENABLED=false`
to disable scheduling during development. Manual processing still needs
`FIREBASE_PROJECT_ID` and valid credentials. Without them, `/health` and disabled job status work, while manual
scraping and saved-result routes return `503 storage_unavailable`.
Collections are written lazily on first use; indexes/rules must be deployed beforehand.

```sh
curl --fail http://127.0.0.1:8000/api/v1/jobs/daily
curl --include --request POST http://127.0.0.1:8000/api/v1/jobs/daily/trigger \
  --header "Authorization: Bearer $SCRAPE_TRIGGER_TOKEN"
curl --fail 'http://127.0.0.1:8000/api/v1/lessons?limit=20&offset=0'
# Replace LESSON_ID with an ID from the list response.
curl --fail http://127.0.0.1:8000/api/v1/lessons/LESSON_ID
```

`GET /api/v1/jobs/daily` exposes schedule configuration, `in_process_enabled`, next scheduled time,
last-run start/finish/status/counts/error, and up to 20 failed URLs with attempt
counts and timestamps. No token or database credentials are returned. Triggering
requires the Bearer token; saved lesson/status reads currently have no per-user
access controls, appropriate only when these public-article results are intended
to be readable by service users. The trigger respects enabled/due/daily-lock rules;
it cannot force a second completed run on the same date.

To test the deployment mode locally, leave `SCRAPE_IN_PROCESS=false` and call the
authenticated trigger after the due time instead. Run the external client against
your HTTPS deployed service from a trusted machine as follows (the token must
already be set privately in the environment):

```sh
export SCRAPE_BASE_URL='https://YOUR-SERVICE.onrender.com'
.venv/bin/python scripts/trigger_daily.py
```

### Tests for daily processing

```sh
# Unit/API tests do not need Firebase credentials or network access.
env -u FIREBASE_PROJECT_ID -u FIRESTORE_EMULATOR_HOST .venv/bin/python -m pytest -q
# Emulator integration: install Firebase CLI and its supported Java runtime first.
# Terminal 1 (demo project; no hosted Firebase writes):
firebase emulators:start --only firestore --project demo-english-scrapper
# Terminal 2:
FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 \
  .venv/bin/python -m pytest tests/test_daily_firestore.py -q
```

Integration tests use only a loopback emulator with project `demo-english-scrapper`
and a fresh collection prefix per test. No existing data is deleted. They check
real SDK transactions, concurrent manual/scheduled exclusion, canonical uniqueness,
expired-owner fencing, restart recovery, failure records, and API persistence.
Without `FIRESTORE_EMULATOR_HOST`, emulator tests skip. The emulator does not enforce
production composite indexes; deploy the included indexes and verify on your own
Firebase project before deployment. No hosted Firebase or Render deployment is
claimed by these checks.

To run the test-site URL form with the emulator, leave it running and start:

```sh
ENABLE_TEST_SITE=true SCRAPE_ENABLED=false \
FIREBASE_PROJECT_ID=demo-english-scrapper FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 \
TESSDATA_PREFIX="$PWD/.ocr-models" \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Use the model path appropriate to your OCR installation. This form runs real
publisher fetching and OCR; fixture browsing and mocked tests stay offline.
Emulator data is temporary unless you explicitly export/import it; production
records must live in hosted Firestore, never the emulator or Render filesystem.
See the [Firestore emulator guide](https://firebase.google.com/docs/emulator-suite/connect_firestore).

External-client tests use mocked HTTP and a simulated clock to cover loading
pages, transient failures, authentication/redirect rejection, total retry budget,
and restart recovery without sleeping or calling Render.

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
# Use a separate development Firebase project and server credentials.
export FIREBASE_PROJECT_ID='your-dev-project-id'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/service-account.json'
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

## Local fixture test website

Choose the workflow you need:

| Goal | Instructions |
| --- | --- |
| Browse fixture articles and parser output | Start the development server below with `ENABLE_TEST_SITE=true`, then open the fixture index |
| Exercise the complete fixture scrape with mocked OCR | Run the [end-to-end fixture test](#end-to-end-fixture-pipeline-test); no listening server is needed |
| Test the production API with development routes disabled | Use the [production test commands](#production-safeguards-and-test-commands) with `ENABLE_TEST_SITE=false` |

The existing FastAPI app includes an opt-in, offline parser playground. Set
**`ENABLE_TEST_SITE=true` before starting the app** to register its routes.
The default is false, and `.env.example` is documentation only: dotenv files are
not automatically loaded. The site serves four synthetic article HTML files and
local PNG images from `app/dev_site/`:

| Fixture | Expected parser result |
| --- | --- |
| `one-tall-image` | Select the 720 × 1800 lesson graphic |
| `thumbnail-and-tall` | Select the tall article image rather than the OG thumbnail |
| `no-lesson-image` | Retain article metadata and show `no_lesson_image` |
| `malformed-data` | Safely reject unusable malformed JSON-LD/Fusion data with `invalid_article_data` |

Start it from the repository root using the existing virtual environment:

```sh
ENABLE_TEST_SITE=true SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open [the local fixture index](http://127.0.0.1:8000/_dev/test-site/) in a browser.
The **Scrape a Chosun article** form at the top accepts a live HTTPS Chosun URL
and calls `POST /api/v1/scrape` on the same server. It displays the expression
when reliably classified, processing status, and the full JSON response (including
raw OCR), or the API error. Submitting this form performs real external fetching
and OCR; the four fixture links remain offline. Configure `FIREBASE_PROJECT_ID` and server credentials for
Cloud Firestore and install the Korean/English OCR models as described
above before submitting. For locally provisioned models, also set
`TESSDATA_PREFIX="$PWD/.ocr-models"` when starting the server. Duplicate URLs use
the existing saved result. The form does not enable local URL fetching or bypass
API host validation. Keep `ENABLE_TEST_SITE=false` in production.

It visibly links to all four pages:

- [One tall lesson image](http://127.0.0.1:8000/_dev/test-site/articles/one-tall-image)
- [Thumbnail plus tall lesson image](http://127.0.0.1:8000/_dev/test-site/articles/thumbnail-and-tall)
- [No lesson image](http://127.0.0.1:8000/_dev/test-site/articles/no-lesson-image)
- [Malformed article data](http://127.0.0.1:8000/_dev/test-site/articles/malformed-data)

Each page shows the fixture beside output from the **existing pure article
parser**: title, canonical URL, published date, section, language, image candidates,
selection, resized URLs and any parsing error. The image-selection result links
to its local PNG file. The malformed page also shows zero parsed JSON-LD documents
and no usable Fusion data, while retaining title/source fallbacks.

### Check for new expressions on the test website

The homepage shows **Speaking English lessons by day** with the full saved
lesson, not just its heading. Each day displays every available structured field
(Korean meaning/explanation, English dialogue, Korean translation, vocabulary,
and source credit), the **complete raw OCR text without truncation**, processing
status and parser notes, and expandable raw OCR regions/confidence/bounding boxes.
Unclassified fields are explicitly marked; the page never invents missing text or
silently treats OCR as fully reliable. Links open the original lesson image and
saved OCR JSON for review; publisher images are not copied into fixtures or loaded
automatically.

**Lesson date** comes from the actual article URL's year/month/day path, while
**Published** retains the original metadata timestamp (which can be the previous
evening). Invalid/missing URL dates remain unspecified. The date selector switches
between loaded days. **Load more saved days** paginates stored English article records, five
at a time, without the previous ten-lesson/twenty-record cutoff. A Firestore
canonical-URL range query orders the real padded year/month/day paths descending,
so importing an old lesson does not displace today from the first page; the
ordinary article API retains its original creation-time ordering. No new
Firestore composite index or data migration is required. Pages containing
non-series records still expose continuation so later matching days are reachable.
Dates are sorted among loaded results; concurrent inserts can shift offset pages,
so the browser deduplicates by saved article ID. Refresh resets to page one.

Opening the homepage reads saved data only. Individual synthetic fixture pages
continue to work without Firestore, external HTTP, or OCR.

Press **Check for new expressions** to fetch the fixed publisher tag page:
[스피킹이 강해지는 영어](https://www.chosun.com/tag/daily-speaking-english/).
The service reads safely parsed links/Fusion data, takes the first matching story
in publisher order, and calls the same idempotent scrape/save pipeline used by
manual submissions and daily jobs. An unknown latest article runs real image OCR,
is saved in Firestore, and appears on the page. A known latest article is returned
without another image download or OCR run. **Refresh saved expressions** reloads
the cards without contacting Chosun, including results saved by a daily job.
Manual URL submissions refresh these lessons too. The check also lists dates
currently present in the publisher's newest tag feed. Choose one under **Quick selection from the newest publisher page** and press **Load selected day** to OCR/save an unsaved
day or retrieve its existing full result. The selected day is retained in the UI
even when it is an older cached result outside page one. A selected day uses the
same URL validation, locking and persistence pipeline as a manual submission.

“New” means **new to this scraper**, not proof that an article was published since
your previous visit. The first check can import an existing latest article. A check
processes only the newest listed lesson. Other listed days can be loaded
individually; the complete historical catalog is available through the archive
browser described below. Catalog indexing and full image OCR are separate:
indexing all titles does not claim all lesson images have already been OCR-processed.
Empty listings and upstream errors are shown explicitly rather than called “up to date”.
Raw OCR is preserved even when an expression could not be classified reliably.

Successful discovery is cached for 60 seconds **per process**; the UI shows the
actual source-check time and whether the cache was used. Errors are not cached;
failed OCR remains retryable using the shared pipeline. Concurrent checks receive
`503 series_check_busy`, while Firestore's article lease protects concurrent work
across processes. This cache is not a public rate limiter; use cautious frequency.
There is no browser polling or new in-process timer. Guaranteed unattended checks
still require the external scheduling setup above; Render Free can sleep.

The following routes exist only with `ENABLE_TEST_SITE=true` and remain absent
from OpenAPI and production by default:

```sh
BASE_URL=http://127.0.0.1:8765
# Firestore reads only:
curl --fail "$BASE_URL/_dev/test-site/series/saved"
# Next page of full saved lessons:
curl --fail "$BASE_URL/_dev/test-site/series/saved?limit=5&offset=5"
# Explicit publisher check; may fetch an image, run OCR, and save:
curl --fail --request POST "$BASE_URL/_dev/test-site/series/check"
# Deterministic mocked discovery/OCR tests:
.venv/bin/python -m pytest tests/test_series.py -q
```

### Curl examples: fixture pages and scrape URL validation

With the development server above running, use a second terminal:

```sh
BASE_URL=http://127.0.0.1:8000

# These GETs return local HTML with visible parser/debug output.
curl --fail --show-error "$BASE_URL/_dev/test-site/"
curl --fail --show-error "$BASE_URL/_dev/test-site/articles/thumbnail-and-tall"
curl --fail --show-error "$BASE_URL/_dev/test-site/articles/no-lesson-image"
curl --fail --show-error "$BASE_URL/_dev/test-site/articles/malformed-data"

# Check that the original local PNG is served, without printing binary bytes.
curl --fail --show-error --output /dev/null \
  --write-out '%{http_code} %{content_type}\n' \
  "$BASE_URL/_dev/test-site/assets/lesson.png"
# Expected: 200 image/png

# Exercise the public scrape endpoint against every local fixture URL.
# Every POST intentionally returns HTTP 422 with error.code=validation_error.
for fixture in one-tall-image thumbnail-and-tall no-lesson-image malformed-data; do
  curl --include --show-error \
    --request POST "$BASE_URL/api/v1/scrape" \
    --header 'Content-Type: application/json' \
    --data "{\"article_url\":\"$BASE_URL/_dev/test-site/articles/$fixture\"}" \
    --write-out '\n'
done
```

Enabling fixture routes **does not permit localhost scraping**. These POSTs test
URL rejection, so even the missing/malformed fixtures return `validation_error`
before fetching. A normal server cannot return a successful fixture scrape from
curl. Use the end-to-end pytest command below for a 200 pipeline response and for
the distinct `no_lesson_image` / `invalid_article_data` processing errors.

Do not substitute the fixtures' synthetic `https://www.chosun.com/test-fixtures/…`
canonical URLs in curl: outside pytest they are treated as real publisher URLs,
not redirected to the local fixture server.

To check hostname rejection separately from the HTTP-scheme check, use this
single request. The API receives the POST over local HTTP, but the submitted
article URL uses HTTPS and is still rejected because its hostname is localhost:

```sh
curl --include --show-error \
  --request POST http://127.0.0.1:8000/api/v1/scrape \
  --header 'Content-Type: application/json' \
  --data '{"article_url":"https://localhost/_dev/test-site/articles/thumbnail-and-tall"}'
# Expected: HTTP 422; error.code is "validation_error".
```

URL validation happens before any fixture fetching or OCR. There is no HTTPS
connection to the submitted localhost URL, and no database configuration is
needed for these rejected requests. Browsing the same fixture directly with GET
returns HTTP 200 HTML; it does not submit it to the scrape pipeline.

No Chosun requests, database access, OCR initialization or runtime file generation
are needed to browse this site. The Chosun-shaped canonical URLs and `example.test`
image URLs inside metadata are inert synthetic identifiers used by the parser;
browser image/style requests stay on this local app. Embedded fixture scripts
are never executed by the parser and the browser CSP blocks scripts/external
resources. These fixtures are not publisher captures or OCR accuracy benchmarks.

This is a parser/selection playground, not an end-to-end local HTTP scraper:
`POST /api/v1/scrape` still rejects localhost and private addresses. Network,
download, OCR and persistence integration remain covered by the existing mocked
pipeline tests. The test site never weakens production SSRF restrictions.

`ENABLE_TEST_SITE` defaults to **false**. Routes/assets are only registered when
it is explicitly `true` before app creation; otherwise the entire
`/_dev/test-site/` tree returns 404. Leave it unset/false on Render and restart the
process after changing it. No test/debug routes appear in the OpenAPI schema.
`.env.example` documents this flag but is not auto-loaded. Run focused checks with
`.venv/bin/python -m pytest tests/test_dev_site.py -q`.

### End-to-end fixture pipeline test

From the repository root, run:

```sh
ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m pytest tests/test_local_fixture_e2e.py -q

# Optional focused checks: successful full responses or article-data errors.
.venv/bin/python -m pytest tests/test_local_fixture_e2e.py \
  -k local_site_drives_complete_real_pipeline -q
.venv/bin/python -m pytest tests/test_local_fixture_e2e.py \
  -k fixture_failures_stop_before_download -q
```

The test explicitly enables an isolated fixture app, while its public API app has
test routes disabled. There is no need to enable a running website first. It exercises
the public scrape API, actual fixture-site HTML routes, article fetching/parsing,
the original PNG file, real MIME/byte decoding, mocked OCR, six-field lesson
post-processing and saved-response retrieval. It requires neither a running
Uvicorn server nor Firestore credentials nor OCR language models; persistence uses the
existing test double. The OCR text is controlled synthetic output, not an OCR
accuracy assertion about the image pixels.

Only the pytest harness installs a fail-closed HTTPX transport. Logical approved
article URLs map to the fixture application's in-process local ASGI routes; the
fixture image metadata namespace is approved only inside that test's monkeypatch
scope and maps to local PNG routes. It uses the existing fixture files unchanged,
never resolves or requests the publisher. DNS, network socket connections, default
HTTP transports and real OCR initialization are forbidden, with a nonexistent
model directory configured to catch accidental dependencies. Unlisted hosts,
paths and requests have no network fallback.

There is **no production localhost allowance** and no new local-scrape endpoint
or environment switch. Even during the test, submitting a localhost/private URL
to `/api/v1/scrape` returns 422 before fetching; a production app has no fixture
routes. Missing images return `422 no_lesson_image`. Unusable malformed JSON-LD
with no valid page data or selected/fallback image returns
`422 invalid_article_data`. Recoverable malformed optional metadata still permits
a valid article image or OG fallback.

### Mocked OCR versus real OCR

| How you run it | What happens |
| --- | --- |
| Browse/curl the fixture HTML pages | The pure article parser displays metadata and selected-image information. OCR is not invoked. |
| Run `tests/test_local_fixture_e2e.py` | Real fixture HTML/PNG routes and image validation run. A mocked OCR service receives the validated PNG bytes and returns the fixed original text/boxes in `tests/fixtures/lesson_ocr_original_six_sections.json`. Results use an in-memory persistence double. |
| Submit an approved real article to the ordinary `/api/v1/scrape` endpoint | Real Korean/English Tesseract OCR runs on the downloaded image; configured language files and configured Firestore credentials are required. |

Mocked OCR has deterministic text, confidence and bounding boxes, even if the
fixture image's visible words differ. It tests pipeline wiring, reading order and
field assignment, not recognition accuracy. It never initializes Tesseract or
downloads models. Real OCR can misread text, vary in confidence, and return partial
or empty results; it must be checked separately with provisioned models. There is
no runtime `MOCK_OCR` flag or curl-accessible mock-OCR endpoint.

### Production safeguards and test commands

Keep `ENABLE_TEST_SITE` **unset or false in production**, including Render.
Registration happens at app creation; restart/redeploy after changing it. A
production process should return 404 for `/_dev/test-site/` and its assets. Local
URL fetching must remain forbidden regardless of that flag. The transport and
extra fixture-image namespace exist only in pytest; do not copy them into the
deployed app or broaden its host/IP allowlists to make fixture curl requests pass.

For a local smoke check of production route visibility, stop the development
server, then restart on the same port without `--reload` and with test routes
explicitly disabled:

```sh
ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
curl --fail --show-error http://127.0.0.1:8000/health
# Expected: {"status":"ok"}
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  http://127.0.0.1:8000/_dev/test-site/
# Expected: 404
```

This check needs neither database connectivity nor OCR models. It verifies the
local app configuration, not a Render deployment.

Run the full suite with production defaults and without inheriting live database
credentials:

```sh
env -u FIREBASE_PROJECT_ID -u FIRESTORE_EMULATOR_HOST \
  ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m pytest -q
```

This does not start a listening website. Tests that verify development features
temporarily opt in within isolated app instances; the production API remains
covered with local URLs rejected. Firestore integration tests skip without
`FIRESTORE_EMULATOR_HOST`; enable those only against a loopback emulator as
described in [daily processing tests](#tests-for-daily-processing).

To run only the non-website tests, exclude the two fixture-site test modules:

```sh
env -u FIREBASE_PROJECT_ID -u FIRESTORE_EMULATOR_HOST \
  ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m pytest -q \
    --ignore=tests/test_dev_site.py --ignore=tests/test_local_fixture_e2e.py
```

These test commands do not stop or reconfigure a server already running in
another terminal. Stop that server with Ctrl+C before restarting it without
`ENABLE_TEST_SITE=true` to check production route visibility.

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

The pure `parse_lesson(ocr, image_width, image_height, config, article_title=...)` service:

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

For an unlabeled image in the `스피킹이 강해지는 영어` series, there is one narrow
additional rule: a centered English heading in the top 20% of the image may fill
`expression` only when its OCR words match the series article title. Its median
word height must be at least 1.25 times the image's median OCR word height, word
gaps must be bounded, and all supporting words must meet heading confidence.
Duplicate matching headings are ambiguous. Typographic apostrophes are equivalent
for comparison, but the returned text and boxes always come from raw OCR. Missing
or incorrect OCR words are never filled in from metadata. This rule returns
`method: series_title_verified_v1` and `status: partial`; other unlabeled fields
remain null. The size/band thresholds are configurable in `LessonParserConfig`.

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

The original single-column, two-column and ambiguous OCR fixtures are synthetic.
`lesson_ocr_original_six_sections.json` adds a wholly original six-section lesson
with shuffled mock OCR regions, pixel boxes, explicit expected reading order,
line grouping and field assignments. Run
`.venv/bin/python -m pytest tests/test_synthetic_lesson.py -q` to check it without
initializing OCR or making HTTP requests; see `tests/fixtures/README.md` for its
format and provenance.
The additional `lesson_ocr_series_heading.json` contains five observed title
regions from the supplied real article plus synthetic surrounding context. It
tests the title corroboration rule without storing the full publisher image/text.

### Verified October 21 article

The [supplied October 21, 2026 article](https://www.chosun.com/national/nie/english/2026/10/21/I2PQNZFBDZHH7PTL7LILUZRDHM/)
was checked with live discovery, safe fetching/downloading and real Korean/English
Tesseract OCR. The 650 × 1444 original lesson image produced
`lesson.fields.expression.text: "back to one’s old self"`. The overall result is
still partial because other unlabeled text has OCR uncertainty and remains raw.
This is one verified example, not an accuracy claim for all publisher layouts.

The URL's issue date is October 21; the publisher's metadata timestamp is
`2026-10-20T12:54:18.732Z`. The service preserves that timestamp and does not exclude
the article from an October 21 run because its publication timestamp is earlier.
No article ID, date or expression is hardcoded into production selection logic.
Tests for this case use local/mocked fixtures rather than repeatedly calling
Chosun. The one live verification does not prove a Render daily run has occurred.

Local verification provisioned the official small `tessdata_fast` Korean/English
files once under ignored `.ocr-models/` and set
`TESSDATA_PREFIX="$PWD/.ocr-models"` before starting Python. This is optional local
setup, not a runtime model downloader. Render continues to use its build-time
language packages. Actual unattended runs still need the Firebase project/credentials, enabled
schedule, trigger secret and external scheduler described above.

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
  dev_site/            # Opt-in local HTML/PNG fixtures; absent from production routes
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
    storage.py         # Firestore history, queue, results and transactional leases
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
  test_dev_site.py
  test_local_fixture_e2e.py
  test_synthetic_lesson.py
  test_daily.py
  test_daily_firestore.py
  test_saved_api.py
  test_trigger_daily.py
```

## Available checks

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
env -u FIREBASE_PROJECT_ID -u FIRESTORE_EMULATOR_HOST \
  ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
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
| 422 | `validation_error`, `unsafe_url`, `unsafe_destination`, `invalid_article`, `invalid_article_data`, `no_lesson_image` |
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
GPU. Manual/scheduled persistence requires external Firestore credentials. The
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
Manual and daily OCR/results are stored in Firestore. Redeploys/restarts can
discard writable request files safely; Firestore remains the source of saved records.

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
5. Confirm the environment values below and configure `FIREBASE_PROJECT_ID` and server ADC credentials
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
5. In **Environment**, review the defaults below, set `FIREBASE_PROJECT_ID`,
   and mount the server credential secret file as described in Firebase setup.
   Configure `SCRAPE_TRIGGER_TOKEN` separately if enabling daily triggers.
6. Set **Health Check Path** to `/health` under Advanced or the service's
   Settings after creation. Do not attach a persistent disk.
7. Select **Create Web Service**, watch build/deploy logs, then check `/health`
   and a separate real scrape as described above.

[Render Docker deployment](https://render.com/docs/docker),
[first-deploy instructions](https://render.com/docs/your-first-deploy).

### Required environment and safe defaults

The Blueprint sets the following explicitly. The same defaults are already in
the Docker image/application. Manual scraping and saved results additionally
require `FIREBASE_PROJECT_ID` and server ADC credentials; no OCR API key is needed. External daily
triggers also require `SCRAPE_TRIGGER_TOKEN` as described above.

| Variable | Value | Supplied by |
| --- | --- | --- |
| `FIREBASE_PROJECT_ID` | Your Firebase project ID | Render Environment |
| `FIRESTORE_DATABASE` | `(default)` | Optional Render Environment override |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/etc/secrets/service-account.json` | Path to privately mounted Render secret file |
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


### Complete historical expression archive

**All daily expressions · January 2021 onwards** lists the publisher's historical
series entries, including unsaved lessons. Use the numbered page buttons, **Previous**, or **Next** to browse
**10 lesson days per page**. The first and last pages (for example **143**) remain
accessible directly; the final page can contain fewer than 10 days. Every row shows the issue date and the expression from the
publisher's title. **Open full lesson** calls the same safe, idempotent image/OCR
pipeline as manual scraping and displays the full saved lesson below the archive.
An unsaved day takes longer; a saved day reuses its persisted OCR result.
Titles are explicitly labeled as publisher metadata, independently of OCR
classification confidence. Missing dates are not invented: only publisher-listed
articles appear, rather than one fabricated entry for every calendar day.

The Chosun page's `?page=143` URL **does not paginate the server-rendered HTML**;
it can still contain page-one data. The publisher's own pagination JavaScript
uses the public `/pf/api/v3/content/fetch/story-feed` endpoint with the tag slug,
`size=10`, and an `offset`. The indexer reads that JSON through the existing
bounded HTTPS fetcher (public DNS pinning, redirects revalidated, connect/read/
total timeouts, 2 MiB cap). It never executes JavaScript or guesses article URLs.
On October 21, 2026 the publisher reported **1,425 lessons across 143 pages**;
page 143 includes **January 1, 2021**. Some historical entries use
`/national/nie/YYYY/MM/DD/` instead of `/national/nie/english/YYYY/MM/DD/`;
both observed paths are retained and sorted by issue date.

Populate or refresh the full catalog from a trusted local terminal, with the
same server-side Firestore credentials used by the application:

```sh
export FIREBASE_PROJECT_ID=english-scrapper
export GOOGLE_APPLICATION_CREDENTIALS="$PWD/secrets/english-scrapper-service-account.json"
.venv/bin/python -m scripts.index_archive
# After an interrupted initial import, skip pages already committed:
.venv/bin/python -m scripts.index_archive --resume
```

The default command refreshes **every page**. Use `--resume` only to finish an
interrupted import: offsets shift when new articles are published, so a later
full refresh should not skip earlier pages. Each source page commits atomically into
`chosun_archive` (canonical-URL keys) and `chosun_archive_state` (page checkpoints,
source count, last index timestamp). After the full import, an atomic transaction
publishes metadata page snapshots in `chosun_archive_pages`; the UI never shows
a partly rebuilt snapshot as a complete catalog. Replays are idempotent; older entries are
retained. A renewable database lease prevents concurrent archive imports across
instances. Calls are sequential with a one-second gap; transient network/5xx
failures retry at most three attempts with exponential delays. Blocked responses
stop immediately. Successful pages survive an interruption and filesystem loss.
The progress counts remain visible on the site even after a partial import.

The indexer runs outside the sleeping Render Free web service. It needs no paid
worker, runtime model downloads, or persistent disk. It indexes metadata only,
so it does not start Tesseract or download 1,425 lesson images. A full refresh
currently writes roughly 1,425 catalog records plus 143 checkpoint updates and 143 page snapshots;
account for Firestore read/write quotas before repeatedly rebuilding it. Normal
browsing returns ten entries using two Firestore document reads (state and page),
including direct jumps to page 143; it avoids expensive skipped-document offset
reads. Publishing loads the small metadata catalog once in the operator process
and writes one snapshot document per page. Full OCR runs only when a day is
opened. Newly saved series lessons from manual/daily processing also join the
catalog and exact-date lookup immediately; the paginated archive is a dated
snapshot, refreshed by rerunning the full indexer. This also catches other missed
historical days.
The existing protected external daily job remains responsible for unattended
new-lesson processing; enabling the archive does not enable an unreliable
in-process timer on Render Free.

The read-only archive API is development-only like the site itself:

```sh
curl 'http://127.0.0.1:8765/_dev/test-site/series/archive?page=143'
curl 'http://127.0.0.1:8765/_dev/test-site/series/archive?day=2021-01-01'
```

It returns explicit metadata models, indexed/source counts and page state.
There is no unauthenticated bulk-import HTTP endpoint. `ENABLE_TEST_SITE=false`
(the default) removes this route as well as the rest of the development UI.

Archive tests use short original JSON fixtures for pages 1 and 143, exercise
all 143 pages/1,425 distinct URLs deterministically, and mock external HTTP/OCR.
The Firestore emulator test verifies committed page replay, persistence across
repository instances, pagination and exact-date lookup. Run the ordinary suite
with `ENABLE_TEST_SITE=false SCRAPE_ENABLED=false .venv/bin/python -m pytest -q`;
set the loopback `FIRESTORE_EMULATOR_HOST` as documented above to include SDK tests.
