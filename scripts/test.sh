#!/usr/bin/env bash
# Run the full merge gate: ruff, backend (pytest), mypy and frontend (Vitest),
# in Docker Compose.
# Usage: ./scripts/test.sh [--backend-only|--frontend-only]
# Env:   AUDR_SKIP_STATIC=1  skip the ruff and mypy phases (AUD-744); set by the
#                            GitHub `tests` job, which has dedicated jobs for both
#        AUDR_TEST_PROJECT   reuse a pinned compose project instead of a fresh one
#        AUDR_SKIP_PREWARM=1 skip the image_pull phase and let the suite pull
#                            lazily again (AUD-747)
# Exits non-zero if any suite or static gate fails.
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

# Transcript of everything that happens before (and around) the suites: the
# environment snapshot, the image pulls, the `--wait` healthcheck, the builds
# and the frontend dependency install. It exists because a run that dies in any
# of those phases used to produce no file at all, and CI's only other record of
# the phase was a job log nobody can read without repository admin rights
# (AUD-714). Written from the first line of the run so there is always at least
# one artifact to upload, however early the death.
ENV_LOG="${ROOT}/.test-env.log"
: >"${ENV_LOG}"

# Machine-readable ledger of which phase ran, how it ended and how long it took
# (AUD-715). One tab-separated row per phase, appended as each phase ends:
#
#   <slug> <rc> <start epoch> <end epoch> <human label>
#
# ENV_LOG above says the same thing in prose, but nothing that can read it is
# available to an agent inheriting a red CI run: the artifact download endpoint
# needs authentication and the job log needs repository admin (AUD-714). This
# file is the machine-readable half, and scripts/ci-phase-ledger.sh replays it
# into one *workflow step* per phase — step names and conclusions being the one
# channel a reader with no credentials can hear. See that script's header.
#
# Written here, next to ENV_LOG and before anything can fail, for the same
# reason: a run that dies early must still leave a ledger saying so.
PHASE_LEDGER="${ROOT}/.test-phases.tsv"
: >"${PHASE_LEDGER}"

# Keep the slugs passed here in step with ci-phase-ledger.sh's PHASES list — a
# slug only this file knows is a phase no CI reader can see. That script warns
# when the two drift.
record_phase() {
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >>"${PHASE_LEDGER}"
}

# Exit status of the last run_phase, for callers that need it after the `if !`
# that swallows `$?`.
PHASE_RC=0

# Run a pre-suite phase: announce it, stream its output to both the job log and
# ENV_LOG, record it in the ledger, and report its status without letting
# `set -e` kill the script first — the point is to fail *loudly*, not to stop
# failing.
run_phase() {
  local slug="$1" label="$2" start
  shift 2
  echo "==> ${label}"
  { echo; echo "### ${label}"; echo "### \$ $*"; } >>"${ENV_LOG}"
  start="$(date +%s)"
  set +e
  "$@" 2>&1 | tee -a "${ENV_LOG}"
  PHASE_RC="${PIPESTATUS[0]}"
  set -e
  record_phase "${slug}" "${PHASE_RC}" "${start}" "$(date +%s)" "${label}"
  return "${PHASE_RC}"
}

# What the suite was handed. Nearly every unreadable red so far has come down to
# a property of the runner rather than of the tree — two cores and a cold image
# cache on GitHub against a warm self-hosted box, or an anonymous Docker Hub
# pull allowance shared with every other job on that IP — so record those
# properties before anything can fail.
snapshot_env() {
  {
    echo "### environment snapshot"
    echo "date:      $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "host:      $(uname -srm)  nproc=$(nproc 2>/dev/null || echo '?')"
    echo "project:   ${PROJECT}"
    echo "ci:        ${CI:-unset}  runner=${RUNNER_NAME:-unset}  gitea=${GITEA_ACTIONS:-unset}"
    echo "commit:    $(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo '?')"
    echo "--- memory"
    free -m 2>/dev/null || true
    echo "--- disk (docker root)"
    df -h "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /)" 2>/dev/null || true
    echo "--- docker"
    docker version --format 'client={{.Client.Version}} server={{.Server.Version}}' 2>&1 || true
    docker compose version --short 2>&1 || true
    echo "--- docker login state (anonymous pulls are rate-limited per IP)"
    docker info -f '{{range .RegistryConfig.IndexConfigs}}{{.Name}} {{end}}' 2>/dev/null || true
    [[ -f "${HOME}/.docker/config.json" ]] && echo "docker config.json present" || echo "docker config.json absent (anonymous)"
  } >>"${ENV_LOG}" 2>&1
  echo "==> Environment snapshot written to .test-env.log"
}

# Everything we can cheaply say about why a pre-suite phase failed. `--wait`
# reports only that it timed out; the healthcheck's own complaint is in the
# container log, and whether the container exists at all is in `ps`.
dump_compose_state() {
  local why="$1"
  {
    echo
    echo "==> PRE-SUITE FAILURE: ${why}"
    echo "--- docker compose ps --all"
    "${COMPOSE[@]}" ps --all 2>&1 || true
    echo "--- docker compose logs db-test (tail 200)"
    "${COMPOSE[@]}" logs --no-color --tail 200 db-test 2>&1 || true
    echo "--- docker compose logs provider-mock (tail 50)"
    "${COMPOSE[@]}" logs --no-color --tail 50 provider-mock 2>&1 || true
    echo "--- disk after failure"
    df -h "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /)" 2>&1 || true
  } 2>&1 | tee -a "${ENV_LOG}"
}

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

# shellcheck disable=SC2329  # invoked indirectly by `trap teardown EXIT` below
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
TEST_SERVICES='db-test|provider-mock|migrate-test|migration-smoke|backend-tests|benchmark|frontend-tests'
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

snapshot_env

# Pull every external base image up front, as a phase of its own (AUD-747).
#
# The suite used to pull lazily, as a side effect of whichever phase first
# needed an image, so a `429 Too Many Requests` from Docker Hub's shared per-IP
# allowance was reported as *that* phase failing. It surfaced once as
# `frontend dependency install (npm ci)` and was read — reasonably, since the
# phase ledger is the only channel an agent here can read (AUD-714) — as a
# broken lockfile. Pulling first means a refused pull reds a phase that is about
# pulling and nothing else. Images already in the local store are skipped, so
# this adds no pull traffic and no time to the common case.
PREWARM_ARGS=()
[[ "${RUN_BACKEND}" -eq 1 ]] && PREWARM_ARGS+=(--backend)
[[ "${RUN_FRONTEND}" -eq 1 ]] && PREWARM_ARGS+=(--frontend)
if ! run_phase image_pull "Pre-warming external base images (docker pull)" \
    "${ROOT}/scripts/prewarm-images.sh" "${PREWARM_ARGS[@]}"; then
  # prewarm-images.sh prints the diagnosis itself and run_phase has already
  # streamed it to both sinks; `ps`/`logs` would say nothing about a registry
  # refusal. Fatal, unlike the static gates: every phase below needs the images.
  echo "==> PRE-SUITE FAILURE: base image pre-warm exited ${PHASE_RC}" | tee -a "${ENV_LOG}"
  exit "${PHASE_RC}"
fi

# ── Static gates: ruff and mypy ───────────────────────────────────────────────
#
# These live here because this script is what the team merges on. Agents
# self-merge on a green `./scripts/test.sh` (docs/engineering-workflow.md), but
# ruff and mypy were only ever enforced by the separate `lint` and
# `backend-mypy` GitHub jobs — which speak *after* the merge has landed. On
# 2026-10-25 that cost two red commits on `main`: AUD-658 added a `news_gate.py`
# error outside the AUD-690 baseline, `7d2b9f2` and `10ab22b` both merged
# mypy-red on a green suite, and `main` stayed red for ~50 minutes until AUD-743
# regenerated the baseline. A gate the implementer cannot run is not a gate.
#
# AUDR_SKIP_STATIC=1 opts out, and the GitHub `tests` job sets it: `lint` and
# `backend-mypy` already run there, in parallel, on their own runners. Repeating
# them inside `tests` would lengthen the critical path to re-prove what another
# job has already proven. The Gitea overlay never calls this script — it drives
# compose directly under a per-job COMPOSE_PROJECT — so none of this touches the
# concurrency fix in AUD-720.
#
# Both phases are non-fatal: they set EXIT like every other phase and let the
# run continue, so a ruff nit cannot hide a pytest failure behind it.
if [[ "${AUDR_SKIP_STATIC:-0}" != "1" ]]; then
  # Pinned to the same tag as the `lint` job and backend/pyproject.toml's dev
  # extra (AUD-392): an unpinned ruff makes the verdict depend on the day it
  # ran. The image is distroless and has no shell, so each check is its own
  # `docker run` rather than a loop inside one container.
  # Each check runs even when an earlier one fails, so one `ruff FAILED` names
  # every violation in the tree rather than only the first file. The quoting is
  # deliberate: `$1` must reach the inner shell unexpanded and arrive as "${ROOT}"
  # on its argv, so a path with a space in it stays one argument.
  # shellcheck disable=SC2016  # $1/$? are for the inner bash, not this one
  if ! run_phase ruff "Static lint (ruff check + format --check: backend/, scripts/)" \
      bash -c '
        set -o pipefail
        rc=0
        docker run --rm -v "$1:/w" -w /w/backend ghcr.io/astral-sh/ruff:0.16.10 check --no-cache . || rc=$?
        docker run --rm -v "$1:/w" -w /w/backend ghcr.io/astral-sh/ruff:0.16.10 format --check --no-cache . || rc=$?
        docker run --rm -v "$1:/w" -w /w ghcr.io/astral-sh/ruff:0.16.10 check --no-cache scripts/ || rc=$?
        docker run --rm -v "$1:/w" -w /w ghcr.io/astral-sh/ruff:0.16.10 format --check --no-cache scripts/ || rc=$?
        exit $rc
      ' _ "${ROOT}"; then
    echo "==> ruff FAILED (exit ${PHASE_RC}) — fix before merging" | tee -a "${ENV_LOG}"
    EXIT="${PHASE_RC}"
  fi
fi

# `--wait-timeout` matters as much as the dump below. A healthcheck that fails
# outright makes `--wait` return in seconds, but one that hangs in `starting`
# never returns at all — and an unbounded wait would burn the workflow's whole
# 45-minute job timeout before any artifact could be uploaded, defeating the
# logging this ticket exists to add on exactly the failure mode it was opened
# for. 180s is an order of magnitude past a healthy start (postgres goes healthy
# in ~5s here, and the healthcheck's own budget is 15 retries x 3s).
if ! run_phase db_up "Starting test environment (db-test, waiting for healthcheck)" \
    "${COMPOSE[@]}" up -d --wait --wait-timeout "${AUDR_TEST_WAIT_TIMEOUT:-180}" db-test; then
  dump_compose_state "\`up -d --wait db-test\` exited ${PHASE_RC} (healthcheck never went healthy — the pull is the image_pull phase's job now, unless AUDR_SKIP_PREWARM was set)"
  exit "${PHASE_RC}"
fi

if [[ "${RUN_BACKEND}" -eq 1 ]]; then
  # Rebuild both images: a stale migrate-test leaves the DB at an old migration
  # and backend tests then fail with "column does not exist".
  # migration-smoke shares Dockerfile's `runtime` target with migrate-test, so
  # it is a cache hit here and not a third build. Named explicitly all the same:
  # `compose run` would otherwise build it lazily inside the migration_smoke
  # phase, and a build failure would be reported as that phase failing — the
  # same misattribution AUD-747 fixed for the image pulls.
  if ! run_phase backend_build "Building backend test images (migrate-test, migration-smoke, backend-tests)" \
      "${COMPOSE[@]}" build migrate-test migration-smoke backend-tests; then
    # The builder's own output is the diagnosis here and run_phase has already
    # streamed it to both sinks; `ps`/`logs` would add nothing about a build.
    echo "==> PRE-SUITE FAILURE: \`build migrate-test migration-smoke backend-tests\` exited ${PHASE_RC}" \
      | tee -a "${ENV_LOG}"
    exit "${PHASE_RC}"
  fi

  # Migration reversibility, before the suite (AUD-698).
  #
  # `migration-smoke` runs upgrade -> downgrade -> upgrade over the newest
  # revisions and asserts a single head, through the same `python -m alembic`
  # entrypoint the deploy uses. It exists because the guarded deploy's rollback
  # is built on `alembic downgrade` succeeding and has nothing to try when it
  # does not: it reports 'downgrade failed' and leaves the stack on the new
  # build. See scripts/migration-smoke.sh for the full argument.
  #
  # First, because it is the cheapest phase that can condemn the commit and
  # because `backend-tests` reads this database as already-migrated — so the
  # smoke's postcondition (back at head, which it asserts) has to hold before
  # pytest starts rather than after.
  #
  # NOT fatal to the run, unlike the build above: a broken downgrade must not
  # hide a pytest failure behind it, and an implementer usually needs both
  # verdicts in one pass. It sets EXIT like every other suite phase, so the
  # merge gate is still red.
  if ! run_phase migration_smoke "Migration safety (single head, newest revisions reverse and re-apply)" \
      "${COMPOSE[@]}" --profile backend run --rm --no-TTY migration-smoke; then
    echo "==> MIGRATION SMOKE FAILED (exit ${PHASE_RC}) — the deploy could not roll this back" \
      | tee -a "${ENV_LOG}"
    EXIT="${PHASE_RC}"
  fi

  echo "==> Running backend tests (pytest)"
  BACKEND_START="$(date +%s)"
  set +e
  "${COMPOSE[@]}" --profile backend run --rm --no-TTY backend-tests \
    2>&1 | tee "${ROOT}/.test-backend.log"
  BACKEND_EXIT="${PIPESTATUS[0]}"
  set -e
  record_phase backend_suite "${BACKEND_EXIT}" "${BACKEND_START}" "$(date +%s)" \
    "Backend suite (pytest)"
  echo "==> Backend suite exit=${BACKEND_EXIT}"
  if [[ "${BACKEND_EXIT}" -ne 0 ]]; then EXIT="${BACKEND_EXIT}"; fi

  # mypy --strict against backend/mypy-baseline.txt — the same `backend-mypy`
  # service the dedicated CI job runs, so the two cannot disagree. After the
  # suite, not before: a type regression and a test failure are different fixes,
  # and pytest's verdict is the one an implementer is usually waiting on.
  # Static analysis only, so it needs neither db-test nor the provider mock, but
  # it shares Dockerfile.test with backend-tests and the build above has already
  # warmed every layer it needs.
  if [[ "${AUDR_SKIP_STATIC:-0}" != "1" ]]; then
    if ! run_phase backend_mypy "mypy (strict) — backend/src, gated against the baseline" \
        "${COMPOSE[@]}" --profile backend run --rm --no-TTY --build backend-mypy; then
      echo "==> mypy FAILED (exit ${PHASE_RC}) — a new error outside backend/mypy-baseline.txt" \
        | tee -a "${ENV_LOG}"
      EXIT="${PHASE_RC}"
    fi
  fi
fi

if [[ "${RUN_FRONTEND}" -eq 1 ]]; then
  echo "==> Installing frontend dependencies"
  FRONTEND_INSTALL_START="$(date +%s)"
  set +e
  # Full install output to .test-env.log, last 20 lines to the console: an
  # `npm ci` that fails on a registry hiccup is a pre-suite death too, and the
  # 20-line tail is rarely where the reason is.
  "${COMPOSE[@]}" --profile frontend run --rm --no-TTY --entrypoint sh frontend-tests \
    -c 'npm ci --no-audit --no-fund' 2>&1 | tee -a "${ENV_LOG}" | tail -20
  FRONTEND_INSTALL_EXIT="${PIPESTATUS[0]}"
  set -e
  record_phase frontend_install "${FRONTEND_INSTALL_EXIT}" "${FRONTEND_INSTALL_START}" \
    "$(date +%s)" "Frontend dependency install (npm ci)"
  if [[ "${FRONTEND_INSTALL_EXIT}" -ne 0 ]]; then
    echo "==> Frontend dependency install FAILED (exit ${FRONTEND_INSTALL_EXIT})"
    EXIT="${FRONTEND_INSTALL_EXIT}"
  else
    echo "==> Running frontend tests (Vitest)"
    FRONTEND_START="$(date +%s)"
    set +e
    "${COMPOSE[@]}" --profile frontend run --rm --no-TTY frontend-tests \
      2>&1 | tee "${ROOT}/.test-frontend.log"
    FRONTEND_EXIT="${PIPESTATUS[0]}"
    set -e
    record_phase frontend_suite "${FRONTEND_EXIT}" "${FRONTEND_START}" "$(date +%s)" \
      "Frontend suite (Vitest)"
    echo "==> Frontend suite exit=${FRONTEND_EXIT}"
    if [[ "${FRONTEND_EXIT}" -ne 0 ]]; then EXIT="${FRONTEND_EXIT}"; fi

    # Generated API types vs. backend/openapi.json (AUD-694). Runs on the host,
    # not in the frontend-tests container: it only needs Node to diff against
    # the already-checked-out schema file, and does not touch Docker.
    echo "==> Checking generated API types against backend/openapi.json"
    API_TYPES_START="$(date +%s)"
    set +e
    (cd "${ROOT}/frontend" && npm run check:api-types)
    API_TYPES_EXIT="$?"
    set -e
    record_phase api_types "${API_TYPES_EXIT}" "${API_TYPES_START}" "$(date +%s)" \
      "Generated API types vs backend/openapi.json"
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
