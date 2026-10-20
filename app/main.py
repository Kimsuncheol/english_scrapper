"""ASGI application and shared error handling."""

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import router
from app.schemas import ErrorDetail, ErrorResponse, ValidationIssue
from app.services.errors import ArticleFetchError
from app.services.article import ArticleFetchConfig
from app.services.images import ImageDownloadConfig
from app.services.scrape import ScrapeConfig
from app.daily_config import DailyConfig
from app.daily_api import router as daily_router
from app.services.daily import DailyRunner


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Misconfigured server limits should fail startup rather than disable limits.
    ArticleFetchConfig.from_env()
    ImageDownloadConfig.from_env()
    ScrapeConfig.from_env()
    # Language models initialize on the first OCR call, under the service lock.
    # /health must not wait for OCR or perform any network/model work.
    daily = DailyRunner(DailyConfig.from_env())
    app.state.daily_runner = daily
    daily.start()
    try:
        yield
    finally:
        await daily.stop()


async def article_fetch_error_handler(request: Request, exc: ArticleFetchError) -> JSONResponse:
    response = ErrorResponse(error=ErrorDetail(code=exc.code, message=str(exc)))
    return JSONResponse(status_code=exc.status_code, content=response.model_dump(mode="json"))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    response = ErrorResponse(
        error=ErrorDetail(
            code="validation_error",
            message="Request validation failed. Check the indicated fields.",
            details=[
                ValidationIssue(
                    location=list(error["loc"]), message=error["msg"], type=error["type"]
                )
                for error in exc.errors()
            ],
        )
    )
    return JSONResponse(status_code=422, content=response.model_dump(mode="json"))


def create_app() -> FastAPI:
    application = FastAPI(title="Chosun English Lesson API", version="0.1.0", lifespan=lifespan)
    application.include_router(router)
    application.include_router(daily_router)
    application.add_exception_handler(ArticleFetchError, article_fetch_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    flag = os.getenv("ENABLE_TEST_SITE", "false").lower()
    if flag not in {"true", "false"}:
        raise ValueError("ENABLE_TEST_SITE must be true or false")
    if flag == "true":
        from app.dev_site import router as test_site_router
        application.include_router(test_site_router)
    return application


app = create_app()
