"""Pinned upstream artifacts: downloaded only by the explicit setup/build script.

Source: RapidOCR 3.10.0 default_models.yaml. Models originate from PaddleOCR
(Apache-2.0); see docs/ocr-models.md for attribution and license links.
"""

from hashlib import sha256
from pathlib import Path

MODEL_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5"
MODELS = {
    "ch_PP-OCRv5_det_mobile.onnx": (
        f"{MODEL_BASE}/det/ch_PP-OCRv5_det_mobile.onnx",
        "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    ),
    "korean_PP-OCRv5_rec_mobile.onnx": (
        f"{MODEL_BASE}/rec/korean_PP-OCRv5_rec_mobile.onnx",
        "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b",
    ),
}


def verify_models(directory: str | Path) -> list[Path]:
    paths = []
    for filename, (_, expected) in MODELS.items():
        path = Path(directory) / filename
        if not path.is_file():
            raise RuntimeError("Missing OCR models; run scripts/prepare_ocr_models.py during setup/build")
        with path.open("rb") as stream:
            digest = sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("OCR model checksum mismatch; reprovision the pinned models")
        paths.append(path)
    return paths
