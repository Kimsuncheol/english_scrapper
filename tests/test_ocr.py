import asyncio
from io import BytesIO
from threading import Event, Thread
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.main import app
from app.schemas import OCRText
from app.services import ocr, scrape
from app.services.article import FetchedArticle
from app.services.ocr import OCRConfig, OCRError, OCRService, TesseractEngine, normalize_tesseract_tsv

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def word(text="Hello", confidence="90", x=10, y=20, w=50, h=15, line=1):
    return f"5\t1\t1\t1\t{line}\t1\t{x}\t{y}\t{w}\t{h}\t{confidence}\t{text}\n"


def png(size=(200, 100)):
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


def test_normalizes_korean_english_regions_and_reading_order():
    data = HEADER + word("안녕하세요", "93.5") + word("Hello", "80", x=80) + word("World", "95", line=2, y=50)
    result = normalize_tesseract_tsv(data, 200, 100)
    assert result.raw_text == "안녕하세요 Hello\nWorld"
    assert result.status == "ok"
    assert result.regions[0].confidence == 0.935
    assert result.regions[0].bounding_box.model_dump() == {"x": 10, "y": 20, "width": 50, "height": 15}
    assert result.korean_text is None and result.english_text is None
    assert result.mean_confidence == pytest.approx((0.935 + 0.8 + 0.95) / 3)


def test_low_confidence_is_retained_and_flagged():
    result = normalize_tesseract_tsv(HEADER + word("흐릿함", "20"), 200, 100)
    assert result.status == "low_confidence"
    assert result.raw_text == "흐릿함"
    assert result.regions[0].low_confidence
    assert result.warnings
    assert normalize_tesseract_tsv(HEADER + word("text", "60"), 200, 100, min_confidence=0.7).status == "low_confidence"


def test_native_headerless_tsv_is_normalized():
    native = "1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t\n" + word("안녕하세요", "95") + word("Hello", "96", x=80)
    result = normalize_tesseract_tsv(native, 200, 100)
    assert result.raw_text == "안녕하세요 Hello"
    assert len(result.regions) == 2


@pytest.mark.parametrize("tsv", ["", HEADER, HEADER + word(" "), HEADER + word("bad", "-1")])
def test_empty_ocr_results(tsv):
    result = normalize_tesseract_tsv(tsv, 200, 100)
    assert result.status == "empty"
    assert result.regions == []
    assert result.raw_text == ""
    assert result.mean_confidence is None
    assert result.warnings


@pytest.mark.parametrize("row", [word(confidence="nan"), word(confidence="inf"), word(confidence="101"), word(confidence="bad"), word(x=-1), word(w=0), word(x=1000)])
def test_invalid_regions_are_discarded(row):
    assert normalize_tesseract_tsv(HEADER + row, 200, 100).regions == []


def test_clips_boxes_to_original_image_and_normalizes_unicode():
    result = normalize_tesseract_tsv(HEADER + word("e\u0301", x=190, y=90), 200, 100)
    assert result.regions[0].text == "é"
    assert result.regions[0].bounding_box.width == 10
    assert result.regions[0].bounding_box.height == 10


def test_unsupported_result_format_is_clean_error():
    with pytest.raises(OCRError) as error:
        normalize_tesseract_tsv("text\tconfidence\nhello\t99", 200, 100)
    assert error.value.code == "ocr_failed"


def fake_api(monkeypatch):
    api = Mock()
    api.Recognize.return_value = True
    api.GetTSVText.return_value = HEADER + word("안녕") + word("Hello", x=80)
    factory = Mock(return_value=api)
    monkeypatch.setattr(ocr, "create_tesseract_api", factory)
    return api, factory


def test_engine_keeps_api_and_clears_each_request(monkeypatch):
    api, factory = fake_api(monkeypatch)
    engine = TesseractEngine(OCRConfig())
    for _ in range(2):
        result = engine.recognize(png())
        assert result.raw_text == "안녕 Hello"
    factory.assert_called_once()
    assert api.SetImage.call_count == 2
    api.Recognize.assert_called_with(timeout=30_000)
    assert api.Clear.call_count == 2
    assert api.ClearAdaptiveClassifier.call_count == 2
    engine.close()
    api.End.assert_called_once()


def test_engine_limits_pixels_before_recognition(monkeypatch):
    api, _ = fake_api(monkeypatch)
    engine = TesseractEngine(OCRConfig(max_pixels=100))
    with pytest.raises(OCRError) as error:
        engine.recognize(png())
    assert error.value.code == "ocr_image_too_large"
    api.SetImage.assert_not_called()
    api.Clear.assert_called_once()


def test_engine_limits_bytes_and_rejects_invalid_image(monkeypatch):
    api, _ = fake_api(monkeypatch)
    engine = TesseractEngine(OCRConfig(max_bytes=10))
    with pytest.raises(OCRError) as error:
        engine.recognize(b"x" * 11)
    assert error.value.code == "ocr_image_too_large"
    with pytest.raises(OCRError) as error:
        engine.recognize(b"invalid")
    assert error.value.code == "ocr_invalid_image"
    api.SetImage.assert_not_called()


def test_recognition_failure_always_clears_image_state(monkeypatch):
    api, _ = fake_api(monkeypatch)
    api.Recognize.return_value = False
    engine = TesseractEngine(OCRConfig())
    with pytest.raises(OCRError) as error:
        engine.recognize(png())
    assert error.value.code == "ocr_recognition_failed"
    api.Clear.assert_called_once()


def test_service_initializes_once_and_uses_replaceable_engine():
    engine = Mock()
    engine.recognize.return_value = OCRText(raw_text="안녕 Hello")
    factory = Mock(return_value=engine)
    service = OCRService(factory)
    assert service.initialize()
    assert service.initialize()
    for _ in range(2):
        assert asyncio.run(service.recognize(b"pixels")).raw_text == "안녕 Hello"
    factory.assert_called_once()
    assert engine.recognize.call_count == 2
    service.close()
    service.close()
    engine.close.assert_called_once()


@pytest.mark.parametrize("exception", [RuntimeError("no kor model"), ImportError("no binding"), OSError("no native library")])
def test_initialization_failure_is_cached(exception):
    factory = Mock(side_effect=exception)
    service = OCRService(factory)
    assert not service.initialize()
    for _ in range(2):
        with pytest.raises(OCRError) as error:
            asyncio.run(service.recognize(b"bytes"))
        assert error.value.code == "ocr_unavailable"
        assert error.value.status_code == 503
    factory.assert_called_once()


def test_busy_service_rejects_parallel_recognition():
    started, release = Event(), Event()
    engine = Mock()
    def recognize(data):
        started.set()
        release.wait(5)
        return OCRText(raw_text="test")
    engine.recognize.side_effect = recognize
    service = OCRService(lambda: engine)
    worker = Thread(target=lambda: asyncio.run(service.recognize(b"one")))
    worker.start()
    try:
        assert started.wait(3)
        with pytest.raises(OCRError) as error:
            asyncio.run(service.recognize(b"two"))
        assert error.value.code == "ocr_busy"
    finally:
        release.set()
        worker.join(5)
        service.close()


def test_unexpected_engine_failure_is_clean_application_error():
    engine = Mock()
    engine.recognize.side_effect = RuntimeError("internal details")
    service = OCRService(lambda: engine)
    with pytest.raises(OCRError) as error:
        asyncio.run(service.recognize(b"bytes"))
    assert error.value.code == "ocr_failed"
    assert "internal details" not in str(error.value)


def test_missing_language_files_do_not_trigger_download(tmp_path):
    with pytest.raises(RuntimeError, match="kor and eng"):
        ocr.create_tesseract_api(OCRConfig(tessdata_dir=str(tmp_path)))


def test_missing_native_binding_is_clean_initialization_failure(monkeypatch, tmp_path):
    for language in ("kor", "eng"):
        (tmp_path / f"{language}.traineddata").touch()
    monkeypatch.setattr(ocr, "_tesserocr", None)
    monkeypatch.setattr(ocr, "_binding_error", ImportError("library missing"))
    service = OCRService(lambda: TesseractEngine(OCRConfig(tessdata_dir=str(tmp_path))))
    assert not service.initialize()
    with pytest.raises(OCRError) as error:
        asyncio.run(service.recognize(b"bytes"))
    assert error.value.code == "ocr_unavailable"


def test_api_creation_requires_both_languages_and_lstm_mode(monkeypatch, tmp_path):
    for language in ("kor", "eng"):
        (tmp_path / f"{language}.traineddata").touch()
    binding = Mock()
    monkeypatch.setattr(ocr, "_tesserocr", binding)
    config = OCRConfig(tessdata_dir=str(tmp_path))
    ocr.create_tesseract_api(config)
    binding.PyTessBaseAPI.assert_called_once_with(
        path=str(tmp_path), lang="kor+eng", oem=binding.OEM.LSTM_ONLY, psm=binding.PSM.AUTO
    )


def test_environment_config(monkeypatch):
    monkeypatch.setenv("OCR_MIN_CONFIDENCE", "0.7")
    monkeypatch.setenv("OCR_MAX_PIXELS", "1000000")
    monkeypatch.setenv("OCR_TIMEOUT_MS", "1000")
    config = OCRConfig.from_env()
    assert (config.min_confidence, config.max_pixels, config.timeout_ms) == (0.7, 1000000, 1000)


def test_api_maps_ocr_initialization_failure(monkeypatch, mock_image_download):
    html = '<script>Fusion.globalContent = {"content_elements":[{"type":"image","url":"https://images.chosun.com/a.png"}]};</script>'
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(url="https://www.chosun.com/example/", html=html, title="Test")))
    monkeypatch.setattr(scrape, "recognize_image", AsyncMock(side_effect=OCRError("ocr_unavailable", "OCR unavailable", 503)))
    response = TestClient(app).post("/api/v1/scrape", json={"article_url": "https://www.chosun.com/example/"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ocr_unavailable"


def test_lifespan_initializes_ocr_without_breaking_health(monkeypatch):
    import app.main as main
    initialize = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "initialize_ocr", initialize)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    initialize.assert_awaited_once()
