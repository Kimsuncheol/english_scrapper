"""Thin HTTP routes delegating extraction to the service layer."""

from fastapi import APIRouter, Depends, Response

from app.schemas import ErrorResponse, HealthResponse, ScrapeRequest, ScrapeResponse
from app.dependencies import get_store
from app.services.saved import process_and_store

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
        503: {"model": ErrorResponse, "description": "Service or OCR busy, or OCR unavailable"},
    },
)
async def scrape(request: ScrapeRequest, response: Response, store=Depends(get_store)) -> ScrapeResponse:
    saved, _ = await process_and_store(store, request.article_url)
    response.headers["Location"] = f"/api/v1/articles/{saved.id}"
    return saved.payload
