"""Opt-in fixture playground with a browser form for the public scrape API."""

from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from app.services.errors import ArticleFetchError
from app.services.parsing import metadata_from_page, parse_article_html, read_page
from app.dependencies import get_store
from app.schemas import SeriesCheckResponse, SeriesSavedResponse
from app.services.series import saved_expressions

ROOT = Path(__file__).parent
PREFIX = "/_dev/test-site"
CASES = {
    "one-tall-image": ("One tall lesson image", "JSON-LD metadata and one full-size article graphic."),
    "thumbnail-and-tall": ("Thumbnail + tall lesson image", "The parser should select the article graphic rather than the OG thumbnail."),
    "no-lesson-image": ("No lesson image", "A text-only article should report no_lesson_image."),
    "malformed-data": ("Malformed article data", "Broken JSON-LD and Fusion data should be ignored safely."),
}
ASSETS = {"style.css", "scrape.js", "lesson.png", "lesson-small.png", "thumbnail.png"}
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    # Embedded article scripts are data for the parser, never browser code.
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}
router = APIRouter(prefix=PREFIX, include_in_schema=False)


def navigation():
    links = "".join(f'<a href="{PREFIX}/articles/{slug}">{escape(title)}</a>' for slug, (title, _) in CASES.items())
    return (f'<header class="site-header"><a class="brand" href="{PREFIX}/">Lesson scraper lab</a>'
        '<span class="badge">DEVELOPMENT ONLY · OFFLINE FIXTURES</span></header>'
        f'<nav aria-label="Fixture pages"><a href="{PREFIX}/">All fixtures</a>{links}</nav>')


def inspect_fixture(html, slug):
    # The production parser requires an approved article source. This is inert
    # synthetic metadata, not a network destination or an SSRF allowlist change.
    source = f"https://www.chosun.com/test-fixtures/{slug}/"
    page = read_page(html)
    debug = {
        "status": "ok",
        "article": metadata_from_page(page, source).model_dump(mode="json"),
        "json_ld_documents": len(page.jsonld),
        "fusion_global_content_parsed": bool(page.global_content),
        "lesson_image": None,
        "article_images": [],
        "error": None,
    }
    try:
        result = parse_article_html(html, source)
        debug["lesson_image"] = result.lesson_image.model_dump(mode="json")
        debug["article_images"] = [image.model_dump(mode="json") for image in result.article_images]
    except ArticleFetchError as exc:
        debug.update(status="error", error={"code": exc.code, "message": str(exc)})
    return debug


def debug_panel(data):
    selected = data["lesson_image"]
    if selected:
        name = Path(urlsplit(selected["url"]).path).name
        preview = (f'<a href="{PREFIX}/assets/{name}">Open selected local image</a>'
            if name in ASSETS and name.endswith(".png") else "")
        summary = f'<p class="success">Selected {escape(name)} · {selected["width"]} × {selected["height"]}</p>{preview}'
    else:
        summary = f'<p class="error">{escape(data["error"]["code"])}</p>'
    payload = escape(json.dumps(data, ensure_ascii=False, indent=2))
    return ('<aside class="debug-panel"><h2>Parser output</h2>' + summary +
        '<p>Computed from this file by the real article parser. No fetching, OCR or database work.</p>'
        f'<pre id="parser-debug">{payload}</pre></aside>')


@router.get("/", response_class=HTMLResponse)
def index():
    cards = "".join(f'<a class="fixture-card" href="{PREFIX}/articles/{slug}">'
        f'<span class="case-number">0{index}</span><h2>{escape(title)}</h2>'
        f'<p>{escape(description)}</p><span>Open fixture →</span></a>'
        for index, (slug, (title, description)) in enumerate(CASES.items(), 1))
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    html = html.replace("<!-- FIXTURE_NAV -->", navigation()).replace("<!-- FIXTURE_LINKS -->", cards)
    return HTMLResponse(html, headers=HEADERS)


@router.get("/articles/{slug}", response_class=HTMLResponse)
def fixture_article(slug: str):
    if slug not in CASES:
        raise HTTPException(status_code=404, detail="Unknown development fixture")
    html = (ROOT / "fixtures" / f"{slug}.html").read_text(encoding="utf-8")
    data = inspect_fixture(html, slug)
    html = html.replace("<!-- FIXTURE_NAV -->", navigation()).replace("<!-- PARSER_DEBUG -->", debug_panel(data))
    return HTMLResponse(html, headers=HEADERS)


@router.get("/assets/{name}")
def asset(name: str):
    if name not in ASSETS:
        raise HTTPException(status_code=404, detail="Unknown development asset")
    return FileResponse(ROOT / "assets" / name, headers=HEADERS)


@router.get("/series/saved", response_model=SeriesSavedResponse)
async def series_saved(response: Response, store=Depends(get_store)):
    response.headers["Cache-Control"] = "no-store"
    return await saved_expressions(store)


@router.post("/series/check", response_model=SeriesCheckResponse)
async def series_check(request: Request, response: Response, store=Depends(get_store)):
    response.headers["Cache-Control"] = "no-store"
    return await request.app.state.series_checker.check(store)
