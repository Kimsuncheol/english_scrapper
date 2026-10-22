"""Replaceable OCR interface, one initialized engine per process, no files."""

import atexit
import csv
from dataclasses import dataclass
from io import BytesIO, StringIO
import logging
import math
import os
from pathlib import Path
from threading import Lock
from typing import Callable, Protocol
import unicodedata
import warnings

import anyio
from PIL import Image, UnidentifiedImageError

from app.schemas import BoundingBox, OCRRegion, OCRText
from app.services.errors import ArticleFetchError

logger = logging.getLogger(__name__)

# cysignals (used by current tesserocr) registers signal handlers on import.
# Import the binding on the ASGI main thread, before any worker-thread use.
# This does not initialize an OCR engine or load language models.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
_binding_error = None
try:
    import tesserocr as _tesserocr
except (ImportError, OSError, RuntimeError, ValueError) as exc:
    _tesserocr = None
    _binding_error = exc


class OCRError(ArticleFetchError):
    """OCR failures use the existing application error envelope."""


@dataclass(frozen=True)
class OCRConfig:
    tessdata_dir: str | None = None
    model_dir: str = ".ocr-models/rapidocr"
    min_confidence: float = 0.5
    max_pixels: int = 4_000_000
    max_bytes: int = 10 * 1024 * 1024
    timeout_ms: int = 30_000

    def __post_init__(self):
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError("OCR confidence threshold must be between 0 and 1")
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (self.max_pixels, self.max_bytes, self.timeout_ms)):
            raise ValueError("OCR resource limits must be positive integers")

    @classmethod
    def from_env(cls):
        return cls(
            tessdata_dir=os.getenv("TESSDATA_PREFIX"),
            model_dir=os.getenv("OCR_MODEL_DIR", ".ocr-models/rapidocr"),
            min_confidence=float(os.getenv("OCR_MIN_CONFIDENCE", "0.5")),
            max_pixels=int(os.getenv("OCR_MAX_PIXELS", "4000000")),
            max_bytes=int(os.getenv("OCR_MAX_BYTES", str(10 * 1024 * 1024))),
            timeout_ms=int(os.getenv("OCR_TIMEOUT_MS", "30000")),
        )


class OCREngine(Protocol):
    def recognize(self, image_bytes: bytes) -> OCRText: ...
    def close(self) -> None: ...


def normalize_tesseract_tsv(tsv: str, width: int, height: int, min_confidence: float = 0.5) -> OCRText:
    """Normalize word regions and reading order, keeping uncertain text visible."""
    columns = ["level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width", "height", "conf", "text"]
    required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
    first = tsv.split("\n", 1)[0].split("\t")
    # Native GetTSVText omits the header; CLI exports include it.
    headerless = len(first) == len(columns) and first[0] in {"1", "2", "3", "4", "5"}
    reader = csv.DictReader(StringIO(tsv), fieldnames=columns if headerless else None,
        delimiter="\t", quoting=csv.QUOTE_NONE)
    if reader.fieldnames and not required.issubset(reader.fieldnames):
        raise OCRError("ocr_failed", "OCR engine returned an unsupported result format.", 500)
    regions = []
    lines = {}
    for row in reader:
        if row.get("level") != "5":
            continue
        raw_text = row.get("text")
        if not isinstance(raw_text, str):
            continue
        text = unicodedata.normalize("NFC", " ".join(raw_text.split()))
        if not text:
            continue
        try:
            confidence = float(row["conf"]) / 100
            x, y, w, h = (int(row[key]) for key in ("left", "top", "width", "height"))
            if not math.isfinite(confidence) or not 0 <= confidence <= 1 or min(x, y) < 0 or min(w, h) <= 0:
                continue
            w, h = min(w, width - x), min(h, height - y)
            if min(w, h) <= 0:
                continue
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        regions.append(OCRRegion(text=text, confidence=confidence,
            bounding_box=BoundingBox(x=x, y=y, width=w, height=h),
            low_confidence=confidence < min_confidence))
        line = tuple(row.get(key) for key in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(line, []).append(text)
    mean = sum(region.confidence for region in regions) / len(regions) if regions else None
    status = "empty" if not regions else "low_confidence" if mean < min_confidence else "ok"
    notes = []
    if not regions:
        notes.append("No readable text regions were found.")
    elif any(region.low_confidence for region in regions):
        notes.append("Some recognized regions have low confidence; verify their text.")
    return OCRText(raw_text="\n".join(" ".join(words) for words in lines.values()),
        regions=regions, mean_confidence=mean, status=status, warnings=notes)


def create_tesseract_api(config: OCRConfig):
    # Set before importing the native runtime. Deployment sets this explicitly.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    candidates = [config.tessdata_dir] if config.tessdata_dir else [
        "/usr/share/tesseract-ocr/5/tessdata", "/opt/homebrew/share/tessdata",
        "/usr/local/share/tessdata", "/usr/share/tessdata",
    ]
    path = next((Path(path) for path in candidates if path and all(
        (Path(path) / f"{language}.traineddata").is_file() for language in ("kor", "eng")
    )), None)
    if path is None:
        raise RuntimeError("Install both kor and eng traineddata; set TESSDATA_PREFIX to their directory")
    if _tesserocr is None:
        raise RuntimeError("Tesseract native binding is unavailable") from _binding_error
    return _tesserocr.PyTessBaseAPI(path=str(path), lang="kor+eng", oem=_tesserocr.OEM.LSTM_ONLY, psm=_tesserocr.PSM.AUTO)


class TesseractEngine:
    def __init__(self, config: OCRConfig):
        self.config = config
        self.api = create_tesseract_api(config)

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
                    # Pillow and tesserocr pass pixel buffers directly; no temp file.
                    with image.convert("RGB") as rgb:
                        self.api.SetImage(rgb)
                        if not self.api.Recognize(timeout=self.config.timeout_ms):
                            raise OCRError("ocr_recognition_failed", "OCR failed or exceeded its processing deadline.", 504)
                        tsv = self.api.GetTSVText(0)
            return normalize_tesseract_tsv(tsv, width, height, self.config.min_confidence)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise OCRError("ocr_image_too_large", "Image exceeds the decoder safety limit.", 422) from exc
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
            raise OCRError("ocr_invalid_image", "OCR input is not a complete supported image.", 422) from exc
        finally:
            # Clear request pixels/results while retaining initialized languages.
            self.api.Clear()
            self.api.ClearAdaptiveClassifier()

    def close(self):
        self.api.End()


class OCRService:
    def __init__(self, factory: Callable[[], OCREngine] | None = None):
        self.factory = factory or create_rapidocr_engine
        self._lock = Lock()
        self._initialized = False
        self._engine = None

    def _initialize_locked(self):
        if not self._initialized:
            self._initialized = True
            try:
                self._engine = self.factory()
            except Exception as exc:
                logger.error("OCR initialization failed (%s); check OCR dependencies and provisioned models", type(exc).__name__)

    def initialize(self) -> bool:
        with self._lock:
            self._initialize_locked()
            return self._engine is not None

    def _recognize(self, image_bytes: bytes) -> OCRText:
        if not self._lock.acquire(blocking=False):
            raise OCRError("ocr_busy", "OCR is processing another image. Retry later.", 503)
        try:
            self._initialize_locked()
            if self._engine is None:
                raise OCRError("ocr_unavailable", "OCR initialization failed. Check server dependencies and provisioned Korean/English models.", 503)
            try:
                return self._engine.recognize(image_bytes)
            except OCRError:
                raise
            except Exception as exc:
                logger.error("OCR recognition failed (%s)", type(exc).__name__)
                raise OCRError("ocr_failed", "OCR could not process the lesson image.", 500) from exc
        finally:
            self._lock.release()

    async def recognize(self, image_bytes: bytes) -> OCRText:
        return await anyio.to_thread.run_sync(self._recognize, image_bytes)

    def close(self):
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
            self._initialized = True


def create_rapidocr_engine():
    # Heavy imports and model loading remain off the ASGI/health path.
    from app.services.rapid_ocr import RapidOCREngine
    return RapidOCREngine(OCRConfig.from_env())


ocr_service = OCRService()
atexit.register(ocr_service.close)


async def initialize_ocr() -> bool:
    return await anyio.to_thread.run_sync(ocr_service.initialize)


async def recognize_image(image_bytes: bytes) -> OCRText:
    return await ocr_service.recognize(image_bytes)
