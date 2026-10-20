from unittest.mock import AsyncMock

import pytest

from app.services import scrape
from app.services.images import DownloadedImage


@pytest.fixture
def mock_image_download(monkeypatch):
    """Keep API/parser tests separate from image transport and decoding tests."""
    download = AsyncMock(side_effect=lambda image: DownloadedImage(data=b"test-image", metadata=image))
    monkeypatch.setattr(scrape, "download_lesson_image", download)
    return download
