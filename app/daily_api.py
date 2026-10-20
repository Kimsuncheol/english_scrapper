"""Daily-job status/control and persistent saved-lesson retrieval."""

from datetime import datetime, timezone
import secrets

from fastapi import APIRouter, Header, Query, Request
import psycopg

from app.schemas import DailyStatusResponse, DailyTriggerResponse, ErrorResponse, SavedLessonResponse, SavedLessonSummary
from app.services.errors import ArticleFetchError
from app.services.storage import StorageUnavailable

router = APIRouter(prefix="/api/v1", responses={503: {"model": ErrorResponse}})


def runner(request):
    return request.app.state.daily_runner


@router.get("/jobs/daily", response_model=DailyStatusResponse)
async def daily_status(request: Request):
    service = runner(request)
    config = service.config
    data = {"last_run": None, "failures": []}
    if config.database_url:
        try:
            data = await service.store.status()
        except (psycopg.Error, OSError) as exc:
            raise StorageUnavailable() from exc
    return DailyStatusResponse(enabled=config.enabled, timezone=config.timezone,
        hour=config.hour, minute=config.minute, storage_configured=bool(config.database_url),
        next_scheduled_at=config.next_due(datetime.now(timezone.utc)),
        local_error_code=service.error_code, **data)


@router.post("/jobs/daily/trigger", response_model=DailyTriggerResponse, status_code=202,
    responses={401: {"model": ErrorResponse}})
async def trigger_daily(request: Request, authorization: str | None = Header(default=None)):
    service = runner(request)
    token = service.config.trigger_token
    if not token:
        raise ArticleFetchError("trigger_unavailable", "Configure SCRAPE_TRIGGER_TOKEN to allow external wake-up.", 503)
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
        raise ArticleFetchError("authentication_required", "A valid Bearer token is required.", 401)
    return DailyTriggerResponse(status=service.trigger())


@router.get("/lessons", response_model=list[SavedLessonSummary])
async def saved_lessons(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    try:
        return await runner(request).store.list_lessons(limit, offset)
    except (psycopg.Error, OSError) as exc:
        raise StorageUnavailable() from exc


@router.get("/lessons/{lesson_id}", response_model=SavedLessonResponse,
    responses={404: {"model": ErrorResponse}})
async def saved_lesson(request: Request, lesson_id: str):
    try:
        value = await runner(request).store.lesson(lesson_id)
    except (psycopg.Error, OSError) as exc:
        raise StorageUnavailable() from exc
    if value is None:
        raise ArticleFetchError("lesson_not_found", "No saved lesson has that ID.", 404)
    return value
