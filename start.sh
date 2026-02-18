#!/usr/bin/env bash
set -e

# Render provides PORT. Use it.
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
