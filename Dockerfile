FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY app ./app
COPY scripts/start.sh ./scripts/start.sh
# Ollama Cloud runs inference remotely. No local OCR packages, weights, GPU,
# model downloads, or API key are needed to build the image or serve /health.
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "/app/scripts/start.sh"]
