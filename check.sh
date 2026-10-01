#!/usr/bin/env bash
# Saddle's single gate: lint, format, types, tests with 100% coverage.
# CI runs exactly this script (plus the time-boxed mutation job).
set -euo pipefail
cd "$(dirname "$0")"
# The JavaScript, HTML, CSS and Markdown checkers are development-only npm
# packages pinned by package-lock.json. Install them when they are missing or
# older than the lockfile; --ignore-scripts so no package runs code on install.
if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
    npm ci --ignore-scripts --no-audit --no-fund
fi
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
# CI workflows lint clean (shellcheck included) and uv.lock matches pyproject.toml.
uv run actionlint .github/workflows/*.yml
uv lock --check
# The suite runs on as many pytest-xdist workers as the audit uses on this repo
# (`[tool.saddle] test-workers` in pyproject.toml); pytest-cov combines every
# worker's lines for the 100% line and branch gate in `addopts`. `worksteal`
# lets an idle worker take tests still queued on a busy one, as the audit does.
workers=$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["tool"]["saddle"]["test-workers"])')
uv run pytest -n "$workers" --dist worksteal
