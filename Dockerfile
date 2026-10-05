# Stage 1: build the frontend
# Base images are referenced by tag, not by digest, so a reader can see at a
# glance which Node and Python this builds on. The digests the tags resolved to
# at each release are recorded in docs/third-party.md section 1.1 — look there
# when a build has to be reproduced exactly.
FROM node:22-alpine AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The SPA bakes its version in at build time (frontend/build-meta.ts).
# AUDR_VERSION is optional — without it the bundle falls back to package.json,
# which scripts/release.sh keeps on the same number. Declared here, after
# `npm ci`, so changing the version does not re-resolve node_modules.
ARG AUDR_VERSION=""
ARG AUDR_GIT_SHA=""
ENV AUDR_VERSION=${AUDR_VERSION} AUDR_GIT_SHA=${AUDR_GIT_SHA}
RUN npm run build

# Stage 2: install Python dependencies with uv
FROM python:3.14-slim AS backend-builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 3: final runtime image
FROM python:3.14-slim AS runtime
WORKDIR /app

# No apt layer here on purpose: an apt install resolves to whatever the Debian
# archive serves at build time, which is an unpinned dependency in the runtime
# image. The compose health check is `python -m audr.operations.healthcheck`,
# which uses the interpreter already present rather than `curl`, for exactly
# this reason — keep health checks stdlib-only. See docs/third-party.md 1.3.

# Non-root user for runtime security
RUN useradd -m -u 1000 -s /bin/bash audr

# Copy virtual environment from builder
COPY --from=backend-builder /app/.venv /app/.venv

# Copy backend source and migration files
COPY backend/src/ /app/src/
COPY backend/alembic.ini /app/
COPY backend/migrations/ /app/migrations/

# Copy built frontend assets. The API serves the SPA from here
# (audr.api.spa) — there is no separate web container — so this is the only
# copy of the bundle and `frontend-builder` above is load-bearing.
COPY --from=frontend-builder /app/dist/ /app/static/

RUN chown -R audr:audr /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

# Build provenance for GET /api/v1/version. Last in the file on purpose —
# these change on every build, so anything above them stays cached.
# The version itself is NOT taken from a build arg: it lives in
# backend/src/audr/version.py, which is already in the image, so an image can
# never report a version its code does not carry.
ARG AUDR_GIT_SHA=""
ARG AUDR_BUILT_AT=""
ARG AUDR_VERSION=""
ENV AUDR_GIT_SHA=${AUDR_GIT_SHA} AUDR_BUILT_AT=${AUDR_BUILT_AT}
LABEL org.opencontainers.image.version=${AUDR_VERSION} \
      org.opencontainers.image.revision=${AUDR_GIT_SHA} \
      org.opencontainers.image.created=${AUDR_BUILT_AT} \
      org.opencontainers.image.source="https://github.com/br3d/audr"

USER audr

EXPOSE 8000
CMD ["uvicorn", "audr.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
