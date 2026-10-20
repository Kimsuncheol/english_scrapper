# Chosun English Lesson API

FastAPI service that safely fetches Chosun.com articles and extracts article
metadata and lesson-image URLs, then downloads and validates the selected image
in memory. OCR is not implemented: successful requests return metadata with
`ocr: null`. No files are written at runtime.

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
  `source`, `is_fallback`, and `content_element_path`. After downloading,
  `content_type`, `byte_size`, `format`, `download_url`, and measured dimensions
  describe the verified image. `url` retains the original selected URL.
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
    scrape.py          # Fetch/parse/download orchestration
    article.py         # Bounded async HTML fetching and article checks
    parsing.py         # Pure metadata parsing and configurable image selection
    images.py          # In-memory download, format and pixel validation
    urls.py            # HTTPS article-host policy
    errors.py          # Shared application errors
tests/
  fixtures/            # Synthetic representative HTML samples
  test_api.py
  test_article.py
  test_parsing.py
  test_images.py
```

## Tests

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

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
The HTTP API exposes the metadata, not raw image bytes or a local file path.
`article_images` retains the original unverified candidate metadata. OCR is not
attempted and downloaded bytes are released after the request completes.

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
request bodies. The API currently uses the defaults.

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

## Render Free

Deploy as a Python Web Service using Python 3.12:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check: `/health`

Select the Free instance and omit `--reload`. There is no database, background
worker, OCR model, browser, or runtime storage requirement. Render's filesystem
is ephemeral. Downloads use bounded in-memory bytes and never depend on files
from earlier requests. Keep one worker and measure memory/latency before
adding OCR; bound image sizes, decoded pixels, runtime, and concurrent work.
