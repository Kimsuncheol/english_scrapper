"""Public request, success, and error contracts."""

from datetime import datetime
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


class ScrapeResponse(BaseModel):
    article: ArticleMetadata
    lesson_image: LessonImageMetadata
    article_images: list[LessonImageMetadata] = Field(default_factory=list)
    ocr: OCRText | None = None
    lesson: LessonParsingResult | None = None


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
