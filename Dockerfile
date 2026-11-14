# ==========================================
# Stage 1: Build & Dependencies
# ==========================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv directly from official binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project definition
COPY pyproject.toml .

# Install dependencies into virtualenv using uv
ENV UV_COMPILE_BYTECODE=1
RUN uv venv /app/.venv && uv pip install --no-cache -r pyproject.toml

# ==========================================
# Stage 2: Minimal Production Runtime
# ==========================================
FROM python:3.11-slim AS runner

WORKDIR /app

# Create non-root user
RUN groupadd -r sentinel && useradd -r -g sentinel sentinel

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy source code and static assets
COPY main.py router.py cache.py guardrails.py evals.py dashboard.html config.yaml ./

# Permissions
RUN chown -R sentinel:sentinel /app

USER sentinel

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
