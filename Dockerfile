# ==========================================
# Stage 1: Build & Dependencies
# ==========================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv directly from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project definition
COPY pyproject.toml .

# Install dependencies using uv into isolated virtualenv
ENV UV_COMPILE_BYTECODE=1
RUN uv venv /app/.venv && uv pip install --no-cache -r pyproject.toml

# ==========================================
# Stage 2: Minimal Production Runtime (<150MB base)
# ==========================================
FROM python:3.11-slim AS runner

WORKDIR /app

# Security: run as non-root user
RUN groupadd -r sentinel && useradd -r -g sentinel sentinel && \
    mkdir -p /app/data && chown -R sentinel:sentinel /app

# Copy virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV SENTINELGATE_DB="/app/data/sentinelgate.db"

# Copy application source code and configuration
COPY app /app/app
COPY main.py dashboard.html config.yaml /app/

RUN chown -R sentinel:sentinel /app

USER sentinel

VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
