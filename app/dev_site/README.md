# Local parser playground fixtures

These are synthetic HTML and PNG fixtures, not publisher article captures or
OCR ground truth. All browser image/style URLs point to this application's
development route. Chosun-shaped article URLs and `example.test` image URLs in
metadata are inert identifiers and are never fetched.

The original lesson raster is 720 × 1800, the smaller variant is 240 × 600, and
the thumbnail is 320 × 168. They contain synthetic bilingual lesson text and
were drawn locally with Pillow. No external media or model is required.

`app.dev_site` reads these files and invokes the existing pure article parser.
It serves only allowlisted fixtures/assets, never arbitrary URLs or disk paths.
Fusion script text lives in inert HTML templates; the parser reads it as usual,
while the browser never executes it. CSP also blocks scripts/external resources.
The route tree is
registered only when `ENABLE_TEST_SITE=true` before the app is created; it is
absent by default and does not appear in the API schema.

Use the repository README's local test website instructions to start it. This
playground does not initialize OCR, write to PostgreSQL, or relax production
SSRF restrictions.

The separate `tests/test_local_fixture_e2e.py` harness routes allowed logical
article/image requests to these real HTML/PNG routes using an in-process ASGI
transport. It tests the real fetch/decode pipeline with mocked OCR and memory
persistence; it adds no production localhost exception and needs no running
server, model download or external requests. Unusable malformed data reports
`invalid_article_data`; a valid text-only article reports `no_lesson_image`.
