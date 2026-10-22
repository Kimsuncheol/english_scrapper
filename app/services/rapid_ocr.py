"""CPU-only Korean/English OCR with verified local ONNX models and no runtime downloads.

RapidOCR 3.10.0's lazy loaders/session attributes are deliberately isolated here;
upgrading the library requires checking this adapter and its offline smoke test.
"""

from io import BytesIO
import math
from threading import Timer
from time import monotonic
import unicodedata
import warnings

from PIL import Image, UnidentifiedImageError

from app.schemas import BoundingBox, OCRRegion, OCRText
from app.services.ocr import OCRConfig, OCRError
from app.services.ocr_models import verify_models


def normalize_rapidocr(result, width: int, height: int, min_confidence: float = 0.5) -> OCRText:
    """Keep line regions in engine reading order; enclose polygons in pixel boxes."""
    boxes, texts, scores = result.boxes, result.txts, result.scores
    if all(value is None for value in (boxes, texts, scores)):
        boxes, texts, scores = [], [], []
    if any(value is None for value in (boxes, texts, scores)) or not len(boxes) == len(texts) == len(scores):
        raise OCRError("ocr_failed", "OCR engine returned an unsupported result format.", 500)
    regions = []
    for polygon, text, score in zip(boxes, texts, scores):
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            score = float(score)
            points = [(float(x), float(y)) for x, y in polygon]
            if len(points) != 4 or not all(math.isfinite(v) for point in points for v in point):
                continue
            if not math.isfinite(score) or not 0 <= score <= 1:
                continue
            x = max(0, math.floor(min(p[0] for p in points)))
            y = max(0, math.floor(min(p[1] for p in points)))
            right = min(width, math.ceil(max(p[0] for p in points)))
            bottom = min(height, math.ceil(max(p[1] for p in points)))
            if right <= x or bottom <= y:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        regions.append(OCRRegion(
            text=unicodedata.normalize("NFC", " ".join(text.split())), confidence=score,
            bounding_box=BoundingBox(x=x, y=y, width=right-x, height=bottom-y),
            low_confidence=score < min_confidence,
        ))
    mean = sum(r.confidence for r in regions) / len(regions) if regions else None
    notes = []
    if not regions:
        notes.append("No readable text regions were found.")
    elif any(r.low_confidence for r in regions):
        notes.append("Some recognized regions have low confidence; verify their text.")
    return OCRText(engine="rapidocr-onnx", regions=regions,
        raw_text="\n".join(r.text for r in regions), mean_confidence=mean,
        status="empty" if not regions else "low_confidence" if mean < min_confidence else "ok",
        warnings=notes)


class DeadlineSession:
    """Apply one image deadline to every ONNX run, including recognition batches."""

    def __init__(self, session, deadline):
        self.session, self.deadline = session, deadline

    def __getattr__(self, name):
        return getattr(self.session, name)

    def run(self, output_names, input_feed):
        from onnxruntime import RunOptions
        remaining = self.deadline() - monotonic()
        if remaining <= 0:
            raise TimeoutError("OCR deadline exceeded")
        options = RunOptions()
        timer = Timer(remaining, lambda: setattr(options, "terminate", True))
        timer.daemon = True
        timer.start()
        try:
            return self.session.run(output_names, input_feed, run_options=options)
        finally:
            timer.cancel()


def create_rapidocr_api(config: OCRConfig):
    detector, recognizer = verify_models(config.model_dir)
    # Imports are lazy: health and mocked tests need neither inference nor models.
    import cv2
    from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR
    cv2.setNumThreads(1)
    api = RapidOCR(params={
        "Global.log_level": "critical",  # Do not log recognized content/library tracebacks.
        "Global.use_cls": False,  # Upright lesson graphics need no orientation model.
        "Global.text_score": 0.0,  # Retain uncertain text for correction/debugging.
        "Global.max_side_len": 2000,
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Det.model_path": str(detector),
        "Det.limit_type": "max",
        "Det.limit_side_len": 960,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.model_path": str(recognizer),
        "Rec.rec_batch_num": 1,
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "EngineConfig.onnxruntime.use_cuda": False,
    })
    # Explicit local paths bypass RapidOCR's downloader. The pinned recognizer
    # embeds its dictionary. Force lazy loaders now so init failures are cached.
    api._load_det_model()
    api._load_rec_model()
    return api


class RapidOCREngine:
    def __init__(self, config: OCRConfig):
        self.config = config
        self.api = create_rapidocr_api(config)
        self._deadline = 0.0
        for component in (self.api.text_det, self.api.text_rec):
            native = component.session
            native.session = DeadlineSession(native.session, lambda: self._deadline)

    def recognize(self, image_bytes: bytes) -> OCRText:
        if len(image_bytes) > self.config.max_bytes:
            raise OCRError("ocr_image_too_large", "Image exceeds the OCR byte limit.", 422)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(image_bytes), formats=["PNG", "JPEG", "WEBP", "GIF"]) as image:
                    width, height = image.size
                    if width * height > self.config.max_pixels:
                        raise OCRError("ocr_image_too_large", "Image exceeds the configured OCR pixel limit.", 422)
                    if getattr(image, "is_animated", False):
                        raise OCRError("ocr_invalid_image", "Animated images are not supported.", 422)
                    rgb = image.convert("RGB")
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise OCRError("ocr_image_too_large", "Image exceeds the decoder safety limit.", 422) from exc
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
            raise OCRError("ocr_invalid_image", "OCR input is not a complete supported image.", 422) from exc
        try:
            self._deadline = monotonic() + self.config.timeout_ms / 1000
            result = self.api(rgb)
            if monotonic() >= self._deadline:
                raise TimeoutError("OCR deadline exceeded")
            return normalize_rapidocr(result, width, height, self.config.min_confidence)
        except Exception as exc:
            if isinstance(exc, TimeoutError) or monotonic() >= self._deadline:
                raise OCRError("ocr_recognition_failed", "OCR exceeded its processing deadline.", 504) from exc
            raise
        finally:
            rgb.close()

    def close(self):
        # Releasing the API releases its ONNX sessions; no request data is cached.
        self.api = None
