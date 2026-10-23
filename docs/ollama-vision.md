# Ollama Cloud image transcription

The production pipeline is now:

1. Discover the publisher-listed newest lesson (or accept a manual article URL).
2. Fetch/parse the Chosun article and choose its full lesson image.
3. Download and decode the image with the existing HTTPS/CDN, redirect, byte,
   pixel and timeout protections.
4. Send the image bytes to **Ollama Cloud**, using **`gemma4:31b`** by default.
5. Validate the generated transcription, organize the lesson, and save it in
   Cloud Firestore. The test website reads the saved result.

The app does not run RapidOCR, Tesseract, ONNX Runtime, or a local Ollama daemon
in production. Inference runs remotely. It requires no GPU, multi-gigabyte model
downloads, or persistent local filesystem on Render Free. The previous local OCR
adapters remain as optional compatibility/test code, outside the deployed
processing path. Ollama is the serving platform; Gemma 4 31B is the vision model.
No claim is made that AI transcription is always accurate, especially for small
Korean text. The model is told to transcribe visible text rather than translate,
rewrite, complete missing text, or follow instructions embedded in the graphic.

## Local setup

Use Python 3.12 and the existing virtual environment:

```sh
.venv/bin/python -m pip install -r requirements.txt
# Set the key privately in the environment, or use a file outside source control:
export OLLAMA_API_KEY_FILE="$PWD/secrets/ollama-api-key"
export OLLAMA_MODEL=gemma4:31b
sh scripts/start-local.sh
```

`secrets/` is excluded from Git and Docker. Give the key file mode 0600. The local
start script picks up `secrets/ollama-api-key` if it exists and `OLLAMA_API_KEY`
is unset. The application does not automatically source `.env` files. Never put
a real key in README examples, browser JavaScript, `render.yaml`, Git, or a Docker
build argument. Credentials are sent only in the Authorization header to the fixed
`https://ollama.com/api/chat` endpoint. Redirects are never followed. The key is
not passed to Chosun or its image CDN.

Open [the local test site](http://127.0.0.1:8765/_dev/test-site/), press **Check for
new expressions**, or submit a real article URL. These actions use the same
Ollama pipeline as manual API and scheduled work. Real transcription requires
Firestore credentials too. The fixture pages and mocked end-to-end tests remain
offline; browsing a fixture never calls Ollama.

```sh
curl --fail-with-body http://127.0.0.1:8765/health
curl --include --max-time 180 http://127.0.0.1:8765/api/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"article_url":"https://www.chosun.com/national/nie/english/2026/10/23/4GIRUNPIMZH7NDNUA2QEUEL5LQ/"}'
```

The example is a known publisher link, not a generated/guessed URL. Daily discovery
continues to read the series page; the date/article ID is never hardcoded into
production processing.

## Settings and Render Free

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_API_KEY` | unset | Server-side Ollama Cloud credential |
| `OLLAMA_API_KEY_FILE` | unset | Alternative secret file, used when the key variable is unset |
| `OLLAMA_MODEL` | `gemma4:31b` | Cloud vision model; use one enabled on your Ollama account |
| `OLLAMA_TIMEOUT_SECONDS` | `60` | Total time for preparing and transcribing one image |
| `OLLAMA_MAX_OUTPUT_TOKENS` | `4096` | Bound generated output; truncation is rejected |
| `OLLAMA_MAX_RESPONSE_BYTES` | `1048576` | Bound decoded provider response size |
| `OCR_MAX_BYTES` | `10485760` | Maximum image bytes sent for transcription |
| `OCR_MAX_PIXELS` | `4000000` | Image decoding limit before sending to Ollama |
| `SCRAPE_TIMEOUT_SECONDS` | `90` | Whole article/image/AI pipeline deadline |
| `SCRAPE_MAX_CONCURRENT_REQUESTS` | `1` | Bound simultaneous pipeline memory/use |

Deploy the repository as its existing Docker Web Service with one worker. Set
`OLLAMA_API_KEY` in **Render → english-scrapper → Environment**, save, and redeploy.
Alternatively add a Render secret file named `ollama-api-key` and set
`OLLAMA_API_KEY_FILE=/etc/secrets/ollama-api-key`. Retain the existing Firebase
project, Firestore, and service-account settings. The Blueprint requests a private
key via `sync: false`; it does not contain a real credential. Remove obsolete
`OCR_MODEL_DIR`/`TESSDATA_PREFIX` variables if present; they have no effect on the
new production provider.

No extra system packages or local model files are needed. `/health` does not
require an Ollama key, contact Ollama, or initialize models. Image pixels/base64
stay in memory for the request; saved texts and job state live in Firestore.
Render Free may sleep and restart; its first request may need to wake the service.
Cloud model loading, shared capacity, network latency, and account quotas can add
latency independently of Render. Ollama Cloud usage limits/pricing are separate
from Render Free. This change does not select a paid Render worker or a larger
plan. Use an external scheduler for reliable daily triggers as documented in the
README; an in-process timer on a sleeping Free service is not reliable.

## Validation, output and retry behavior

Ollama Cloud currently does **not** guarantee structured outputs. The prompt
requests JSON, but the app independently validates it with Pydantic. The model
returns text lines and fields containing exact copies of those lines. The server
derives field line indices itself, so incorrect model arithmetic cannot silently
attach the wrong text. Unknown schema fields, malformed JSON, oversized responses, and incomplete
generation fail before saving. A rewritten or misordered lesson field is left
unassigned with a warning; usable raw transcription is preserved. A single outer JSON code fence is
accepted; arbitrary commentary around JSON is rejected. This does not prove that
the model's transcription itself matches every printed character.

New API results use `ocr.engine: "ollama/gemma4:31b"`. `ocr.raw_text` contains
newline-joined text, and `ocr.lines` preserves the individual lines. Ollama vision
does not provide measured OCR coordinates/confidences here: `ocr.regions` is empty,
`ocr.mean_confidence` is null, and field `bounding_box`/`confidence` are null.
`line_indices` reference `ocr.lines`; `region_indices` is empty. Missing fields
remain null. Unreadable spans are marked `[unreadable]`, with a warning. The
parsing method is `ollama_vision_v1`. AI results carry a review warning and the
pipeline returns `processing.status: partial`, even when all fields are assigned.
Historical OCR responses retain their original engine labels and boxes/scores.

| HTTP | Application code | Meaning |
| --- | --- | --- |
| 503 | `ollama_not_configured` | Missing/unreadable server credential |
| 503 | `ollama_access_denied` | Key or model access denied |
| 503 | `ollama_rate_limited` | Account usage/concurrency limit |
| 502 | `ollama_unavailable` | Provider/network/model failure or rejected redirect |
| 502 | `ollama_invalid_response` | Invalid, truncated or oversized generated output |
| 504 | `ollama_timeout` | AI request exceeded its deadline |
| 422 | `ocr_invalid_image` / `ocr_image_too_large` | Invalid or excessive input |

There is one AI call per new scrape, with no hidden SDK retries or JSON-repair
calls. Manual failures are returned immediately. Existing daily-run retries use
bounded exponential backoff for transient 502/503/504 failures and preserve failed
queue entries for later daily runs. Retrying after a client/provider timeout can
consume another Ollama request; cancelling our HTTP request cannot guarantee the
provider stops inference. Existing canonical URLs reuse their saved result and do
not call Ollama again. No historical OCR is overwritten automatically. On the opt-in development site,
**Read image again with Ollama** makes an explicit new call and preserves the old
payload in `chosun_lessons/{id}/revisions/{revision_id}` in the same Firestore
transaction. The current record keeps its ID/creation timestamp. Failed refreshes
leave the old result intact. This action is not exposed when test routes are
disabled. Deploy the updated app before refreshing records: old deployments may
not understand nullable AI geometry/confidence fields.

## Verification

```sh
env -u FIREBASE_PROJECT_ID -u FIRESTORE_EMULATOR_HOST \
  ENABLE_TEST_SITE=false SCRAPE_ENABLED=false SCRAPE_IN_PROCESS=false \
  .venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_ollama_vision.py tests/test_local_fixture_e2e.py -q
```

Automated tests mock Ollama and external HTTP: no real key, image inference,
model downloads, publisher traffic, or API charges are required. A separate live
smoke check recognized the original synthetic lesson's Korean and English and
identified `Don't sweat it.` using Gemma 4 31B in approximately 5.3 seconds. The live October 23, 2026 publisher lesson was also fetched through the safe
pipeline: its 650 × 1442 image produced 24 transcript lines, the expression
`turn over a new leaf`, and all six lesson fields in approximately 5.1 seconds.
Verification results were kept locally; existing production Firestore data was
not changed. These checks confirm wiring, not character-perfect accuracy or a
Render deployment.

Sources: [Ollama Cloud](https://docs.ollama.com/cloud),
[image inputs](https://docs.ollama.com/capabilities/vision),
[structured-output limitation](https://docs.ollama.com/capabilities/structured-outputs),
[Gemma 4 vision models](https://ollama.com/library/gemma4).
