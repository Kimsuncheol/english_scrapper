"""Public request, success, and error contracts."""

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ScrapeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_url: HttpUrl = Field(description="Absolute HTTP(S) Chosun.com article URL")

    @field_validator("article_url", mode="before")
    @classmethod
    def require_absolute_url(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                parts = urlsplit(value)
                valid = parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("article_url must be an absolute HTTP or HTTPS URL")
        return value

    @field_validator("article_url")
    @classmethod
    def require_chosun_url(cls, value: HttpUrl) -> HttpUrl:
        host = (value.host or "").lower()
        if host != "chosun.com" and not host.endswith(".chosun.com"):
            raise ValueError("article_url must use chosun.com or a subdomain of chosun.com")
        if value.username is not None or value.password is not None:
            raise ValueError("article_url must not include credentials")
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
