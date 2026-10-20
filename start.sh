#!/bin/sh
set -eu
cd "$(dirname "$0")"
NGS_RUNTIME_PYTHON="${NGS_RUNTIME_PYTHON:-$(pwd)/.venv/bin/python}"
if [ ! -x "$NGS_RUNTIME_PYTHON" ]; then
  NGS_RUNTIME_PYTHON="$(pwd)/.venv/bin/python"
  if [ ! -x "$NGS_RUNTIME_PYTHON" ]; then
    python3.13 -m venv .venv
    "$NGS_RUNTIME_PYTHON" -m pip install -r requirements.lock
  fi
fi
exec "$NGS_RUNTIME_PYTHON" -m uvicorn backend.api:app --host 127.0.0.1 --port "${NGS_PORT:-8766}" --no-access-log
