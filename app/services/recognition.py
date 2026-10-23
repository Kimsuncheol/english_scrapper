"""Internal result carrying a transcript and optional AI-classified lesson."""

from dataclasses import dataclass

from app.schemas import LessonParsingResult, OCRText


@dataclass(frozen=True)
class RecognizedLesson:
    ocr: OCRText
    lesson: LessonParsingResult
