#!/usr/bin/env bash
# Saddle's single gate: lint, format, types, tests with 100% coverage.
# CI runs exactly this script (plus the time-boxed mutation job).
set -euo pipefail
cd "$(dirname "$0")"
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
