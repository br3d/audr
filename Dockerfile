# Stage 1: build the frontend
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# AUD-407: the SPA bakes its version in at build time (frontend/build-meta.ts).
# AUDR_VERSION is optional — without it the bundle falls back to package.json,
# which scripts/release.sh keeps on the same number. Declared here, after
# `npm ci`, so changing the version does not re-resolve node_modules.
ARG AUDR_VERSION=""
ARG AUDR_GIT_SHA=""
ENV AUDR_VERSION=${AUDR_VERSION} AUDR_GIT_SHA=${AUDR_GIT_SHA}
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

# No apt layer here on purpose. This stage used to install `curl` for the
# container health check, which was the one element of the release image that
# was not reproducible: every `FROM` above is digest-pinned, but apt resolves
# to whatever the Debian archive serves at build time. The health check in
# compose.yaml now uses the interpreter that is already in the image
# (`python -c` + stdlib urllib), so nothing in the runtime needs apt. See
# docs/third-party.md section 1.3 and AUD-379. Keep health checks stdlib-only;
# reintroducing apt here reintroduces the unpinned dependency.

# Non-root user for runtime security
RUN useradd -m -u 1000 -s /bin/bash audr

# Copy virtual environment from builder
COPY --from=backend-builder /app/.venv /app/.venv

# Copy backend source and migration files
COPY backend/src/ /app/src/
COPY backend/alembic.ini /app/
COPY backend/migrations/ /app/migrations/

# Copy built frontend assets. Until AUD-388 this was dead weight — the SPA was
# served by a separate nginx image and nothing ever read /app/static. The API
# now serves it from here (audr.api.spa), so this is the only copy of the bundle
# and `frontend-builder` above is load-bearing for the runtime image.
COPY --from=frontend-builder /app/dist/ /app/static/

RUN chown -R audr:audr /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src

# AUD-407: build provenance for GET /api/v1/version. Last in the file on
# purpose — these change on every build, so anything above them stays cached.
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
