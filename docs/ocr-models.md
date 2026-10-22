# RapidOCR models and deployment

The default OCR adapter is **RapidOCR 3.10.0 + ONNX Runtime 1.31.0**, CPU only.
It explicitly selects PP-OCRv5 mobile detection and Korean recognition; the
Korean recognizer also supports English and numbers. It does not use RapidOCR's
default Chinese recognizer or a GPU. Recognition quality has not been benchmarked
against a human-transcribed set of publisher lessons.

## Provision once

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/prepare_ocr_models.py
export OCR_MODEL_DIR="$PWD/.ocr-models/rapidocr"
sh scripts/start-local.sh
```

The setup script downloads these pinned artifacts from RapidAI's ModelScope
repository and verifies their upstream SHA-256 checksums. Existing correct files
are reused; missing/corrupt models cause initialization failure at runtime, with
no fallback download or silent switch to English-only recognition.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `ch_PP-OCRv5_det_mobile.onnx` | 4,819,576 | `4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae` |
| `korean_PP-OCRv5_rec_mobile.onnx` | 13,488,748 | `cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b` |

The combined weights are **18,308,324 bytes (17.46 MiB)**. The recognizer embeds
its character dictionary. No orientation/classification model is loaded because
the service expects upright lesson graphics.

Artifact provenance:
[RapidOCR 3.10.0 model manifest](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml),
[PaddleOCR recognition models](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/module_usage/text_recognition.en.md),
[RapidOCR model documentation](https://rapidai.github.io/RapidOCRDocs/main/en/model_list/).

RapidOCR and PaddleOCR use Apache-2.0; ONNX Runtime uses MIT. Copyright/license
notices are retained in `docs/licenses/` and copied into the Docker image.
Models originate from PaddleOCR and are converted/distributed by RapidAI.
See [RapidOCR](https://github.com/RapidAI/RapidOCR),
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), and
[ONNX Runtime](https://github.com/microsoft/onnxruntime).
No paid OCR API or API key is required. Hosting/database costs are separate.

## Render Free

Use the repository Dockerfile and `scripts/start.sh` (one Uvicorn worker).
The only explicitly installed Debian runtime packages are `libgomp1` and
`ca-certificates`; headless OpenCV needs no GUI packages. Python uses pinned CPU
ONNX wheels, not PaddlePaddle/PyTorch or Tesseract system packages. The build
provisions the two models at `/app/.ocr-models/rapidocr` and checks initialization.
Set `OCR_MODEL_DIR=/app/.ocr-models/rapidocr` (already in Docker/Blueprint).
No model download runs during service startup, `/health`, or requests. Immutable
weights live in the deployed image; request pixels are kept in memory. Saved
lessons remain in Firestore. Writable Render storage is never required.

Model files are not total RAM use: ONNX Runtime, OpenCV, buffers, recognition
activations, FastAPI and Firestore consume additional memory. Use one request at
a time, single-threaded ONNX execution, recognition batch size 1, detector long
side capped at 960 pixels, and disabled CPU memory arenas. RapidOCR may resize
its working image to a 2000-pixel long side, but maps returned boxes back to the
original input coordinates. Inputs still have the existing 4,000,000-pixel and
10 MiB limits.

OCR initializes once per process on the first OCR request; health does not load
models. Render Free sleep/restart therefore adds model initialization to the
first new scrape. There is no guaranteed startup or inference duration on shared
Free CPU. `OCR_TIMEOUT_MS=30000` applies across all ONNX inference batches using
cooperative cancellation. Image decoding, model initialization, and Python
pre/post-processing cannot be forcibly interrupted by ONNX cancellation; this
is not a hard process deadline. A timeout returns `504 ocr_recognition_failed`.
The engine lock remains held until work exits; a timed-out native operation is
never left running alongside a second image.

A local macOS smoke test with networking blocked recognized Korean and English
from the original synthetic 720 × 1800 fixture twice: approximately 10.5 seconds
for first initialization, 0.85/0.79 seconds per image, and 380 MiB peak process RSS.
A repeat in a fresh process measured 0.93-second initialization, 0.86/0.80-second
recognition, and 387 MiB peak RSS; local caches affect these observations.
This includes local installed dependencies and is **not a Render/Linux memory
measurement, an accuracy benchmark, or proof of fit within Free memory**. Use
`scripts/check_ocr.py` after provisioning to repeat an offline check. Linux x86-64 Python 3.12 wheels were resolved for all pinned dependencies, but
no Docker runtime was available for local container execution during this change. If
Render runs out of memory, lower input limits or use separately hosted OCR;
do not add more workers. No paid service or larger plan is configured here.

## Result compatibility and saved lessons

New results identify `ocr.engine` as `rapidocr-onnx`. Regions represent detected
text lines (rather than Tesseract words), with 0–1 engine confidence and enclosing
axis-aligned boxes in original image pixels. Combined raw text follows the
engine's reading order. The existing geometry-based lesson parser and reading
view consume the same response schema. Low-confidence lines stay visible and
flagged; empty recognition returns `status: empty`. Scores from different engines
are not calibrated or directly comparable.

The `OCREngine` interface, singleton lock, initialization-failure caching, and
sanitized application errors are unchanged. The legacy Tesseract adapter and
TSV normalizer remain available for compatibility tests; Tesseract is not installed
or selected by the deployment. The schema's legacy default `engine: tesseract`
is retained to interpret historical records without an engine label; the new
adapter always writes its explicit engine label.

Existing saved results are intentionally reused by canonical URL. They are not
automatically overwritten, deleted, or rerun when the engine changes. This change
improves the processing path for new lessons; upgrading previously saved OCR
requires a separate, explicit reprocessing operation.

## Checks

```sh
# Deterministic tests: mock engine outputs, no model/network requirement.
ENABLE_TEST_SITE=false SCRAPE_ENABLED=false \
  .venv/bin/python -m pytest tests/test_rapid_ocr.py tests/test_ocr.py -q
# Optional real inference: synthetic local image, sockets blocked, no downloads.
.venv/bin/python scripts/check_ocr.py
```
