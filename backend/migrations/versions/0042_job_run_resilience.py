"""Job store resilience: dead_letter terminal state + origin tag (AUD-689).

From the architecture audit (AUD-685), `job_run` had three gaps:

1. `_expire_stale_leases` recycled a stale `in_progress` run back to
   `pending` unconditionally. A poison-pill job that hard-kills the worker
   (OOM/SIGKILL) before it can report a failure gets re-leased forever —
   there was no terminal state for "this lease keeps expiring, give up."
   `dead_letter` is that terminal state, bounded by the row's own
   `max_retries`.

2. The same unconditional recycle-to-`pending` is what let an expired
   *scheduled* run (`claim_job` origin) bypass both the schedule's
   freshness gate and the AUD-356 consecutive-failure backoff streak:
   `Worker.run_once` always checks `claim_pending_job` first, and a
   recycled-to-`pending` row is indistinguishable from a legitimate
   on-demand request sitting in that same queue. Recycling a scheduled
   run's lease to `failed` instead (this migration's behaviour change,
   in `jobs/store.py`) removes it from that queue and feeds it into the
   backoff streak `claim_job` already counts `failed` rows against — no
   new gate needed, just no longer bypassing the existing one.

3. An on-demand ("Run now") job that fails had no way back to the user's
   click — it just sat `failed`. `origin` distinguishes a `claim_job`-
   started row (`'scheduled'`) from an `enqueue_job`-started one
   (`'on_demand'`) so `fail_job`/the lease-expiry path above know which
   failures are safe to auto-requeue (bounded by `max_retries`) instead of
   silently dropping the request.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from alembic import op

revision = "0042"
down_revision: str | None = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE job_run ADD COLUMN origin text NOT NULL DEFAULT 'scheduled'"
    )
    op.execute(
        "ALTER TABLE job_run ADD CONSTRAINT ck_job_run_origin"
        " CHECK (origin = ANY (ARRAY['scheduled'::text, 'on_demand'::text]))"
    )

    op.execute("ALTER TABLE job_run DROP CONSTRAINT ck_job_run_ck_job_run_status")
    op.execute(
        "ALTER TABLE job_run ADD CONSTRAINT ck_job_run_ck_job_run_status"
        " CHECK (status = ANY (ARRAY["
        "'pending'::text, 'in_progress'::text, 'completed'::text,"
        " 'failed'::text, 'cancelled'::text, 'dead_letter'::text"
        "]))"
    )


def downgrade() -> None:
    op.execute("UPDATE job_run SET status = 'failed' WHERE status = 'dead_letter'")
    op.execute("ALTER TABLE job_run DROP CONSTRAINT ck_job_run_ck_job_run_status")
    op.execute(
        "ALTER TABLE job_run ADD CONSTRAINT ck_job_run_ck_job_run_status"
        " CHECK (status = ANY (ARRAY["
        "'pending'::text, 'in_progress'::text, 'completed'::text,"
        " 'failed'::text, 'cancelled'::text"
        "]))"
    )

    op.execute("ALTER TABLE job_run DROP CONSTRAINT ck_job_run_origin")
    op.execute("ALTER TABLE job_run DROP COLUMN origin")
