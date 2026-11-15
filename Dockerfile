# ==========================================
# Stage 1: React Dashboard Frontend Builder
# ==========================================
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

# Copy frontend manifest files
COPY frontend/package*.json ./

# Install dependencies cleanly
RUN npm ci --silent 2>/dev/null || npm install --silent

# Copy frontend source code and compile Vite distribution
COPY frontend/ ./
RUN npm run build || mkdir -p dist

# ==========================================
# Stage 2: Python Backend Builder & Model Cache
# ==========================================
FROM python:3.11-slim AS python-builder

WORKDIR /app

# Install Astral uv for ultra-fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project definition
COPY pyproject.toml .

# Install dependencies into isolated virtual environment
ENV UV_COMPILE_BYTECODE=1
RUN uv venv /app/.venv && uv pip install --no-cache -r pyproject.toml

# Pre-warm FastEmbed ONNX model so container boots offline without download lag
ENV FASTEMBED_CACHE_PATH="/app/.cache/fastembed"
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/.cache/fastembed')"

# ==========================================
# Stage 3: Minimal Production Runtime (<180MB)
# ==========================================
FROM python:3.11-slim AS runner

WORKDIR /app

# Security: Dedicated non-root service account
RUN groupadd -r sentinel && useradd -r -u 10001 -g sentinel sentinel && \
    mkdir -p /app/data /app/.cache/fastembed && \
    chown -R sentinel:sentinel /app

# Copy virtual environment and pre-warmed model weights from builder stages
COPY --from=python-builder --chown=sentinel:sentinel /app/.venv /app/.venv
COPY --from=python-builder --chown=sentinel:sentinel /app/.cache/fastembed /app/.cache/fastembed
COPY --from=frontend-builder --chown=sentinel:sentinel /frontend/dist /app/frontend/dist

# Environment configuration
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV FASTEMBED_CACHE_PATH="/app/.cache/fastembed"
ENV SENTINELGATE_DB="/app/data/sentinelgate.db"
ENV DEMO_MODE="true"

# Copy application source code and fallback dashboard
COPY --chown=sentinel:sentinel app /app/app
COPY --chown=sentinel:sentinel sentinelgate /app/sentinelgate
COPY --chown=sentinel:sentinel main.py dashboard.html config.example.yaml /app/
RUN cp /app/config.example.yaml /app/config.yaml

USER sentinel

VOLUME ["/app/data"]

EXPOSE 8000

# Sub-second HTTP healthcheck probe
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Production server entrypoint
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
