#!/usr/bin/env bash
set -e

exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:${PORT:-10000} \
  --workers 1 \
  --threads 2 \
  --timeout 900 \
  --graceful-timeout 60 \
  --keep-alive 30 \
  --max-requests 300 \
  --max-requests-jitter 50 \
  --log-level info
