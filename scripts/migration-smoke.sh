#!/usr/bin/env bash
# Prove the newest Alembic revisions are single-headed and actually reversible
# (AUD-698).
#
#   docker compose -f compose.test.yaml --profile backend run --rm migration-smoke
#
# Runs INSIDE the runtime image, against the ephemeral test database, with the
# same DATABASE_URL and the same `python -m alembic` entrypoint that
# `migrate-test` and the deploy both use. Nothing here touches a real database.
#
# WHY THIS EXISTS
# ---------------
# The guarded deploy's rollback path is built on `alembic downgrade` succeeding
# (ci/gitea-overlay/workflows/deploy.yaml, "Guarded deploy"). When it does not,
# the workflow has nothing left to try: it says so and leaves the stack on the
# new build, which is the one state the rollback existed to avoid. So an
# irreversible migration is not a cosmetic omission — it is a one-way door, and
# until this script existed nothing in CI opened that door to check.
#
# Two properties, both cheap, both impossible to assert without a real engine:
#
#   1. ONE HEAD. `alembic upgrade head` on a tree with two heads fails with
#      "Multiple head revisions are present"; `downgrade` on one is worse,
#      because it picks a branch. Two heads is what a merge of two branches that
#      each added a migration produces, and it is invisible in review — both
#      files look fine, it is the *pair* that is wrong.
#   2. THE NEWEST REVISIONS GO DOWN AND BACK UP. Downgrades are written by hand,
#      run by nobody, and are usually wrong in a way only Postgres will say:
#      a dropped constraint that was never named, a CHECK restored with a value
#      the data no longer satisfies, a column dropped that a later index needs.
#
# Reversibility is checked over the newest AUDR_MIGRATION_SMOKE_DEPTH revisions
# (default 3) rather than the whole chain. The depth is a deliberate trade:
#
#   * a branch normally adds one migration and sometimes two or three, so 3
#     covers the revisions actually under review — the ones nobody has ever run
#     a downgrade for;
#   * the full chain would re-litigate 40-odd merged revisions on every push,
#     and three of them are knowingly irreversible by design (data repairs —
#     see backend/tests/unit/test_migration_chain.py, which is where that
#     allowlist lives and is enforced). Walking all the way down would red the
#     suite on every branch for a decision taken years of commits ago.
#
# Raise it with AUDR_MIGRATION_SMOKE_DEPTH when a branch adds more than three,
# and set it to 0 to check the heads assertion alone.
#
# The database is left AT HEAD, which is what makes this safe to run before the
# backend suite against the same ephemeral database: the final `upgrade head` is
# not a courtesy, it is the postcondition, and it is asserted below.
set -euo pipefail

DEPTH="${AUDR_MIGRATION_SMOKE_DEPTH:-3}"
ALEMBIC=(python -m alembic)

fail() {
  echo "==> MIGRATION SMOKE FAILED: $*" >&2
  exit 1
}

# ── 1. exactly one head ───────────────────────────────────────────────────────
#
# `alembic heads` prints one line per head. It exits 0 with two of them, so the
# count is the assertion — the exit code says nothing.
echo "==> alembic heads"
heads_out="$("${ALEMBIC[@]}" heads 2>&1)" || fail "\`alembic heads\` exited non-zero:
${heads_out}"
printf '%s\n' "${heads_out}"

# Blank lines and alembic's own chatter are dropped; a head line is
# `<revision> (head)` or `<revision> (head), <branch label>`.
head_count="$(printf '%s\n' "${heads_out}" | grep -c '(head' || true)"
if [ "${head_count}" -ne 1 ]; then
  fail "expected exactly 1 head, found ${head_count}.

Two heads means two revisions share a down_revision — the normal way to get
there is merging two branches that each added a migration. \`alembic upgrade
head\` refuses to run against it, so the next deploy cannot migrate at all.

Fix it by rebasing one of the two onto the other (re-point its down_revision at
the other's revision id), NOT with \`alembic merge\`: a merge revision makes the
chain non-linear, and the deploy's rollback walks it with \`downgrade -1\`."
fi
echo "==> one head, as required"

# Head revision id, for the postcondition below. Stripped of alembic's
# annotations: `0043 (head)` -> `0043`.
head_rev="$(printf '%s\n' "${heads_out}" | grep '(head' | head -1 | awk '{print $1}')"

# ── 2. upgrade -> downgrade -> upgrade over the newest revisions ──────────────
echo "==> alembic upgrade head (from whatever this database is at)"
"${ALEMBIC[@]}" upgrade head || fail "\`alembic upgrade head\` failed — the forward path is broken, never mind the reverse"

if [ "${DEPTH}" -le 0 ]; then
  echo "==> AUDR_MIGRATION_SMOKE_DEPTH=${DEPTH}: skipping the reversibility leg by request"
  exit 0
fi

echo "==> alembic downgrade -${DEPTH} (the leg the deploy's rollback depends on)"
if ! "${ALEMBIC[@]}" downgrade "-${DEPTH}"; then
  fail "\`alembic downgrade -${DEPTH}\` failed.

This is the exact state the guarded deploy cannot recover from: on a failed
health check it runs \`alembic downgrade\` to put the schema back, and when that
fails it has nothing left to try — it reports 'downgrade failed' and leaves the
stack on the new build (ci/gitea-overlay/workflows/deploy.yaml).

So this is not a test-only problem. Either give the newest revision(s) a
downgrade() that really reverses them, or — if the change genuinely cannot be
reversed, as a destructive data repair cannot — say so deliberately: add the
revision to the irreversible allowlist in
backend/tests/unit/test_migration_chain.py, with the reason, which is also what
documents it for whoever is reading a failed deploy at the time."
fi

echo "==> alembic upgrade head (re-apply, and leave the database where we found it)"
"${ALEMBIC[@]}" upgrade head || fail "the database went down ${DEPTH} revision(s) but will not come back up.

A downgrade that succeeds and then blocks its own re-upgrade is worse than one
that fails outright: the deploy's rollback would report success and leave a
schema no later deploy can migrate forward."

# ── 3. postcondition: back at head ───────────────────────────────────────────
#
# Asserted rather than assumed, because the backend suite runs against this same
# ephemeral database and reads it as already-migrated. A smoke run that exited 0
# having left the schema three revisions back would show up as unrelated
# "column does not exist" failures in another phase entirely.
current_out="$("${ALEMBIC[@]}" current 2>&1)" || fail "\`alembic current\` exited non-zero after the cycle:
${current_out}"
printf '%s\n' "${current_out}"
case "${current_out}" in
  *"${head_rev}"*) ;;
  *) fail "after the cycle the database is not at ${head_rev} — the backend suite would run against a half-migrated schema" ;;
esac

echo "==> migration smoke passed: 1 head, newest ${DEPTH} revision(s) reverse and re-apply, database at ${head_rev}"
