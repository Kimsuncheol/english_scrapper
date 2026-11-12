"""Pinned official PaddleOCR inference archives and extracted model checksums."""

from hashlib import sha256
from pathlib import Path

BASE_URL = "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
MODELS = {
    "PP-OCRv5_mobile_det": {
        "archive_sha256": "50446e5d01ac2a73d5319c89513281f6578414c888c602f9af13f93feefffc58",
        "files": {
            "inference.yml": "98069072e1b6b37d727fd9d9f11725faa46d6ea0de012f2ed26caea011c37699",
            "inference.json": "05feef1acb00aa4cd7362b15f7f501fc4f99d7b1fa73c1c871e0c7b1504b0f5c",
            "inference.pdiparams": "afa1820cb16c1fd0dad589d0f8b389139061c1ef6d68019685fd07be997dda5b",
        },
    },
    "korean_PP-OCRv5_mobile_rec": {
        "archive_sha256": "a6261f800ad335aa6ef77b6f7c4dcedfd5e11b95f4d70982d41ee31c2ace5e66",
        "files": {
            "inference.yml": "f757fa1c40e99edcf27e9cce879b93eb2a51fa46f5ef39095689b8c37dd75998",
            "inference.json": "562404e3c590c50c93778d5f0a94df21b47b5ab8f3ea6d47c7f8a7930c3bc844",
            "inference.pdiparams": "cac3e5f12cf04aaa77f6a5bc704e4e736ef2908476551891d84b41b4e9090462",
        },
    },
}


def verify_models(directory):
    paths = []
    for name, spec in MODELS.items():
        path = Path(directory) / name
        for filename, expected in spec["files"].items():
            model = path / filename
            if not model.is_file():
                raise RuntimeError("Missing PaddleOCR models; run scripts/prepare_paddleocr_models.py")
            if sha256(model.read_bytes()).hexdigest() != expected:
                raise RuntimeError("PaddleOCR model checksum mismatch; reprovision models")
        paths.append(path)
    return paths
