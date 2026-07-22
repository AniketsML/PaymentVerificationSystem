#!/usr/bin/env sh
# ──────────────────────────────────────────────────────────────────────────────
#  Payment Verification System — one command to run everything (Linux / macOS).
#
#    ./run.sh            start the console + workers (+ ingestion if configured)
#    ./run.sh --check    preflight checks only
#    ./run.sh --port 8010
#
#  Uses uv when available (no venv setup needed), else python3.
# ──────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    exec uv run --with-requirements requirements.txt python run.py "$@"
elif [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python run.py "$@"
else
    exec python3 run.py "$@"
fi
