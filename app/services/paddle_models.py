"""Pinned official PaddleOCR inference archives and extracted model checksums."""

from hashlib import sha256
from pathlib import Path

BASE_URL = "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
MODELS = {
    "PP-OCRv5_mobile_det": {
        "archive_sha256": "781056046c9ed77a15c94681605db6a0f62317c2e9cce6931c71da2478d4bc30",
        "files": {
            "inference.yml": "98069072e1b6b37d727fd9d9f11725faa46d6ea0de012f2ed26caea011c37699",
            "inference.onnx": "a431985659dc921974177a95adcfbb90fd9e51989a5e04d70d0b75f597b6e61d",
        },
    },
    "korean_PP-OCRv5_mobile_rec": {
        "archive_sha256": "568ed8b43a260adc9f484d92105e425ea8cddf8ce16940c177bc12864cfb0eb0",
        "files": {
            "inference.yml": "f757fa1c40e99edcf27e9cce879b93eb2a51fa46f5ef39095689b8c37dd75998",
            "inference.onnx": "92f0b7785e64fc9090106a241cf4c1eb97472824558272751b88a2a4476d3a08",
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
