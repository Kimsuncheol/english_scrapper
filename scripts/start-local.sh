#!/bin/sh
set -eu

# Local development only. Render continues to use scripts/start.sh.
cd "$(dirname "$0")/.."
export FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-english-scrapper}"
export FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
export GOOGLE_APPLICATION_CREDENTIALS="${GOOGLE_APPLICATION_CREDENTIALS:-$PWD/secrets/english-scrapper-service-account.json}"
export ENABLE_TEST_SITE="${ENABLE_TEST_SITE:-true}"
export SCRAPE_ENABLED="${SCRAPE_ENABLED:-false}"
export SCRAPE_IN_PROCESS=false
if [ -z "${OLLAMA_API_KEY:-}" ] && [ -f "$PWD/secrets/ollama-api-key" ]; then
    export OLLAMA_API_KEY_FILE="${OLLAMA_API_KEY_FILE:-$PWD/secrets/ollama-api-key}"
fi
exec "$PWD/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "${LOCAL_PORT:-8765}" --workers 1
