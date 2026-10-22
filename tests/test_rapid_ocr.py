"""Deterministic adapter tests: no real inference, downloads, or publisher traffic."""

import asyncio
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.main import app
from app.services import ocr, rapid_ocr, scrape
from app.services.article import FetchedArticle
from app.services.images import DownloadedImage
from app.services.ocr import OCRConfig, OCRError, OCRService
from app.services.ocr_models import MODELS, verify_models
from app.services.rapid_ocr import DeadlineSession, RapidOCREngine, normalize_rapidocr


def output(texts=("Don't give up!", "포기하지 마세요."), scores=(0.95, 0.85)):
    boxes = [[[10, 10+i*30], [190, 10+i*30], [190, 30+i*30], [10, 30+i*30]] for i in range(len(texts))]
    return SimpleNamespace(boxes=boxes, txts=texts, scores=scores)


def png(size=(200, 100)):
    stream = BytesIO()
    Image.new("RGB", size, "white").save(stream, format="PNG")
    return stream.getvalue()


def fake_api(monkeypatch, result=None):
    api = Mock(return_value=result or output())
    monkeypatch.setattr(rapid_ocr, "create_rapidocr_api", Mock(return_value=api))
    return api


def test_korean_english_punctuation_reading_order_and_boxes():
    result = normalize_rapidocr(output(), 200, 100)
    assert result.engine == "rapidocr-onnx"
    assert result.raw_text == "Don't give up!\n포기하지 마세요."
    assert result.mean_confidence == pytest.approx(0.9)
    assert result.regions[1].bounding_box.model_dump() == {"x": 10, "y": 40, "width": 180, "height": 20}
    assert result.status == "ok"


def test_low_confidence_retained_and_empty_handled():
    result = normalize_rapidocr(output(("흐릿한 문장",), (0.2,)), 200, 100)
    assert result.status == "low_confidence" and result.regions[0].low_confidence
    assert result.raw_text == "흐릿한 문장" and result.warnings
    empty = normalize_rapidocr(SimpleNamespace(boxes=None, txts=None, scores=None), 200, 100)
    assert (empty.status, empty.raw_text, empty.regions, empty.mean_confidence) == ("empty", "", [], None)
    assert empty.warnings


@pytest.mark.parametrize("score", [float('nan'), float('inf'), -0.1, 1.1, 'bad'])
def test_invalid_scores_discarded(score):
    assert not normalize_rapidocr(output(("hello",), (score,)), 200, 100).regions


def test_polygon_clipping_unicode_and_invalid_boxes():
    result = output(("e\u0301", "bad"), (0.9, 0.9))
    result.boxes = [[[-2.5, -3], [205.1, 0], [201, 25.3], [0, 26]], [[float('nan'), 0]]*4]
    normalized = normalize_rapidocr(result, 200, 100)
    assert len(normalized.regions) == 1
    assert normalized.regions[0].text == "é"
    assert normalized.regions[0].bounding_box.model_dump() == {"x": 0, "y": 0, "width": 200, "height": 26}


def test_inconsistent_result_is_clean_error():
    result = output()
    result.scores = (0.9,)
    with pytest.raises(OCRError, match="unsupported result format"):
        normalize_rapidocr(result, 200, 100)


def test_default_service_initializes_rapid_engine_once(monkeypatch):
    api = fake_api(monkeypatch)
    service = OCRService()
    for _ in range(2):
        result = asyncio.run(service.recognize(png()))
        assert result.engine == "rapidocr-onnx"
    rapid_ocr.create_rapidocr_api.assert_called_once()
    assert api.call_count == 2
    # Inputs are decoded pixels, never paths or request bytes retained in a file.
    assert isinstance(api.call_args.args[0], Image.Image)
    service.close()


@pytest.mark.parametrize("data,config,code", [
    (b'invalid', OCRConfig(), 'ocr_invalid_image'),
    (b'x'*11, OCRConfig(max_bytes=10), 'ocr_image_too_large'),
    (png(), OCRConfig(max_pixels=10), 'ocr_image_too_large'),
])
def test_image_limits_before_inference(monkeypatch, data, config, code):
    api = fake_api(monkeypatch)
    with pytest.raises(OCRError) as error:
        RapidOCREngine(config).recognize(data)
    assert error.value.code == code
    api.assert_not_called()


def test_animated_images_rejected(monkeypatch):
    stream = BytesIO()
    Image.new('RGB', (20, 20), 'white').save(stream, format='GIF', save_all=True,
        append_images=[Image.new('RGB', (20, 20), 'black')], duration=100)
    api = fake_api(monkeypatch)
    with pytest.raises(OCRError) as error:
        RapidOCREngine(OCRConfig()).recognize(stream.getvalue())
    assert error.value.code == 'ocr_invalid_image'
    api.assert_not_called()


def test_deadline_failure_and_next_request_recovery(monkeypatch):
    api = fake_api(monkeypatch)
    engine = RapidOCREngine(OCRConfig())
    api.side_effect = [TimeoutError('private native details'), output()]
    with pytest.raises(OCRError) as error:
        engine.recognize(png())
    assert error.value.status_code == 504
    assert 'private' not in str(error.value)
    assert engine.recognize(png()).status == 'ok'


def test_deadline_session_terminates_native_run_without_sleep(monkeypatch):
    import onnxruntime
    options = SimpleNamespace(terminate=False)
    monkeypatch.setattr(onnxruntime, 'RunOptions', lambda: options)
    timer = Mock()
    def make_timer(seconds, terminate):
        timer.start.side_effect = terminate
        return timer
    monkeypatch.setattr(rapid_ocr, 'Timer', make_timer)
    native = Mock()
    wrapper = DeadlineSession(native, lambda: rapid_ocr.monotonic()+1)
    wrapper.run(['output'], {'input': 'pixels'})
    assert options.terminate
    native.run.assert_called_once_with(['output'], {'input': 'pixels'}, run_options=options)
    timer.cancel.assert_called_once()
    expired = DeadlineSession(native, lambda: 0)
    with pytest.raises(TimeoutError):
        expired.run([], {})
    assert native.run.call_count == 1


def test_missing_models_fail_without_importing_or_downloading(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    service = OCRService(lambda: RapidOCREngine(OCRConfig(model_dir=str(tmp_path))))
    assert not service.initialize()
    with pytest.raises(OCRError) as error:
        asyncio.run(service.recognize(png()))
    assert error.value.code == 'ocr_unavailable'
    socket.socket.connect.assert_not_called()


def test_checksums_reject_wrong_model(tmp_path):
    for name in MODELS:
        (tmp_path/name).write_bytes(b'not a model')
    with pytest.raises(RuntimeError, match='checksum'):
        verify_models(tmp_path)


def test_explicit_model_cpu_settings_and_no_orientation(monkeypatch, tmp_path):
    import rapidocr
    paths = [tmp_path/'det.onnx', tmp_path/'rec.onnx']
    monkeypatch.setattr(rapid_ocr, 'verify_models', Mock(return_value=paths))
    constructor = Mock()
    monkeypatch.setattr(rapidocr, 'RapidOCR', constructor)
    rapid_ocr.create_rapidocr_api(OCRConfig(model_dir=str(tmp_path)))
    params = constructor.call_args.kwargs['params']
    assert params['Rec.lang_type'] == rapidocr.LangRec.KOREAN
    assert params['Det.model_path'] == str(paths[0])
    assert params['Rec.model_path'] == str(paths[1])
    assert params['Rec.rec_batch_num'] == 1
    assert params['Global.use_cls'] is False
    assert params['Global.text_score'] == 0
    assert params['EngineConfig.onnxruntime.intra_op_num_threads'] == 1
    assert params['EngineConfig.onnxruntime.enable_cpu_mem_arena'] is False
    constructor.return_value._load_det_model.assert_called_once()
    constructor.return_value._load_rec_model.assert_called_once()


def test_manual_endpoint_persists_rapidocr_output_idempotently(monkeypatch, persisted_store):
    api = fake_api(monkeypatch)
    fixture = json.loads((Path(__file__).parent/'fixtures/lesson_ocr_single_column.json').read_text())['ocr']
    texts, boxes, scores = [], [], []
    # RapidOCR emits complete lines. Coalesce this single-column fixture's word
    # annotations by their exact line coordinates, independently of the parser.
    lines = {}
    for region in fixture['regions']:
        box = region['bounding_box']
        lines.setdefault((box['y'], box['height']), []).append(region)
    for (y, h), regions in lines.items():
        regions.sort(key=lambda region: region['bounding_box']['x'])
        x = min(region['bounding_box']['x'] for region in regions)
        right = max(region['bounding_box']['x']+region['bounding_box']['width'] for region in regions)
        texts.append(' '.join(region['text'] for region in regions))
        scores.append(sum(region['confidence'] for region in regions)/len(regions))
        boxes.append([[x,y], [right,y], [right,y+h], [x,y+h]])
    api.return_value = SimpleNamespace(txts=texts, scores=scores, boxes=boxes)
    service = OCRService()
    html = '<script>Fusion.globalContent={"content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":1000,"height":1500}]};</script>'
    monkeypatch.setattr(scrape, 'fetch_article', AsyncMock(return_value=FetchedArticle(
        url='https://www.chosun.com/lesson/rapid/', html=html, title='English lesson')))
    monkeypatch.setattr(scrape, 'download_lesson_image', AsyncMock(side_effect=lambda image:
        DownloadedImage(data=png((1000,1500)), metadata=image)))
    monkeypatch.setattr(scrape, 'recognize_image', service.recognize)
    try:
        client = TestClient(app)
        response = client.post('/api/v1/scrape', json={'article_url':'https://www.chosun.com/lesson/rapid/'})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['ocr']['engine'] == 'rapidocr-onnx'
        assert body['ocr']['regions'] and body['ocr']['raw_text']
        assert body['lesson']['fields']['expression']['text'] == "Don't sweat it."
        again = client.post('/api/v1/scrape', json={'article_url':'https://www.chosun.com/lesson/rapid/'})
        assert again.status_code == 200
        api.assert_called_once()
    finally:
        service.close()


def test_setup_download_verifies_hash_and_reuses_cached_model(monkeypatch, tmp_path):
    from hashlib import sha256
    from scripts import prepare_ocr_models as setup
    from app.services import ocr_models
    payload = b'original synthetic model fixture'
    manifest = {'fixture.onnx': ('https://example.invalid/model', sha256(payload).hexdigest())}
    monkeypatch.setattr(setup, 'MODELS', manifest)
    monkeypatch.setattr(ocr_models, 'MODELS', manifest)
    download = Mock(return_value=BytesIO(payload))
    monkeypatch.setattr(setup, 'urlopen', download)
    setup.prepare(tmp_path)
    assert (tmp_path/'fixture.onnx').read_bytes() == payload
    setup.prepare(tmp_path)
    download.assert_called_once()
    assert not (tmp_path/'fixture.download').exists()


def test_setup_rejects_tampered_download_without_publishing_it(monkeypatch, tmp_path):
    from scripts import prepare_ocr_models as setup
    monkeypatch.setattr(setup, 'MODELS', {'fixture.onnx': ('https://example.invalid/model', '0'*64)})
    monkeypatch.setattr(setup, 'urlopen', Mock(return_value=BytesIO(b'tampered')))
    with pytest.raises(RuntimeError, match='checksum'):
        setup.prepare(tmp_path)
    assert not (tmp_path/'fixture.onnx').exists()
    assert not (tmp_path/'fixture.download').exists()


def test_environment_model_directory(monkeypatch):
    monkeypatch.setenv('OCR_MODEL_DIR', '/app/models')
    assert OCRConfig.from_env().model_dir == '/app/models'
