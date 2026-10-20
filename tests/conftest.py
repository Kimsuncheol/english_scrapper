from unittest.mock import AsyncMock

import pytest

from app.services import scrape
from app.services.images import DownloadedImage
from app.schemas import OCRText


@pytest.fixture
def mock_image_download(monkeypatch):
    """Keep API/parser tests separate from image transport and decoding tests."""
    download = AsyncMock(side_effect=lambda image: DownloadedImage(data=b"test-image", metadata=image))
    monkeypatch.setattr(scrape, "download_lesson_image", download)
    return download


@pytest.fixture
def mock_ocr(monkeypatch):
    recognize = AsyncMock(return_value=OCRText(raw_text="안녕하세요 Hello"))
    monkeypatch.setattr(scrape, "recognize_image", recognize)
    return recognize
