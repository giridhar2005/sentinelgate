# Contributing to SentinelGate

Thank you for your interest in contributing to SentinelGate.

SentinelGate is engineered with a strict architectural philosophy: **Zero Bloat, In-Process State, and Sub-Millisecond Critical Path Overhead**. Every contribution must preserve these core invariants.

---

## The Zero-Bloat Constraint

Before opening a pull request, please review our non-negotiable architectural rules:

1. **No External Infrastructure Dependencies:**
   - Do NOT add Redis, Celery, PostgreSQL, SQLAlchemy, RabbitMQ, or vector database sidecars.
   - All persistence, metrics, and semantic caching are handled strictly by in-process SQLite (WAL mode) and local `fastembed` ONNX models.
2. **No Frontend Build Steps:**
   - The `/dashboard` HUD must remain a single, auditable template served directly by FastAPI without Node.js, Webpack, Vite, or `node_modules`.
3. **Sub-Millisecond Pre-Flight Path:**
   - Guardrails, complexity classification, and key validation must complete in `<0.5ms` on standard hardware.

Pull requests that violate these architectural principles will be closed.

---

## Local Development Workflow

SentinelGate uses [`uv`](https://github.com/astral-sh/uv) for fast, deterministic dependency resolution.

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/giridhar2005/sentinelgate.git
cd sentinelgate

# Install dependencies into .venv
uv sync

# Install package in editable mode
uv pip install -e .
```

### 2. Run the Gateway Locally

```bash
# Via console script
uv run sentinelgate --port 8000

# Or directly via module
uv run python -m sentinelgate
```

Open [http://localhost:8000/dashboard](http://localhost:8000/dashboard) to inspect live telemetry.

---

## Running Tests & Linter

Every pull request must pass the offline test suite and Ruff lint checks:

```bash
# Run full pytest suite (36 tests)
uv run pytest -v

# Run Ruff linter and formatter check
uv run ruff check .
```

---

## Running Local Benchmarks

When modifying routing, caching, or guardrail logic, verify that proxy overhead has not regressed:

```bash
uv run python benchmarks/bench.py
```

Attach the terminal benchmark summary table to your pull request description.

---

## Submitting Pull Requests

1. Fork the repository and create your feature branch: `git checkout -b feat/provider-adapter`.
2. Commit with clean, conventional commit messages: `feat(router): add provider adapter for vllm`.
3. Push to your fork and submit a Pull Request targeting `main`.
4. Fill out the [Pull Request Template](.github/pull_request_template.md).
