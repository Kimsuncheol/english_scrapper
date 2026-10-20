#!/bin/sh
set -eu

# Render provides PORT; the fallback is for local container runs.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
