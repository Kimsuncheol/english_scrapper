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


class LessonImageMetadata(BaseModel):
    url: HttpUrl
    alt_text: str | None = None
    content_type: str | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class OCRText(BaseModel):
    raw_text: str
    korean_text: str | None = None
    english_text: str | None = None


class ScrapeResponse(BaseModel):
    article: ArticleMetadata
    lesson_image: LessonImageMetadata
    ocr: OCRText


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
