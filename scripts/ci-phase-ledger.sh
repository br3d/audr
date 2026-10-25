#!/usr/bin/env bash
# Replay scripts/test.sh's phase ledger into GitHub Actions step outputs (AUD-715).
#
# Usage: scripts/ci-phase-ledger.sh [ledger-path]        (default .test-phases.tsv)
#
# WHY THIS EXISTS. The GitHub `tests` job runs scripts/test.sh as a single step,
# so a red run's job metadata says only "Backend + frontend suites: failure" —
# it does not say whether the database never came up, the images failed to
# build, pytest failed, `npm ci` failed, Vitest failed, or the generated API
# types drifted. Those are five unrelated diagnoses with five unrelated fixes.
#
# The log that distinguishes them is uploaded as an artifact, and **no agent on
# this project can read an artifact's contents**: the download endpoint requires
# authentication, `/actions/jobs/<id>/logs` is 403 without repository admin, and
# `gh` is not installed on the agent host (AUD-714). What *is* readable
# tokenlessly on a public repo is run / job / **step** metadata — names,
# conclusions and timestamps. That channel is how the AUD-714 diagnosis was
# actually done.
#
# So this script moves the phase boundaries into that channel. test.sh records
# one ledger row per phase as it ends; this replays the ledger into one step
# output per phase, and tests.yml turns each output into its own workflow step.
# A reader with no credentials then sees, in `GET /actions/jobs/<id>`:
#
#   phase: db-test up + healthcheck    success
#   phase: backend test images build   success
#   phase: backend suite (pytest)      failure      <- the diagnosis
#   phase: frontend dependency install skipped
#
# Output contract, for every phase in PHASES below, whether or not the run
# reached it:
#
#   <slug>=pass|fail|skip      pass = rc 0, fail = rc non-zero, skip = no row
#   <slug>_detail=rc=<n>, <d>s human detail for the step log
#
# `skip` is reported explicitly rather than by omitting the output, because an
# absent output reads as the empty string in a workflow `if:` — which is neither
# 'pass' nor 'skip' and would quietly turn "never reached" into a failed step.
#
# A phase missing from PHASES is a phase no reader can see, so keep this list in
# step with test.sh's record_phase calls. The ledger carrying a slug this script
# does not know is reported too (see the trailing loop) rather than dropped: an
# unknown phase is a maintenance bug, and swallowing it would hide exactly the
# failure this script exists to surface.
set -euo pipefail

LEDGER="${1:-.test-phases.tsv}"

# Order matters: this is the order the steps appear in, and so the order a
# reader sees. Keep it in execution order.
# `ruff` and `backend_mypy` are the static gates (AUD-744). Both report `skip`
# in the GitHub `tests` job, which sets AUDR_SKIP_STATIC=1 because the dedicated
# `lint` and `backend-mypy` jobs cover them in parallel; they carry a real
# verdict for anyone running test.sh locally, which is where they exist to bite.
# `image_pull` is first on purpose (AUD-747): it is the one phase that can fail
# for a reason wholly outside the tree — Docker Hub's anonymous allowance is per
# source IP and this host shares it with every other agent and with the Gitea
# runner. Red there means re-run or warm the image, never read the diff.
# `migration_smoke` sits between the build and the suite (AUD-698). Red there is
# not a test failure: it means the newest revision will not downgrade, and so
# the guarded deploy could not roll this commit back. Different verdict, and a
# different fix, from a red `backend_suite`.
PHASES=(
  image_pull
  ruff
  db_up
  backend_build
  migration_smoke
  backend_suite
  backend_mypy
  frontend_install
  frontend_suite
  api_types
)

emit() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s=%s\n' "$1" "$2" >>"${GITHUB_OUTPUT}"
  fi
  return 0
}

if [[ ! -f "${LEDGER}" ]]; then
  # Not an error. test.sh truncates the ledger on its first lines, so a missing
  # file means the run died before test.sh ran at all — which is itself the
  # finding, and is already reported by the `test-env` artifact going missing.
  # Every phase then reports `skip`, and the whole phase block shows as skipped.
  echo "no phase ledger at ${LEDGER}: the run died before scripts/test.sh wrote one"
fi

for slug in "${PHASES[@]}"; do
  state=skip
  rc=""
  detail="not reached"

  if [[ -f "${LEDGER}" ]]; then
    # First row wins: a phase runs once per invocation, and taking the first
    # keeps the output deterministic if that ever stops being true.
    row="$(awk -F'\t' -v s="${slug}" '$1 == s { print; exit }' "${LEDGER}")" || row=""
    if [[ -n "${row}" ]]; then
      rc="$(cut -f2 <<<"${row}")"
      start="$(cut -f3 <<<"${row}")"
      end="$(cut -f4 <<<"${row}")"
      if [[ "${rc}" == "0" ]]; then state=pass; else state=fail; fi
      detail="rc=${rc}"
      # Guard the arithmetic: a truncated ledger row (killed mid-write) must
      # degrade to a missing duration, not abort the whole replay.
      if [[ "${start}" =~ ^[0-9]+$ && "${end}" =~ ^[0-9]+$ ]]; then
        detail="${detail}, $(( end - start ))s"
      fi
    fi
  fi

  emit "${slug}" "${state}"
  emit "${slug}_detail" "${detail}"
  printf '%-18s %-5s %s\n' "${slug}" "${state}" "${detail}"
done

# Slugs the ledger carries that PHASES does not. Reported loudly in the step log
# but deliberately NOT failed on: this script runs with `if: always()` on a job
# that may already be red for the real reason, and turning a stale phase list
# into a second, unrelated red would bury it.
#
# Guarded on the file existing rather than relying on `cut` failing quietly:
# under `set -o pipefail` a missing ledger makes `cut` exit 1, which fails the
# whole pipeline and — with `set -e` — the script. That would turn "the run died
# before test.sh wrote a ledger" into a red *replay* step, i.e. noise on top of
# the real cause, on the one failure mode this file exists to keep legible.
[[ -f "${LEDGER}" ]] || exit 0

cut -f1 "${LEDGER}" | sort -u | while IFS= read -r slug; do
  [[ -z "${slug}" ]] && continue
  known=0
  for known_slug in "${PHASES[@]}"; do
    if [[ "${slug}" == "${known_slug}" ]]; then
      known=1
      break
    fi
  done
  if [[ "${known}" -eq 0 ]]; then
    echo "WARNING: ledger phase '${slug}' is not in scripts/ci-phase-ledger.sh's PHASES;" \
      "no workflow step reports it. Add it there and in .github/workflows/tests.yml."
  fi
done
