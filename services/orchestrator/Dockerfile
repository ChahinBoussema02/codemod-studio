# syntax=docker/dockerfile:1

# ---- base -----------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---- dependencies ---------------------------------------------------------
# Resolved from the project metadata alone, so this layer is only rebuilt
# when the dependency set changes -- not on every source edit.
FROM base AS deps

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "httpx>=0.27.0" \
        "langgraph>=0.2.50" \
        "langgraph-checkpoint>=2.0.0"

# ---- runtime --------------------------------------------------------------
FROM deps AS runtime

# Non-root: the service only ever reads code sent over HTTP, never the disk.
RUN groupadd --system app && useradd --system --gid app --home /app app

COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app pyproject.toml ./

USER app

ENV CMS_HOST=0.0.0.0 \
    CMS_PORT=8000 \
    CMS_ENVIRONMENT=production \
    CMS_JSON_LOGS=true \
    # Reach the host's Ollama from inside the container network.
    CMS_OLLAMA_BASE_URL=http://host.docker.internal:11434

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)"

# One worker: the bottleneck is Ollama, which serialises inference anyway.
# Scale by running more containers, not more workers per container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
