"""PaddleOCR CPU inference, isolated behind a killable per-image worker."""

from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

from PIL import Image, UnidentifiedImageError

from app.schemas import OCRText
from app.services.errors import OCRError


def decode_image(data, config):
    if len(data) > config.max_bytes:
        raise OCRError("ocr_image_too_large", "Image exceeds the OCR byte limit.", 422)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=["PNG", "JPEG", "WEBP", "GIF"]) as image:
                if image.width * image.height > config.max_pixels:
                    raise OCRError("ocr_image_too_large", "Image exceeds the OCR pixel limit.", 422)
                if getattr(image, "is_animated", False):
                    raise OCRError("ocr_invalid_image", "Animated images are not supported.", 422)
                return image.convert("RGB")
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise OCRError("ocr_image_too_large", "Image exceeds the decoder safety limit.", 422) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise OCRError("ocr_invalid_image", "OCR input is not a complete supported image.", 422) from exc


def create_paddleocr_api(config):
    from app.services.paddle_models import verify_models
    detector, recognizer = verify_models(config.model_dir)
    from paddleocr import PaddleOCR
    return PaddleOCR(
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_detection_model_dir=str(detector),
        text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
        text_recognition_model_dir=str(recognizer),
        device="cpu", engine="onnxruntime", cpu_threads=1,
        engine_config={"providers": ["CPUExecutionProvider"],
                       "intra_op_num_threads": 1, "inter_op_num_threads": 1,
                       "enable_cpu_mem_arena": False},
        enable_mkldnn=False, enable_hpi=False,
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, text_recognition_batch_size=1,
        text_det_limit_side_len=960, text_det_limit_type="max",
        text_rec_score_thresh=0.0,
    )


def normalize_paddleocr(result, width, height, min_confidence=0.5):
    from app.services.ocr_regions import normalize_regions
    try:
        return normalize_regions(result["rec_polys"], result["rec_texts"],
                                 result["rec_scores"], width, height,
                                 min_confidence, engine="paddleocr")
    except (KeyError, TypeError) as exc:
        raise OCRError("ocr_failed", "PaddleOCR returned an unsupported result format.", 500) from exc



class PaddleOCREngine:
    def __init__(self, config):
        from app.services.paddle_models import verify_models
        verify_models(config.model_dir)
        self.config = config

    def recognize(self, image_bytes):
        # Validate before launching the native runtime. Images travel only over
        # stdin; no temporary request files or recognition content are logged.
        with decode_image(image_bytes, self.config):
            pass
        env = {**os.environ, "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1"}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "app.services.paddle_worker", json.dumps(asdict(self.config))],
                input=image_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=self.config.timeout_ms / 1000, check=False, env=env,
                cwd=Path(__file__).resolve().parents[2],
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills AND reaps the child before raising, so the
            # shared OCR lock is never released while native inference survives.
            raise OCRError("ocr_recognition_failed", "PaddleOCR exceeded its processing deadline.", 504) from exc
        if completed.returncode == 3:
            raise OCRError("ocr_unavailable", "PaddleOCR could not initialize. Check CPU dependencies and Korean/English models.", 503)
        if completed.returncode != 0:
            raise OCRError("ocr_failed", "PaddleOCR could not process the lesson image.", 500)
        try:
            return OCRText.model_validate_json(completed.stdout)
        except ValueError as exc:
            raise OCRError("ocr_failed", "PaddleOCR returned an invalid result.", 500) from exc

    def close(self):
        # Each request reaps its worker; no native runtime survives between calls.
        pass
