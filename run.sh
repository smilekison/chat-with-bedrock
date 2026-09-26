#!/usr/bin/env bash
set -e
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
