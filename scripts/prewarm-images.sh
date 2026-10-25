#!/usr/bin/env bash
# Pull the suite's external base images before the suite runs (AUD-747).
#
# Usage: scripts/prewarm-images.sh [--backend] [--frontend] [--list]
#
# WHY THIS EXISTS. Docker Hub's anonymous pull allowance is per **source IP**
# over a rolling window — not per repository, per user or per agent. Several
# agents build on this host continuously and the Gitea self-hosted runner shares
# the same egress IP, so a pull that has to go out to registry-1.docker.io can
# come back `429 Too Many Requests` for reasons that have nothing to do with the
# tree under test.
#
# That alone would be a flake we live with. What made it worth a script is where
# the 429 lands. The suite pulls lazily, as a side effect of whichever phase
# first needs an image: `compose up db-test` pulls postgres, `compose build`
# pulls the Dockerfile bases, and `npm ci` inside frontend-tests pulls
# node:22-alpine. So a rate-limited pull is reported as *that* phase failing.
# The first time it happened the ledger (AUD-715) said
#
#   phase: frontend dependency install (npm ci)   failure
#
# and the honest reading of that — the only reading available to an agent, since
# no one here can open a job log or an artifact (AUD-714) — is "something is
# wrong with package-lock.json". It cost a wrong diagnosis before the real
# message was found in the local log.
#
# So this script moves every outbound pull into one phase that is about pulling
# and nothing else. `image_pull` going red means the registry said no; it can
# mean nothing else. The suite phases that follow then fail only for their own
# reasons.
#
# WHAT IT DOES NOT DO. It does not pull more often than the suite already did.
# An image already in the local store is skipped — no HEAD, no manifest request,
# no allowance spent. That is deliberately the same policy Compose and BuildKit
# already apply (neither compose.test.yaml nor the Dockerfiles set
# `pull_policy: always`), so a floating tag such as `postgres:16-alpine` keeps
# resolving to the copy already on the box. Refreshing those would *increase*
# the pull traffic this script exists to make survivable; freshness of the test
# bases is the job of the digest pins and their bumps, not of the test runner.
#
# Nor does it cover the `ruff` phase's own image. That one is
# `ghcr.io/astral-sh/ruff:<tag>` — a different registry with a far more generous
# anonymous allowance, and a floating tag that is already in the local store, so
# it is neither the failure mode nor at risk of becoming it. Its pin lives in
# test.sh's ruff phase; copying it here would create a second place to bump.
#
# This is the cheap half of AUD-747. The structural fix is a pull-through mirror
# (or an authenticated daemon) so the first pull of a new digest is local too;
# both need host configuration or a credential, and neither is in this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_TEST="${ROOT}/compose.test.yaml"

# Compose names the images it builds itself `<project>-<service>`, and those are
# the ones that must NOT be pulled — they do not exist in any registry. Passing
# our own `-p` is what makes that prefix known rather than guessed.
PREWARM_PROJECT="audr-prewarm"

# Retries exist for the rate limit specifically: the window is rolling, so the
# allowance that refused us is partly restored a few seconds later. Backoff
# doubles — 20s then 40s by default, under a minute added to a run that was
# going to fail outright. Set AUDR_PULL_BACKOFF=0 to disable the sleeping
# (the tests do).
ATTEMPTS="${AUDR_PULL_ATTEMPTS:-3}"
BACKOFF="${AUDR_PULL_BACKOFF:-20}"

WANT_BACKEND=0
WANT_FRONTEND=0
LIST_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --backend)  WANT_BACKEND=1 ;;
    --frontend) WANT_FRONTEND=1 ;;
    --list)     LIST_ONLY=1 ;;
    *)
      echo "Unknown option: ${arg}" >&2
      echo "Usage: $0 [--backend] [--frontend] [--list]" >&2
      exit 2
      ;;
  esac
done

# Neither leg named means both, matching test.sh's own default.
if [[ "${WANT_BACKEND}" -eq 0 && "${WANT_FRONTEND}" -eq 0 ]]; then
  WANT_BACKEND=1
  WANT_FRONTEND=1
fi

# The `image:` refs Compose resolves for the profiles this run will use, minus
# the ones it builds locally. Reading them out of Compose rather than listing
# them here is the point: a bumped digest or a new service is picked up without
# anybody remembering to edit this file.
compose_images() {
  local profiles=()
  [[ "${WANT_BACKEND}" -eq 1 ]] && profiles+=(--profile backend)
  [[ "${WANT_FRONTEND}" -eq 1 ]] && profiles+=(--profile frontend)
  docker compose -p "${PREWARM_PROJECT}" -f "${COMPOSE_TEST}" "${profiles[@]}" \
    config --images 2>/dev/null | grep -v "^${PREWARM_PROJECT}-" || true
}

# The `FROM` bases of the Dockerfiles Compose builds. `config --images` does not
# report these — they are inside the build, which is why a rate-limited base
# used to surface as `backend_build` failing.
#
# A `FROM` naming an earlier stage is not an image and must not be pulled, so
# stage aliases seen so far are excluded; so is any ref still carrying a `$`,
# which is a build arg this script cannot resolve and must leave to BuildKit.
dockerfile_bases() {
  local files=()
  [[ "${WANT_BACKEND}" -eq 1 ]] && files+=("${ROOT}/Dockerfile" "${ROOT}/Dockerfile.test")
  [[ "${#files[@]}" -eq 0 ]] && return 0
  local existing=()
  local f
  for f in "${files[@]}"; do
    [[ -f "${f}" ]] && existing+=("${f}")
  done
  [[ "${#existing[@]}" -eq 0 ]] && return 0
  awk '
    toupper($1) == "FROM" {
      ref = $2
      if (!(ref in stage) && ref !~ /\$/) print ref
      if (toupper($3) == "AS") stage[$4] = 1
    }
  ' "${existing[@]}"
}

if [[ -n "${AUDR_SKIP_PREWARM:-}" ]]; then
  echo "image pre-warm skipped (AUDR_SKIP_PREWARM set); images will be pulled lazily by the suite"
  exit 0
fi

mapfile -t REFS < <({ compose_images; dockerfile_bases; } | sed '/^$/d' | sort -u)

if [[ "${#REFS[@]}" -eq 0 ]]; then
  # Not an error on its own, but it means the resolution above found nothing —
  # which would silently restore the lazy-pull behaviour this script replaces.
  echo "WARNING: no external base images resolved from ${COMPOSE_TEST} or the Dockerfiles;" \
    "pulls will happen lazily inside the suite phases again" >&2
  exit 0
fi

if [[ "${LIST_ONLY}" -eq 1 ]]; then
  printf '%s\n' "${REFS[@]}"
  exit 0
fi

FAILED=()
for ref in "${REFS[@]}"; do
  if docker image inspect "${ref}" >/dev/null 2>&1; then
    echo "already local: ${ref}"
    continue
  fi

  delay="${BACKOFF}"
  pulled=0
  for (( attempt = 1; attempt <= ATTEMPTS; attempt++ )); do
    echo "pulling ${ref} (attempt ${attempt}/${ATTEMPTS})"
    if docker pull "${ref}"; then
      pulled=1
      break
    fi
    if (( attempt < ATTEMPTS )); then
      echo "pull failed; retrying in ${delay}s (a 429 here is the shared-IP allowance, not this tree)"
      [[ "${delay}" != "0" ]] && sleep "${delay}"
      delay=$(( delay * 2 ))
    fi
  done
  [[ "${pulled}" -eq 1 ]] || FAILED+=("${ref}")
done

if [[ "${#FAILED[@]}" -gt 0 ]]; then
  # Say the diagnosis here, in the phase that owns it. This text is the whole
  # reason the script exists: it is what a reader sees instead of a misleading
  # "frontend dependency install failed".
  echo
  echo "==> IMAGE PRE-WARM FAILED after ${ATTEMPTS} attempts each: ${FAILED[*]}"
  echo "    The registry refused the pull. On this host that is almost always Docker Hub's"
  echo "    anonymous per-IP rate limit (HTTP 429), shared with every other agent and with"
  echo "    the Gitea runner — a digest bump is the usual trigger, because a new digest"
  echo "    cannot be served from the local store. The tree under test is not implicated."
  echo "    See AUD-747 for the structural fix (pull-through mirror / authenticated daemon)."
  exit 1
fi

echo "==> All base images present locally"
