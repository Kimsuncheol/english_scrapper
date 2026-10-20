"""Thin HTTP routes delegating extraction to the service layer."""

from fastapi import APIRouter

from app.schemas import ErrorResponse, HealthResponse, ScrapeRequest, ScrapeResponse
from app.services import scrape as scrape_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.post(
    "/api/v1/scrape",
    response_model=ScrapeResponse,
    responses={
        422: {"model": ErrorResponse, "description": "Invalid request, article, or missing lesson image"},
        403: {"model": ErrorResponse, "description": "Publisher blocked access"},
        502: {"model": ErrorResponse, "description": "Publisher fetch failed"},
        504: {"model": ErrorResponse, "description": "Fetch or OCR processing deadline exceeded"},
        500: {"model": ErrorResponse, "description": "OCR recognition failed"},
        503: {"model": ErrorResponse, "description": "OCR unavailable or busy"},
    },
)
async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    return await scrape_service.scrape_article(request.article_url)
