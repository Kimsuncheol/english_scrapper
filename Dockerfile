FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OMP_THREAD_LIMIT=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    OCR_MODEL_DIR=/app/.ocr-models/rapidocr
# ONNX CPU execution uses OpenMP; headless OpenCV needs no GUI/X11 packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY app ./app
COPY scripts/prepare_ocr_models.py ./scripts/prepare_ocr_models.py
COPY scripts/start.sh ./scripts/start.sh
COPY docs/ocr-models.md ./OCR-MODELS.md
COPY docs/licenses ./licenses
# Download and verify immutable models at BUILD time, never at service startup.
RUN python scripts/prepare_ocr_models.py --directory "$OCR_MODEL_DIR" \
    && python -c 'from app.services.ocr import OCRService; s = OCRService(); assert s.initialize(), "OCR initialization failed"; s.close()'
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "/app/scripts/start.sh"]
