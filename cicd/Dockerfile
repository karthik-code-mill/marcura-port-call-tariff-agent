# ─────────────────────────────────────────────────────────────────────────────
# Marcura Port Call Tariff Agent — Dockerfile
# Multi-stage build: builder → runtime
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage A: builder ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage B: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="Marcura Port Call Tariff Agent"
LABEL org.opencontainers.image.description="Agentic-AI Automated Vessel Tariff Calculation System"
LABEL org.opencontainers.image.source="https://github.com/karthik-code-mill/marcura-port-call-tariff-agent"

# Non-root user for security
RUN useradd --create-home --shell /bin/bash tariff
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src/        ./src/
COPY guardrails/ ./guardrails/
COPY prompts/    ./prompts/
COPY config/     ./config/

# Runtime data directories (mounted as volumes in production)
RUN mkdir -p context-layer/rag/raw \
             context-layer/rag/out \
             context-layer/rag/db \
             logs \
 && chown -R tariff:tariff /app

USER tariff

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src:/app

EXPOSE 8000

# Default: start the FastAPI server
# Override CMD to run Stage 1 pipeline instead:
#   docker run ... python -m orchestrator.document_prep_pipeline --country "South Africa" ...
CMD ["uvicorn", "api.api:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
