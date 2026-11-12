FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OCR_BACKEND=paddleocr OCR_MODEL_DIR=/app/.ocr-models/paddleocr \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY app ./app
COPY scripts/start.sh ./scripts/start.sh
COPY scripts/prepare_paddleocr_models.py ./scripts/prepare_paddleocr_models.py
COPY scripts/check_ocr.py ./scripts/check_ocr.py
COPY docs/licenses ./docs/licenses
# Pin and verify models at build time; serving requests never downloads weights.
RUN python scripts/prepare_paddleocr_models.py --directory /app/.ocr-models/paddleocr \
    && python -c 'from app.services.ocr import OCRConfig; from app.services.paddle_ocr import create_paddleocr_api; create_paddleocr_api(OCRConfig.from_env())' \
    && python scripts/check_ocr.py
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "/app/scripts/start.sh"]
