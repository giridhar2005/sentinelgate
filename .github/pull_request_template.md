## Description

Explain the changes proposed in this Pull Request and reference any related issues:
Fixes # (issue)

---

## Architectural Compliance Checklist

- [ ] **Zero Bloat:** No external services (Redis, Celery, Postgres, SQLAlchemy) or JavaScript build pipelines (`node_modules`) have been added.
- [ ] **OpenAI Compatibility:** Maintains drop-in compatibility for official OpenAI Python/Node SDKs (`base_url="http://localhost:8000/v1"`).
- [ ] **Critical Path Latency:** Pre-flight guardrails remain sub-millisecond; no blocking I/O calls introduced in the async event loop.
- [ ] **Test Suite Passing:** All offline tests pass (`uv run pytest -v`).
- [ ] **Code Quality:** All lint and format checks pass (`uv run ruff check .`).
- [ ] **Documentation Updated:** `README.md`, `ARCHITECTURE.md`, and `config.example.yaml` updated if configuration or endpoints changed.

---

## Type of Change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change adding gateway functionality)
- [ ] Performance improvement (latency reduction, memory optimization)
- [ ] Documentation / RFC update

---

## Verification & Testing

Explain how the changes were verified locally:
1. Ran `uv run pytest -v` (attach test summary)
2. Ran `benchmarks/bench.py` (attach overhead latency numbers)
3. Verified `/dashboard` HUD rendering and telemetry updates
