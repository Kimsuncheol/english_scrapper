"""ASGI application and shared error handling."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import router
from app.schemas import ErrorDetail, ErrorResponse, ValidationIssue
from app.services.errors import ArticleFetchError
from app.services.ocr import initialize_ocr


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Failure leaves health/liveness usable; scrape returns a structured 503.
    # Native resources live for the process and are released through atexit.
    await initialize_ocr()
    yield


app = FastAPI(title="Chosun English Lesson API", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(ArticleFetchError)
async def article_fetch_error_handler(request: Request, exc: ArticleFetchError) -> JSONResponse:
    response = ErrorResponse(error=ErrorDetail(code=exc.code, message=str(exc)))
    return JSONResponse(status_code=exc.status_code, content=response.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
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
