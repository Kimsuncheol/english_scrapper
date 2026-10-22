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
while the browser never executes it. CSP blocks external resources and inline scripts. The index loads one local
script for the live URL form, with same-origin API requests allowed.
The route tree is
registered only when `ENABLE_TEST_SITE=true` before the app is created; it is
absent by default and does not appear in the API schema.

Use the repository README's local test website instructions to start it. Fixture browsing does not initialize OCR or write to Firestore. The index URL
form calls the regular scrape API, which runs real OCR and saves to Firestore
when configured. Neither mode relaxes production SSRF restrictions.

The separate `tests/test_local_fixture_e2e.py` harness routes allowed logical
article/image requests to these real HTML/PNG routes using an in-process ASGI
transport. It tests the real fetch/decode pipeline with mocked OCR and memory
persistence; it adds no production localhost exception and needs no running
server, model download or external requests. Unusable malformed data reports
`invalid_article_data`; a valid text-only article reports `no_lesson_image`.

The homepage also reads recent saved series expressions from Firestore. Its
**Check for new expressions** button explicitly checks the fixed publisher tag
page and processes the newest unsaved article through the normal OCR pipeline.
Successful source discovery is cached for 60 seconds per process. The individual
fixture article pages remain offline; no publisher/OCR calls happen on homepage
load. Saved cards persist through Firestore and can be reloaded with **Refresh
saved expressions**. “New” means new to the scraper, not necessarily newly published.

Daily lesson views include all structured fields that were identified, complete
raw OCR text, and expandable original regions. The lesson day is parsed from the
real article URL separately from its publication timestamp. Saved-day pagination
has no fixed recent-record cutoff. Checking the source lists publisher-provided
older dates, which can be individually loaded through the normal scrape pipeline.
The separate historical archive lists every indexed publisher page, with page
navigation, direct page selection and exact-date lookup back to January 2021.
Its titles come from publisher metadata; **Open full lesson** OCRs an unsaved day
or loads its existing complete saved output. `python -m scripts.index_archive`
populates all pages in Firestore outside the web process; `--resume` completes an
interrupted initial import. See the main README for credentials, limits and
refresh behavior. The homepage only reads persisted catalog/results; it does not
crawl the publisher automatically on load.

The Full OCR text reading view preserves every nonblank OCR line in order, with
roomier typography, paragraph spacing, explicit section-heading emphasis and A/B
speaker cues. It does not infer missing lesson fields or correct recognized
words. Expand **Original OCR text · exact line breaks** for the unchanged raw
string; the API payload and raw region indices are unchanged.
