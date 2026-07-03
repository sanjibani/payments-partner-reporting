# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# matplotlib needs a minimal system libs setup for headless rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 libpng16-16 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching).
COPY pyproject.toml ./
COPY src ./src
COPY api ./api
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install .

# Drop privileges for runtime.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8080

# Container Apps invokes `python -m api.main` (or uvicorn) on port 8080.
# We bind to 0.0.0.0:8080 so the ACA ingress can route to it.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]