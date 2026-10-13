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
# So each run cleans up after the previous casualties. Two guards keep this from
# ever touching a live stack:
#   * only auto-generated `audr-test-<pid>` names, never a pinned
#     AUDR_TEST_PROJECT — that opts into a long-lived stack somebody owns;
#   * only projects whose newest container is older than AUDR_TEST_REAP_AGE_H
#     (default 6h, an order of magnitude past the suite's runtime) and which
#     have no running `*-tests-*` container, i.e. no suite currently executing.
REAP_AGE_H="${AUDR_TEST_REAP_AGE_H:-6}"

reap_stale_projects() {
  local cutoff now p names n created newest state running left
  now="$(date +%s)"
  cutoff=$(( now - REAP_AGE_H * 3600 ))

  for p in $(docker ps -a --filter 'label=com.docker.compose.project' \
      --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
      | grep -E '^audr-test-[0-9]+$' | grep -vFx "${PROJECT}" | sort -u); do
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
  # above never sees it) but the tags it built survive.
  for n in $(docker images --format '{{.Repository}}' 2>/dev/null \
      | grep -E '^audr-test-[0-9]+-(backend-tests|migrate-test|benchmark)$' \
      | grep -vE "^${PROJECT}-" | sort -u); do
    created="$(docker inspect -f '{{.Created}}' "${n}:latest" 2>/dev/null)" || continue
    created="$(date -d "${created}" +%s 2>/dev/null)" || continue
    (( created > cutoff )) && continue
    [[ -n "$(docker ps -aq --filter "ancestor=${n}:latest" 2>/dev/null)" ]] && continue
    echo "==> Reaping orphan test image ${n}:latest"
    docker rmi -f "${n}:latest" >/dev/null 2>&1 || true
  done

  # Named frontend volumes outlive `down` when the run was killed before it.
  for n in $(docker volume ls -f dangling=true --format '{{.Name}}' 2>/dev/null \
      | grep -E '^audr-test-[0-9]+_frontend_node_modules$' \
      | grep -vE "^${PROJECT}_"); do
    echo "==> Reaping orphan test volume ${n}"
    docker volume rm "${n}" >/dev/null 2>&1 || true
  done
}

if [[ -z "${AUDR_TEST_PROJECT:-}" ]]; then
  reap_stale_projects || true
fi

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
  fi
fi

if [[ "${EXIT}" -ne 0 ]]; then
  echo "==> TESTS FAILED (exit ${EXIT})"
else
  echo "==> All tests passed"
fi

exit "${EXIT}"
