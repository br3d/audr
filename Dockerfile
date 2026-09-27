# ── Stage 1: build the React frontend ──────────────────────────────────────
# Pin digest for linux/amd64. Update via:
#   docker manifest inspect node:22-alpine \
#     | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'
FROM node:22-alpine@sha256:2c752226d477b4a886378baa95b9af252be59301b725fdb0b7e15208131505a8 AS frontend-builder

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline

COPY frontend/ ./
RUN npm run build

# ── Stage 2: production runtime ─────────────────────────────────────────────
# Pin digest for linux/amd64. Update via:
#   docker manifest inspect python:3.14-slim \
#     | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'
FROM python:3.14-slim@sha256:7bf6c3111fe094f8ee1a1cbcdc63c4cfb345b0e3df42d5aa9a90b3b4b022ab6d AS runtime

# Install uv for reproducible dependency installation from the lockfile
RUN pip install --no-cache-dir "uv==0.7.19"

# Create non-root user
RUN groupadd --gid 1001 audr && \
    useradd --uid 1001 --gid audr --no-create-home --shell /bin/false audr

WORKDIR /app

# Layer cache: install deps before copying source so they rebuild only when
# pyproject.toml/uv.lock changes, not on every source edit.
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src/ ./src/
RUN uv pip install --system --no-dev --no-cache .

# Copy built frontend assets served as static files by the backend
COPY --from=frontend-builder /build/dist /app/frontend/dist

# Alembic migration files are needed at runtime for the init container
COPY backend/migrations/ ./migrations/
COPY backend/alembic.ini ./

ENV PYTHONPATH=/app/src

USER audr

EXPOSE 8000

CMD ["uvicorn", "audr.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
