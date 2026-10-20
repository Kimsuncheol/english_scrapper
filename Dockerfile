FROM python:3.12-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ pkg-config libtesseract-dev libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
# Compile the binding against Debian's runtime libraries, not bundled wheel libs.
RUN pip wheel --no-cache-dir --wheel-dir /wheels --no-binary=tesserocr -r requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OMP_THREAD_LIMIT=1 TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    libtesseract5 liblept5 tesseract-ocr-eng tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY app ./app
# Fail the build if either language or the native binding is unusable.
RUN python -c 'from app.services.ocr import OCRService; s = OCRService(); assert s.initialize(), "OCR initialization failed"; s.close()'
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
