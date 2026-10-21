"""Public request, success, and error contracts."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.services.urls import validate_article_url


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_url: HttpUrl = Field(description="HTTPS article URL on chosun.com or www.chosun.com")

    @field_validator("article_url", mode="before")
    @classmethod
    def require_absolute_url(cls, value: object) -> object:
        if isinstance(value, (str, HttpUrl)):
            return validate_article_url(value)
        return value


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ArticleMetadata(BaseModel):
    url: HttpUrl
    title: str | None = None
    published_at: datetime | None = None
    section: str | None = None
    language: str | None = None


class LessonImageMetadata(BaseModel):
    url: HttpUrl
    alt_text: str | None = None
    content_type: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    resized_urls: dict[str, HttpUrl] = Field(default_factory=dict)
    source: Literal["content_elements", "og:image"] = "content_elements"
    is_fallback: bool = False
    content_element_path: list[int] | None = None
    byte_size: int | None = Field(default=None, ge=0)
    format: str | None = None
    download_url: HttpUrl | None = None


class BoundingBox(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class OCRRegion(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    bounding_box: BoundingBox
    low_confidence: bool = False


class OCRText(BaseModel):
    raw_text: str
    korean_text: str | None = None
    english_text: str | None = None
    engine: str = "tesseract"
    status: Literal["ok", "low_confidence", "empty"] = "ok"
    mean_confidence: float | None = Field(default=None, ge=0, le=1)
    regions: list[OCRRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LessonField(BaseModel):
    text: str
    region_indices: list[int]
    confidence: float = Field(ge=0, le=1)
    bounding_box: BoundingBox


class LessonFields(BaseModel):
    expression: LessonField | None = None
    korean_meaning: LessonField | None = None
    korean_explanation: LessonField | None = None
    english_dialogue: LessonField | None = None
    korean_dialogue_translation: LessonField | None = None
    vocabulary_notes: LessonField | None = None
    source_credit: LessonField | None = None


class LessonParsingResult(BaseModel):
    status: Literal["structured", "partial", "unparsed", "empty"]
    fields: LessonFields = Field(default_factory=LessonFields)
    reasons: list[str] = Field(default_factory=list)
    unassigned_region_indices: list[int] = Field(default_factory=list)
    method: str = "labeled_layout_v1"


class ProcessingStatus(BaseModel):
    status: Literal["completed", "partial"]
    completed_stages: list[Literal["url_validation", "article_fetch", "article_parse", "image_download", "ocr", "lesson_parse"]]
    ocr_status: Literal["ok", "low_confidence", "empty"]
    lesson_status: Literal["structured", "partial", "unparsed", "empty"]
    elapsed_ms: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class ScrapeResponse(BaseModel):
    article: ArticleMetadata
    lesson_image: LessonImageMetadata
    article_images: list[LessonImageMetadata] = Field(default_factory=list)
    ocr: OCRText
    lesson: LessonParsingResult
    processing: ProcessingStatus


class ValidationIssue(BaseModel):
    location: list[str | int]
    message: str
    type: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class DailyRunRecord(BaseModel):
    id: str
    scheduled_date: date
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    attempts: int
    stats: dict[str, int]
    error_code: str | None = None


class DailyFailure(BaseModel):
    url: str
    attempts: int
    error_code: str | None
    last_attempt_at: datetime | None


class DailyRunResult(BaseModel):
    status: str
    stats: dict[str, int]
    error_code: str | None = None
    finished_at: datetime | None = None


class DailyStatusResponse(BaseModel):
    enabled: bool
    in_process_enabled: bool = False
    timezone: str
    hour: int
    minute: int
    storage_configured: bool
    next_scheduled_at: datetime | None = None
    last_run: DailyRunRecord | None = None
    failures: list[DailyFailure] = Field(default_factory=list)
    local_error_code: str | None = None
    latest_run_result: DailyRunResult | None = None


class DailyTriggerResponse(BaseModel):
    status: str


class SavedLessonSummary(BaseModel):
    id: str
    canonical_url: str
    article: ArticleMetadata
    processing_status: str
    created_at: datetime
    updated_at: datetime


class SavedLessonResponse(BaseModel):
    id: str
    canonical_url: str
    payload: ScrapeResponse
    created_at: datetime
    updated_at: datetime


class ArticlePage(BaseModel):
    items: list[SavedLessonSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class SeriesExpression(BaseModel):
    id: str
    article: ArticleMetadata
    expression: str | None = None
    korean_meaning: str | None = None
    processing_status: Literal["completed", "partial"]
    lesson_status: Literal["structured", "partial", "unparsed", "empty"]
    saved_at: datetime
    issue_date: date | None = None
    lesson_image: LessonImageMetadata
    lesson: LessonParsingResult
    ocr: OCRText


class SeriesSavedResponse(BaseModel):
    items: list[SeriesExpression]
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)


class SeriesListedDay(BaseModel):
    article_url: HttpUrl
    issue_date: date | None = None


class SeriesCheckResponse(BaseModel):
    status: Literal["new", "up_to_date", "empty"]
    source_url: HttpUrl
    checked_at: datetime
    discovery_cached: bool
    listed_count: int = Field(ge=0)
    latest: SeriesExpression | None = None
    listed_days: list[SeriesListedDay] = Field(default_factory=list)


class ArchiveEntry(BaseModel):
    article_url: HttpUrl
    title: str
    expression: str
    issue_date: date


class ArchivePage(BaseModel):
    items: list[ArchiveEntry]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    pages: int = Field(ge=0)
    publisher_total: int | None = None
    indexed_pages: int = 0
    expected_pages: int = 0
    checked_at: datetime | None = None
