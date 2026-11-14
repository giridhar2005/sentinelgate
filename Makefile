.PHONY: help install dev run test bench lint format clean

# Default target
help:
	@echo "SentinelGate Developer Commands:"
	@echo "  make install     Install package in editable mode with development dependencies"
	@echo "  make dev         Run SentinelGate with hot-reloading on port 8000"
	@echo "  make run         Run SentinelGate in production mode"
	@echo "  make test        Execute full pytest suite"
	@echo "  make bench       Run local latency and cache benchmark script"
	@echo "  make lint        Run Ruff linter"
	@echo "  make format      Auto-format codebase using Ruff"
	@echo "  make clean       Remove temporary caches, SQLite SHM/WAL files, and build artifacts"

install:
	pip install -e ".[dev]"

dev:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

run:
	uv run python -m sentinelgate --host 0.0.0.0 --port 8000

test:
	uv run pytest -v

bench:
	uv run python benchmarks/bench.py

lint:
	uv run ruff check .

format:
	uv run ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ */__pycache__ */*/__pycache__ *.egg-info build dist
	rm -f sentinelgate.db-shm sentinelgate.db-wal
