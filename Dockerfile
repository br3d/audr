# Stage 1: build the frontend
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: install Python dependencies with uv
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS backend-builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 3: final runtime image
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS runtime
WORKDIR /app

# Runtime system deps (curl needed by health check)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Non-root user for runtime security
RUN useradd -m -u 1000 -s /bin/bash audr

# Copy virtual environment from builder
COPY --from=backend-builder /app/.venv /app/.venv

# Copy backend source and migration files
COPY backend/src/ /app/src/
COPY backend/alembic.ini /app/
COPY backend/migrations/ /app/migrations/

# Copy built frontend assets
COPY --from=frontend-builder /app/dist/ /app/static/

RUN chown -R audr:audr /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

USER audr

EXPOSE 8000
CMD ["uvicorn", "audr.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# Stage 4: nginx image serving the frontend SPA
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10 AS frontend-server
COPY --from=frontend-builder /app/dist/ /usr/share/nginx/html/
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf
