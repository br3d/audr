#!/usr/bin/env bash
# Run the full test suite: backend (pytest) and frontend (Vitest) in Docker Compose.
# Usage: ./scripts/test.sh [--backend-only|--frontend-only]
# Exits non-zero if any suite fails.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_TEST="${ROOT}/compose.test.yaml"
# Unique compose project per invocation. compose.test.yaml declares the fixed
# project name `audr-test`, so two concurrent runs on the same host — a CI job
# and a local run, say — would share containers, volumes and the test network,
# and either side's teardown `down -v` would destroy the other's database
# mid-run. The resulting red suites look like app regressions. Override with
# AUDR_TEST_PROJECT to deliberately reuse a stack.
PROJECT="${AUDR_TEST_PROJECT:-audr-test-$$}"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_TEST}")
EXIT=0

RUN_BACKEND=1
RUN_FRONTEND=1
for arg in "$@"; do
  case "${arg}" in
    --backend-only)  RUN_FRONTEND=0 ;;
    --frontend-only) RUN_BACKEND=0  ;;
    *)
      echo "Unknown option: ${arg}"
      echo "Usage: $0 [--backend-only|--frontend-only]"
      exit 1
      ;;
  esac
done

teardown() {
  echo "==> Tearing down test environment"
  # `--rmi local` also removes the images Compose built for this project
  # (`<project>-migrate-test`, `<project>-backend-tests`). Without it every run
  # leaked two ~560MB images, because PROJECT is unique per invocation and so
  # nothing ever reclaimed the previous run's tags: 238 orphans had accumulated
  # on the agent host and took it to 97% disk. Rebuild cost is unaffected — a
  # per-run project name means the image was never reused anyway, and the shared
  # BuildKit cache still serves the layers. Skipped when the caller pinned
  # AUDR_TEST_PROJECT, since that opts into reusing a long-lived stack.
  local rmi=(--rmi local)
  [[ -n "${AUDR_TEST_PROJECT:-}" ]] && rmi=()
  "${COMPOSE[@]}" --profile backend --profile frontend down -v --remove-orphans "${rmi[@]}" || true
}
trap teardown EXIT

# Reap stale per-run stacks left behind by hard-killed runs (AUD-529).
#
# `trap teardown EXIT` covers a normal exit, Ctrl-C and a failing suite, but it
# does NOT fire when the process is SIGKILLed — which is exactly what happens
# when an agent run is terminated mid-suite. Those runs leak a whole compose
# project: a running postgres and wiremock, two ~550MB images and a
# node_modules volume, none of which anything ever reclaims. Six such orphans
# (up to six days old) had taken the shared agent host to 89% disk.
#
# So each run cleans up after the previous casualties.
#
# A stack is ours to reap when its containers carry a `com.docker.compose.service`
# label from compose.test.yaml's service set — NOT when its project name matches a
# shape (AUD-548). The original name gate (`^audr-test-<pid>$`, and the reaper only
# running at all when AUDR_TEST_PROJECT was unset) rested on "a pinned name opts
# into a long-lived stack somebody owns". In practice a pinned name is an ad-hoc
# per-issue label — `audr-aud522-156759`, `audr-ruff-$$` — whose owner finished
# days ago, and nothing reclaimed it: not its own run (`teardown` drops
# `--rmi local` when pinned, so even a *clean* exit leaks two ~550MB tags), and not
# any later run. Fifteen such images — ~7GB of tags across nine dead projects —
# plus a 35h-idle postgres+wiremock pair were found on the shared host at 86% disk.
#
# Matching on the service label is also strictly safer than the prefix it replaces:
# the dev stack's services (`web`, `init`, `api`, `worker`, `db`, `migrate`) and the
# deploy host's do not intersect this set, so no project name can be reaped by
# accident, whatever it is called.
#
# Three guards keep this from ever touching a live stack, all unchanged from
# AUD-529:
#   * never the current ${PROJECT} — a pinned stack is exempt *while in use*;
#   * only projects whose newest container is older than AUDR_TEST_REAP_AGE_H
#     (default 6h, an order of magnitude past the suite's runtime) and which
#     have no running `*-tests-*` container, i.e. no suite currently executing;
#   * never a stack whose age cannot be established (see `newest == 0` below).
#
# What this does cost: a pinned stack reused on a cycle longer than the age gate is
# now torn down between uses, so the next run rebuilds it. Nothing is lost — the
# suite brings its own stack up (`up -d --wait db-test`) and migrate-test re-runs
# from scratch, so the price is build time, paid from the BuildKit cache. Raise
# AUDR_TEST_REAP_AGE_H past the reuse interval if that trade is wrong for a stack.
REAP_AGE_H="${AUDR_TEST_REAP_AGE_H:-6}"

# compose.test.yaml's service names, as a grep alternation. Keep in step with that
# file; a service missing here is a service whose leftovers nothing reclaims.
TEST_SERVICES='db-test|provider-mock|migrate-test|backend-tests|benchmark|frontend-tests'
# The images and volumes Compose builds carry the same two labels its containers
# do, so they are matched the same way — a project-name prefix would not do here
# either. The leaks found on the shared host at 86% disk were
# `aud388v-backend-tests`, `aud446n-backend-tests`,
# `aud442-ut-3451816-migrate-test` and six more: real compose.test.yaml images
# whose project name does not begin with `audr` at all.

reap_stale_projects() {
  local cutoff now p names n created newest state running left meta iproj isvc vproj
  now="$(date +%s)"
  cutoff=$(( now - REAP_AGE_H * 3600 ))

  for p in $(docker ps -a --filter 'label=com.docker.compose.project' \
      --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}' \
      2>/dev/null \
      | grep -E "\|(${TEST_SERVICES})\$" | cut -d'|' -f1 \
      | grep -vFx "${PROJECT}" | sort -u); do
    names="$(docker ps -a --filter "label=com.docker.compose.project=${p}" \
      --format '{{.Names}}' 2>/dev/null)"
    [[ -z "${names}" ]] && continue

    newest=0
    running=0
    for n in ${names}; do
      created="$(docker inspect -f '{{.Created}}' "${n}" 2>/dev/null)" || continue
      created="$(date -d "${created}" +%s 2>/dev/null)" || continue
      (( created > newest )) && newest="${created}"
      state="$(docker inspect -f '{{.State.Running}}' "${n}" 2>/dev/null)" || continue
      [[ "${state}" == "true" && "${n}" == *-tests-* ]] && running=1
    done

    # `newest == 0` means every inspect failed — the containers went away under
    # us, or the daemon is unhappy. Either way we cannot prove the stack is
    # idle, so leave it alone rather than tear down on an unknown age.
    if (( running == 1 )) || (( newest == 0 )) || (( newest > cutoff )); then continue; fi

    echo "==> Reaping stale test stack ${p} (idle >${REAP_AGE_H}h)"
    docker compose -p "${p}" -f "${COMPOSE_TEST}" \
      --profile backend --profile frontend down -v --remove-orphans --rmi local \
      >/dev/null 2>&1 || true

    # `compose down` only removes containers it still recognises as its own:
    # a stack whose compose metadata the current Compose no longer understands
    # survives the call entirely, silently. Those are exactly the oldest leaks,
    # so force-remove whatever still carries the project label — the stack is
    # already proven idle by the guards above.
    left="$(docker ps -aq --filter "label=com.docker.compose.project=${p}" 2>/dev/null)" || left=""
    if [[ -n "${left}" ]]; then
      # shellcheck disable=SC2086  # intentional word splitting: one id per arg
      docker rm -f ${left} >/dev/null 2>&1 || true
      docker network rm "${p}_test-net" >/dev/null 2>&1 || true
    fi
  done

  # Image-only leftovers: the project's containers are long gone (so the loop
  # above never sees it) but the tags it built survive. This is also the only
  # thing that ever reclaims a *pinned* project's images, since `teardown` skips
  # `--rmi local` for those by design (AUD-548).
  for n in $(docker images --filter 'label=com.docker.compose.service' \
      --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -u); do
    meta="$(docker inspect -f \
      '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' \
      "${n}" 2>/dev/null)" || continue
    iproj="${meta%%|*}"
    isvc="${meta##*|}"
    [[ "${isvc}" =~ ^(${TEST_SERVICES})$ ]] || continue
    [[ "${iproj}" == "${PROJECT}" ]] && continue
    # Only the tag Compose itself built for that project. A *pulled* image keeps
    # the labels of whatever Compose service it was last used by — the dev
    # stack's nginx is on this host as `192.168.1.90:8085/audr-frontend:latest`
    # labelled `service=web` — so insist the repository is exactly
    # `<project>-<service>` before removing anything. That also excludes every
    # registry-namespaced tag, ours and the other tenant's alike.
    [[ "${n}" == "${iproj}-${isvc}:latest" ]] || continue
    created="$(docker inspect -f '{{.Created}}' "${n}" 2>/dev/null)" || continue
    created="$(date -d "${created}" +%s 2>/dev/null)" || continue
    (( created > cutoff )) && continue
    [[ -n "$(docker ps -aq --filter "ancestor=${n}" 2>/dev/null)" ]] && continue
    echo "==> Reaping orphan test image ${n}"
    docker rmi -f "${n}" >/dev/null 2>&1 || true
  done

  # Named frontend volumes outlive `down` when the run was killed before it.
  # `dangling=true` already proves no container holds it, and the age gate keeps
  # a pinned stack's cache alive between two runs inside the window.
  for n in $(docker volume ls -f dangling=true \
      --filter 'label=com.docker.compose.volume=frontend_node_modules' \
      --format '{{.Name}}' 2>/dev/null); do
    vproj="$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' "${n}" 2>/dev/null)" || continue
    [[ "${vproj}" == "${PROJECT}" ]] && continue
    created="$(docker volume inspect -f '{{.CreatedAt}}' "${n}" 2>/dev/null)" || continue
    created="$(date -d "${created}" +%s 2>/dev/null)" || continue
    (( created > cutoff )) && continue
    echo "==> Reaping orphan test volume ${n}"
    docker volume rm "${n}" >/dev/null 2>&1 || true
  done
}

# Always, including when AUDR_TEST_PROJECT is pinned (AUD-548). Skipping the reaper
# for pinned runs was the reason a pinned stack was reaped by nobody: the only run
# that would have cleaned it up is the one that also opted out of cleaning. The
# current ${PROJECT} is exempt inside the function, which is the narrow exemption a
# pinned name actually needs.
reap_stale_projects || true

echo "==> Starting test environment (db-test, waiting for healthcheck)"
"${COMPOSE[@]}" up -d --wait db-test

if [[ "${RUN_BACKEND}" -eq 1 ]]; then
  # Rebuild both images: a stale migrate-test leaves the DB at an old migration
  # and backend tests then fail with "column does not exist".
  echo "==> Building backend test images (migrate-test, backend-tests)"
  "${COMPOSE[@]}" build migrate-test backend-tests

  echo "==> Running backend tests (pytest)"
  set +e
  "${COMPOSE[@]}" --profile backend run --rm --no-TTY backend-tests \
    2>&1 | tee "${ROOT}/.test-backend.log"
  BACKEND_EXIT="${PIPESTATUS[0]}"
  set -e
  echo "==> Backend suite exit=${BACKEND_EXIT}"
  if [[ "${BACKEND_EXIT}" -ne 0 ]]; then EXIT="${BACKEND_EXIT}"; fi
fi

if [[ "${RUN_FRONTEND}" -eq 1 ]]; then
  echo "==> Installing frontend dependencies"
  set +e
  "${COMPOSE[@]}" --profile frontend run --rm --no-TTY --entrypoint sh frontend-tests \
    -c 'npm ci --no-audit --no-fund' 2>&1 | tail -20
  FRONTEND_INSTALL_EXIT="${PIPESTATUS[0]}"
  set -e
  if [[ "${FRONTEND_INSTALL_EXIT}" -ne 0 ]]; then
    echo "==> Frontend dependency install FAILED (exit ${FRONTEND_INSTALL_EXIT})"
    EXIT="${FRONTEND_INSTALL_EXIT}"
  else
    echo "==> Running frontend tests (Vitest)"
    set +e
    "${COMPOSE[@]}" --profile frontend run --rm --no-TTY frontend-tests \
      2>&1 | tee "${ROOT}/.test-frontend.log"
    FRONTEND_EXIT="${PIPESTATUS[0]}"
    set -e
    echo "==> Frontend suite exit=${FRONTEND_EXIT}"
    if [[ "${FRONTEND_EXIT}" -ne 0 ]]; then EXIT="${FRONTEND_EXIT}"; fi

    # Generated API types vs. backend/openapi.json (AUD-694). Runs on the host,
    # not in the frontend-tests container: it only needs Node to diff against
    # the already-checked-out schema file, and does not touch Docker.
    echo "==> Checking generated API types against backend/openapi.json"
    set +e
    (cd "${ROOT}/frontend" && npm run check:api-types)
    API_TYPES_EXIT="$?"
    set -e
    echo "==> API types check exit=${API_TYPES_EXIT}"
    if [[ "${API_TYPES_EXIT}" -ne 0 ]]; then EXIT="${API_TYPES_EXIT}"; fi
  fi
fi

if [[ "${EXIT}" -ne 0 ]]; then
  echo "==> TESTS FAILED (exit ${EXIT})"
else
  echo "==> All tests passed"
fi

exit "${EXIT}"
