FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OCR_BACKEND=rapidocr OCR_MODEL_DIR=/app/.ocr-models/rapidocr \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY app ./app
COPY scripts/start.sh ./scripts/start.sh
COPY scripts/prepare_ocr_models.py ./scripts/prepare_ocr_models.py
COPY docs/licenses ./docs/licenses
# Pin and verify models at build time; serving requests never downloads weights.
RUN python scripts/prepare_ocr_models.py --directory /app/.ocr-models/rapidocr \
    && python -c 'from app.services.ocr import OCRService; s = OCRService(); assert s.initialize(), "OCR initialization failed"; s.close()'
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "/app/scripts/start.sh"]
