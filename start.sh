#!/usr/bin/env bash
set -e

exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:${PORT:-8000} \
  --workers 1 \
  --threads 2 \
  --timeout 120 \
  --max-requests 300 \
  --max-requests-jitter 50 \
  --log-level info
