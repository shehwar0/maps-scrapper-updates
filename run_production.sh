#!/bin/bash
# Production-ready runner for the Scraper
# Usage: ./run_production.sh [port]

set -e

PORT=${1:-5001}

echo "=== Starting Production Lead Scraper ==="
echo "Port: $PORT"
echo "Headless recommended for production runs."

# Activate venv if present
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo "Virtualenv activated."
fi

# Ensure playwright browsers
python -m playwright install chromium --with-deps || true

# Run with production settings
export APP_PORT=$PORT
# Optional: load .env if present
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

python backend/app.py
