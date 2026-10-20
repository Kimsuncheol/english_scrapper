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
        422: {"model": ErrorResponse, "description": "Invalid request or article URL"},
        403: {"model": ErrorResponse, "description": "Publisher blocked access"},
        502: {"model": ErrorResponse, "description": "Publisher fetch failed"},
        504: {"model": ErrorResponse, "description": "Publisher fetch timed out"},
        501: {"model": ErrorResponse, "description": "Extraction is not implemented yet"},
    },
)
async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    return await scrape_service.scrape_article(request.article_url)
