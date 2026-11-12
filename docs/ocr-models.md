# PaddleOCR models and deployment

Production uses **PaddleOCR 3.7.0** and **PaddleX 3.7.2**, with their supported
**ONNX Runtime CPU engine** (1.31.0 on Linux, 1.23.2 on Intel macOS). It explicitly
loads `PP-OCRv5_mobile_det` and `korean_PP-OCRv5_mobile_rec` from official Paddle
ONNX archives. The Korean recognizer includes English and numbers. The public
engine label is `paddleocr`. RapidOCR and native PaddlePaddle are not installed
by production requirements. Historical OCR payloads remain readable.

The native Paddle backend passed Linux build inference but a real Render Free
lesson exceeded its 30-second deadline; the synthetic build check also measured
697 MiB peak per-process RSS. The default uses PaddleOCR's ONNX engine to reduce
resource use, without changing the OCR library or enabling a paid plan.

## Setup

Use Python 3.12. Linux x86-64 is the production target; Intel macOS uses the
ONNX Runtime 1.23.2 wheel. Mocked adapter tests do not require inference packages.

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
| `PP-OCRv5_mobile_det_onnx_infer.tar` | 4,843,520 | `781056046c9ed77a15c94681605db6a0f62317c2e9cce6931c71da2478d4bc30` |
| `korean_PP-OCRv5_mobile_rec_onnx_infer.tar` | 13,537,280 | `568ed8b43a260adc9f484d92105e425ea8cddf8ce16940c177bc12864cfb0eb0` |

Sources: [PaddleOCR recognition models](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md),
[official OCR pipeline](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md).
Archives come from Paddle's `paddle-model-ecology.bj.bcebos.com` host, under
`paddlex/official_inference_model/paddle3.0.0/`. Checksums pin the reviewed
artifacts; they are not an upstream signature. PaddleOCR uses
Apache-2.0; bundled license notices are copied into the Docker image.

## Runtime and deadlines

One request runs at a time. Each image gets a fresh subprocess with CPU threads
set to one, recognition batch size one, detector long side capped at 960 pixels,
and CPU memory arenas, document orientation, unwarping and textline orientation disabled.
The subprocess calls the official `PaddleOCR.predict()` API with
`engine="onnxruntime"` and CPU-only execution providers. Paddle receives BGR
pixels; returned line polygons, text and confidence are normalized into the
existing API schema in original image coordinates.

`OCR_TIMEOUT_MS=60000` covers subprocess startup, imports, model initialization,
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
worker. Use one Uvicorn worker and one concurrent request. PaddleOCR has a
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

A local Intel macOS offline smoke check with the official ONNX archives returned
18 text regions on each synthetic-image pass (3.79 s and 3.05 s, including worker
startup), with 273.2 MiB maximum per-process RSS. These are local observations,
not a Render memory guarantee or an accuracy benchmark.
