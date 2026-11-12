"""PaddleOCR contract, image limits and isolated-worker lifecycle tests."""

import asyncio
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.main import app
from app.services import paddle_ocr, paddle_models, scrape
from app.services.article import FetchedArticle
from app.services.images import DownloadedImage
from app.services.ocr import OCRConfig, OCRError, OCRService, create_ocr_service
from app.services.paddle_ocr import PaddleOCREngine, normalize_paddleocr


def png(size=(200, 100)):
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def prediction():
    return {"rec_texts": ["Don't give up!", "포기하지 마세요."],
            "rec_scores": [0.95, 0.85],
            "rec_polys": [[[10, 10], [190, 10], [190, 30], [10, 30]],
                          [[10, 40], [190, 40], [190, 60], [10, 60]]]}


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(paddle_models, "verify_models", Mock(return_value=[Path("det"), Path("rec")]))
    result = normalize_paddleocr(prediction(), 200, 100)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=result.model_dump_json().encode()))
    monkeypatch.setattr(paddle_ocr.subprocess, "run", run)
    return run


def test_default_backend_is_paddleocr(monkeypatch):
    monkeypatch.delenv("OCR_BACKEND", raising=False)
    service = create_ocr_service()
    assert isinstance(service, OCRService)
    assert service.factory.__name__ == "create_paddleocr_engine"
    assert OCRConfig().model_dir == ".ocr-models/paddleocr"
    monkeypatch.setenv("OCR_BACKEND", "rapidocr")
    with pytest.raises(ValueError, match="OCR_BACKEND"):
        create_ocr_service()


def test_korean_english_lines_confidence_and_original_coordinates():
    result = normalize_paddleocr(prediction(), 200, 100)
    assert result.engine == "paddleocr"
    assert result.raw_text == "Don't give up!\n포기하지 마세요."
    assert result.mean_confidence == pytest.approx(0.9)
    assert result.regions[1].bounding_box.model_dump() == {"x": 10, "y": 40, "width": 180, "height": 20}
    low = prediction()
    low["rec_scores"] = [0.1, 0.2]
    assert normalize_paddleocr(low, 200, 100).status == "low_confidence"
    empty = normalize_paddleocr({"rec_texts": [], "rec_scores": [], "rec_polys": []}, 200, 100)
    assert empty.status == "empty" and empty.regions == []


@pytest.mark.parametrize("result", [{}, {**prediction(), "rec_scores": []}, None])
def test_malformed_predictions_are_clean_errors(result):
    with pytest.raises(OCRError):
        normalize_paddleocr(result, 200, 100)


def test_explicit_cpu_korean_models_and_disabled_optional_modules(monkeypatch):
    verify = Mock(return_value=[Path("det"), Path("rec")])
    monkeypatch.setattr(paddle_models, "verify_models", verify)
    constructor = Mock()
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=constructor))
    paddle_ocr.create_paddleocr_api(OCRConfig())
    params = constructor.call_args.kwargs
    assert params["device"] == "cpu" and params["engine"] == "onnxruntime"
    assert params["engine_config"]["enable_cpu_mem_arena"] is False
    assert params["engine_config"]["providers"] == ["CPUExecutionProvider"]
    assert params["cpu_threads"] == 1 and params["enable_mkldnn"] is False
    assert params["text_recognition_model_name"] == "korean_PP-OCRv5_mobile_rec"
    assert params["text_detection_model_dir"] == "det"
    assert params["text_recognition_model_dir"] == "rec"
    assert params["text_recognition_batch_size"] == 1
    assert params["text_det_limit_side_len"] == 960
    assert not any(params[k] for k in ("use_doc_orientation_classify", "use_doc_unwarping", "use_textline_orientation"))


def test_worker_receives_memory_bytes_and_finite_deadline(worker):
    config = OCRConfig(timeout_ms=1234)
    data = png()
    result = PaddleOCREngine(config).recognize(data)
    assert result.engine == "paddleocr"
    args, kwargs = worker.call_args
    assert args[0][:3] == [sys.executable, "-m", "app.services.paddle_worker"]
    assert json.loads(args[0][3]) == asdict(config)
    assert kwargs["input"] == data and kwargs["timeout"] == 1.234
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["env"]["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] == "True"


@pytest.mark.parametrize("data,config,code", [
    (b"invalid", OCRConfig(), "ocr_invalid_image"),
    (b"x" * 11, OCRConfig(max_bytes=10), "ocr_image_too_large"),
    (png(), OCRConfig(max_pixels=10), "ocr_image_too_large"),
])
def test_invalid_images_never_start_native_runtime(worker, data, config, code):
    with pytest.raises(OCRError) as error:
        PaddleOCREngine(config).recognize(data)
    assert error.value.code == code
    worker.assert_not_called()


def test_animation_rejected_before_worker(worker):
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "white").save(buffer, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (20, 20), "black")], duration=100)
    with pytest.raises(OCRError, match="Animated"):
        PaddleOCREngine(OCRConfig()).recognize(buffer.getvalue())
    worker.assert_not_called()


def test_timeout_allows_next_request_and_does_not_leak_details(worker):
    good = worker.return_value
    worker.side_effect = [subprocess.TimeoutExpired("private dependency detail", 1), good]
    engine = PaddleOCREngine(OCRConfig())
    with pytest.raises(OCRError) as error:
        engine.recognize(png())
    assert error.value.status_code == 504 and "private" not in str(error.value)
    assert engine.recognize(png()).status == "ok"


@pytest.mark.parametrize("returncode,stdout,code", [(3, b"", "ocr_unavailable"),
    (-9, b"", "ocr_failed"), (4, b"", "ocr_failed"), (0, b"invalid", "ocr_failed")])
def test_worker_failures_are_sanitized(worker, returncode, stdout, code):
    worker.return_value = SimpleNamespace(returncode=returncode, stdout=stdout)
    with pytest.raises(OCRError) as error:
        PaddleOCREngine(OCRConfig()).recognize(png())
    assert error.value.code == code


def test_missing_or_corrupt_models_fail_without_network(tmp_path, monkeypatch):
    import socket
    network = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(socket.socket, "connect", network)
    service = OCRService(lambda: PaddleOCREngine(OCRConfig(model_dir=str(tmp_path))))
    assert not service.initialize()
    with pytest.raises(OCRError, match="initialization"):
        asyncio.run(service.recognize(png()))
    for name, spec in paddle_models.MODELS.items():
        folder = tmp_path / name
        folder.mkdir()
        for filename in spec["files"]:
            (folder / filename).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum"):
        paddle_models.verify_models(tmp_path)
    network.assert_not_called()


def test_public_fetch_returns_paddle_regions_without_storage(worker, monkeypatch):
    service = OCRService()
    html = '<script>Fusion.globalContent={"content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":1000,"height":1500}]};</script>'
    monkeypatch.setattr(scrape, "fetch_article", AsyncMock(return_value=FetchedArticle(
        url="https://www.chosun.com/lesson/paddle/", html=html, title="English lesson")))
    monkeypatch.setattr(scrape, "download_lesson_image", AsyncMock(side_effect=lambda image:
        DownloadedImage(data=png((1000,1500)), metadata=image)))
    monkeypatch.setattr(scrape, "recognize_image", service.recognize)
    response = TestClient(app).post("/api/v1/lessons/fetch", json={
        "article_url": "https://www.chosun.com/lesson/paddle/"})
    assert response.status_code == 200, response.text
    assert response.json()["ocr"]["engine"] == "paddleocr"
    assert len(response.json()["ocr"]["regions"]) == 2
    worker.assert_called_once()


def test_setup_extracts_only_verified_regular_files_and_reuses_cache(monkeypatch, tmp_path):
    from scripts import prepare_paddleocr_models as setup
    model = b"synthetic model"
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        info = tarfile.TarInfo("fixture_onnx_infer/inference.json")
        info.size = len(model)
        bundle.addfile(info, BytesIO(model))
        extra = tarfile.TarInfo("../../unexpected")
        extra.size = 3
        bundle.addfile(extra, BytesIO(b"bad"))
    payload = buffer.getvalue()
    manifest = {"fixture": {"archive_sha256": sha256(payload).hexdigest(),
                            "files": {"inference.json": sha256(model).hexdigest()}}}
    monkeypatch.setattr(setup, "MODELS", manifest)
    monkeypatch.setattr(paddle_models, "MODELS", manifest)
    download = Mock(side_effect=lambda *a, **kw: BytesIO(payload))
    monkeypatch.setattr(setup, "urlopen", download)
    setup.prepare(tmp_path)
    setup.prepare(tmp_path)
    assert (tmp_path / "fixture/inference.json").read_bytes() == model
    download.assert_called_once()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fixture"]
    (tmp_path / "fixture/inference.json").unlink()
    manifest["fixture"]["archive_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="archive checksum"):
        setup.prepare(tmp_path)
    assert not (tmp_path / "fixture/inference.json").exists()
