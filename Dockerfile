# Stage 1: build the frontend
# Base images are pinned by digest, with the tag kept in front of the `@` so a
# reader still sees at a glance which Node and Python this builds on. The tag is
# decoration once a digest is present — docker resolves the digest — so these
# two builds of one commit can never land on different base revisions (AUD-697).
#
# Bumping a pin: Dependabot's `docker` ecosystem (.github/dependabot.yml) opens
# a weekly pull request when a pinned tag moves. (That entry did not exist until
# AUD-697 added it with these pins — a digest pin with no bump mechanism freezes
# a base image together with its unfixed CVEs.) It reads these `FROM` lines
# only: compose.yaml's `db` and compose.test.yaml's `db-test`/`frontend-tests`
# are invisible to it, and each of those is pinned to the SAME digest as a line
# below on purpose — see docs/third-party.md 1.1 for which pairs and why — so a
# bump here is not finished until its partner moves too.
#
# To do it by hand, resolve the tag and paste the result, then refresh the table
# in docs/third-party.md 1.1:
#
#   docker buildx imagetools inspect node:22-alpine --format '{{.Manifest.Digest}}'
#
# Pin the multi-arch manifest-list digest that command returns, not a
# per-architecture one, or the build stops working on one of the two arches.
#
# Why this one digest is NOT "what the tag means today", unlike the python and
# postgres pins below and in compose.yaml (AUD-697). Node is a *build-stage*
# image: it runs `npm run build` here and Vitest in compose.test.yaml, and it is
# never shipped — stage 3 copies out `/app/dist` and nothing else. So a fresher
# node digest buys no runtime security, which is the thing that justified
# resolving the other two live. What it does cost is a mandatory fresh pull:
# a bumped digest by construction names an image no local store has yet, and
# Docker Hub rate-limits unauthenticated pulls per source IP. This host and the
# Gitea runner share that IP, so the bump turned the frontend leg of the suite
# red with `429 Too Many Requests` on a tree whose code was green — see the
# follow-up issue filed from AUD-697 about the missing pull-through mirror.
#
# So this stays on the digest the suite has been green against for weeks, and
# the images that actually ship or run in production (python here, postgres in
# compose.yaml) carry today's. Dependabot will propose node's bump on its weekly
# run like any other; take it once there is a mirror, and move
# compose.test.yaml's `frontend-tests` in the same commit.
FROM node:26-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
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
FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS backend-builder
# Pinned for the same reason the bases above are (AUD-697): `uv sync --frozen`
# is deterministic given uv.lock, but which *uv* reads that lock was not, so the
# resolver version floated with the build date. Keep in step with Dockerfile.test.
RUN pip install --no-cache-dir uv==0.13.5
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 3: final runtime image
# Same digest as `backend-builder` on purpose: the venv copied out of that stage
# is built against its interpreter, so the two must not drift apart.
FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS runtime
WORKDIR /app

# No apt layer here on purpose: an apt install resolves to whatever the Debian
# archive serves at build time, which is an unpinned dependency in the runtime
# image. The compose health check is `python -m audr.operations.healthcheck`,
# which uses the interpreter already present rather than `curl`, for exactly
# this reason — keep health checks stdlib-only. See docs/third-party.md 1.3.

# Non-root user for runtime security.
#
# `install -d` re-owns the WORKDIR itself, which `WORKDIR` above created as
# root. Do not drop it: the worker's liveness beacon is a file the `audr` user
# creates *inside* /app at runtime (`/app/worker-heartbeat`, written by
# audr.jobs.__main__ and read by audr.operations.healthcheck), so a root-owned
# /app makes every worker beat fail with EACCES and the container never reports
# healthy. Ownership of the directory is what that needs — not ownership of its
# contents, which the COPYs below set as they land.
RUN useradd -m -u 1000 -s /bin/bash audr \
 && install -d -o audr -g audr /app

# Everything below lands owned by `audr` via `--chown`, rather than being
# re-owned afterwards. A trailing `RUN chown -R audr:audr /app` used to do this
# in one line, but chown rewrites the inode of every file it touches, and a
# layer stores whole files rather than diffs — so that one line copied the venv
# and the Vite bundle into the image a second time (AUD-697). `--chown` is free:
# the COPY was already writing those files.
#
# Measured, because "a layer stores whole files" is the kind of claim that
# deserves a number. Two builds of this same tree, same pinned base digests and
# the same `--provenance=false --sbom=false`, differing only in this one choice:
#
#   COPY --chown (this file)        408,366,992 B
#   trailing RUN chown -R           638,399,647 B
#   -------------------------------------------------
#   difference                     -230,032,655 B  (-219.4 MiB, -36.0%)
#
# Every layer before the last is identical and cached, so the delta is this
# change and nothing else. Re-measure it the same way if you touch the COPYs —
# comparing against a build on a different base, or with buildx attestations
# switched on, measures the base and the attestations instead.

# Copy virtual environment from builder
COPY --from=backend-builder --chown=audr:audr /app/.venv /app/.venv

# Copy backend source and migration files
COPY --chown=audr:audr backend/src/ /app/src/
COPY --chown=audr:audr backend/alembic.ini /app/
COPY --chown=audr:audr backend/migrations/ /app/migrations/

# Copy built frontend assets. The API serves the SPA from here
# (audr.api.spa) — there is no separate web container — so this is the only
# copy of the bundle and `frontend-builder` above is load-bearing.
COPY --from=frontend-builder --chown=audr:audr /app/dist/ /app/static/

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
