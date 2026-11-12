# PaddleOCR models and deployment

Production uses **PaddleOCR 3.7.0**, **PaddleX 3.7.2**, and CPU-only
**PaddlePaddle 3.3.1**. It explicitly loads `PP-OCRv5_mobile_det` and
`korean_PP-OCRv5_mobile_rec`. The Korean recognizer includes English and numbers.
The response engine label is `paddleocr`; no RapidOCR or ONNX Runtime package is
installed by production requirements. Historical RapidOCR/Tesseract payloads
remain readable and their adapters remain available for compatibility tests.

## Setup

Use Python 3.12 on Linux x86-64 (production) or a platform with an official
PaddlePaddle wheel, such as Apple Silicon macOS. PaddlePaddle 3.3.1 does not
publish Intel macOS wheels; native inference on an Intel Mac requires a supported
Linux environment. Mocked adapter tests do not need PaddlePaddle installed.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/prepare_paddleocr_models.py
export OCR_BACKEND=paddleocr
export OCR_MODEL_DIR="$PWD/.ocr-models/paddleocr"
.venv/bin/python scripts/check_ocr.py
```

The setup script downloads two official PaddleOCR archives. Their SHA-256 values
are pinned in `app/services/paddle_models.py`, along with checksums for every
extracted inference file. Setup reads only the expected regular files, skips
archive caches and links, and verifies all files before publishing a model.
Correct local files are reused. Missing or corrupt models fail initialization;
requests do not fall back to a download, a different model, or English-only OCR.

| Official archive | Bytes | SHA-256 |
| --- | ---: | --- |
| `PP-OCRv5_mobile_det_infer.tar` | 4,935,680 | `50446e5d01ac2a73d5319c89513281f6578414c888c602f9af13f93feefffc58` |
| `korean_PP-OCRv5_mobile_rec_infer.tar` | 13,680,640 | `a6261f800ad335aa6ef77b6f7c4dcedfd5e11b95f4d70982d41ee31c2ace5e66` |

Sources: [PaddleOCR recognition models](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md),
[official OCR pipeline](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md).
Archives come from Paddle's `paddle-model-ecology.bj.bcebos.com` host, under
`paddlex/official_inference_model/paddle3.0.0/`. Checksums pin the reviewed
artifacts; they are not an upstream signature. PaddleOCR and PaddlePaddle use
Apache-2.0; bundled license notices are copied into the Docker image.

## Runtime and deadlines

One request runs at a time. Each image gets a fresh subprocess with CPU threads
set to one, recognition batch size one, detector long side capped at 960 pixels,
and MKL-DNN, document orientation, unwarping and textline orientation disabled.
The subprocess calls the official `PaddleOCR.predict()` API. Paddle receives BGR
pixels; returned line polygons, text and confidence are normalized into the
existing API schema in original image coordinates.

`OCR_TIMEOUT_MS=30000` covers subprocess startup, imports, model initialization,
recognition and result serialization. Timeout kills and reaps the child before
the shared OCR lock is released, returning `504 ocr_recognition_failed`. Native
initialization errors return `503 ocr_unavailable`; inference crashes return
`500 ocr_failed`. Dependency messages and recognized text are not logged.
The subprocess blocks sockets and disables PaddleX's model-host connectivity
check. Images travel over stdin and normalized JSON over stdout; no request
images are written to disk. Native model memory is released when the child exits.

Docker installs libgomp1, libgl1 and libglib2.0-0, provisions verified models under
`/app/.ocr-models/paddleocr`, and runs the offline real-inference smoke check
before publishing the image. `/health` does not initialize PaddleOCR or spawn a
worker. Use one Uvicorn worker and one concurrent request. Native Paddle has a
different memory profile from RapidOCR: build success does not prove that all
images fit Render Free memory. No paid plan is enabled by this change.

## Results and verification

Low-confidence text stays visible; blank results remain empty. The existing
lesson parser assigns fields from text and geometry. Changing the runtime does
not establish better recognition accuracy; compare human-checked lesson images
before claiming an improvement. Saved URLs continue to reuse existing results;
explicit reprocessing preserves the prior Firestore revision.

```sh
# Offline, mocked adapter and pipeline coverage:
ENABLE_TEST_SITE=false SCRAPE_ENABLED=false \
  .venv/bin/python -m pytest tests/test_paddle_ocr.py tests/test_ocr.py -q
# Real CPU inference on an original synthetic bilingual lesson, sockets blocked:
.venv/bin/python scripts/check_ocr.py
```

The smoke check runs recognition twice. Its RSS figure is the maximum peak of
one parent or child process, not combined service/container memory.
