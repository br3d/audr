"""Baseline schema — squashed from migrations 001–008 (AUD-306).

This single revision reproduces the full schema that the incremental
migrations 001_foundation … 008_fix_integration_kind produced, verified
byte-for-byte against a live upgrade. It replaces those files so the
migration history starts from one clean baseline.

Staging note: data is disposable, so the staging volume is dropped and
recreated from this baseline. Future schema changes get their own
incremental revisions on top of this one.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


_BASELINE_SQL = r"""CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

CREATE TABLE public.asset (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    token_address text NOT NULL,
    symbol text NOT NULL,
    name text NOT NULL,
    decimals smallint NOT NULL,
    source text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    excluded boolean DEFAULT false NOT NULL,
    decimals_override smallint,
    CONSTRAINT ck_asset_ck_asset_address_lower CHECK ((token_address = lower(token_address))),
    CONSTRAINT ck_asset_ck_asset_source CHECK ((source = ANY (ARRAY['catalog'::text, 'manual'::text])))
);

CREATE TABLE public.asset_metadata_revision (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid NOT NULL,
    symbol text NOT NULL,
    name text NOT NULL,
    decimals smallint NOT NULL,
    source text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.balance_observation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    wallet_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    raw_amount numeric(78,0) NOT NULL,
    block_number bigint NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.balance_observation_invalidation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    observation_id uuid NOT NULL,
    reason text NOT NULL,
    invalidated_at timestamp with time zone DEFAULT now() NOT NULL,
    reorg_depth integer,
    CONSTRAINT ck_balance_observation_invalidation_ck_balance_observat_8422 CHECK ((reason = ANY (ARRAY['reorg'::text, 'verification_pending'::text])))
);

CREATE TABLE public.catalog_entry (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    version_id uuid NOT NULL,
    token_address text NOT NULL,
    symbol text NOT NULL,
    name text NOT NULL,
    decimals smallint NOT NULL,
    CONSTRAINT ck_catalog_entry_ck_catalog_entry_address_lower CHECK ((token_address = lower(token_address)))
);

CREATE TABLE public.catalog_version (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    commit_hash text NOT NULL,
    chain_id integer NOT NULL,
    entry_count integer NOT NULL,
    imported_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.discovery_coverage (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    wallet_id uuid NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    checkpoint json
);

CREATE TABLE public.history_point (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_id uuid NOT NULL,
    snapshotted_at timestamp with time zone NOT NULL,
    total_value_usd numeric(36,18),
    quality text NOT NULL,
    included_wallet_count integer NOT NULL,
    included_asset_count integer NOT NULL,
    has_gap boolean DEFAULT false NOT NULL,
    is_canonical boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_history_point_ck_history_point_quality CHECK ((quality = ANY (ARRAY['complete'::text, 'partial'::text, 'stale'::text, 'unknown'::text])))
);

CREATE TABLE public.integration (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    encrypted_blob bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_integration_kind CHECK ((kind = ANY (ARRAY['rpc'::text, 'coingecko'::text])))
);

CREATE TABLE public.job_run (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    worker_id text,
    retry_count integer DEFAULT 0 NOT NULL,
    max_retries integer DEFAULT 3 NOT NULL,
    error text,
    checkpoint json,
    claimed_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_job_run_ck_job_run_status CHECK ((status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'completed'::text, 'failed'::text, 'cancelled'::text])))
);

CREATE TABLE public.key_state (
    name text NOT NULL,
    wrapped_key bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.login_attempt (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attempted_at timestamp with time zone DEFAULT now() NOT NULL,
    success boolean NOT NULL
);

CREATE TABLE public.monitored_pair (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    wallet_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.operational_event (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_type text NOT NULL,
    payload json,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.owner (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    singleton boolean DEFAULT true NOT NULL,
    argon2_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.provider_budget (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    period_start timestamp with time zone NOT NULL,
    calls_used bigint DEFAULT '0'::bigint NOT NULL,
    calls_limit bigint NOT NULL,
    reset_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.provider_purge_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    purged_by text DEFAULT 'owner'::text NOT NULL,
    quote_observations_deleted integer DEFAULT 0 NOT NULL,
    valuation_lines_deleted integer DEFAULT 0 NOT NULL,
    purged_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.quote_observation (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    quote_set_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    price_usd numeric(36,18) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_quote_observation_ck_quote_observation_price_positive CHECK ((price_usd > (0)::numeric))
);

CREATE TABLE public.quote_set (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_quote_set_ck_quote_set_provider CHECK ((provider = 'coingecko'::text)),
    CONSTRAINT ck_quote_set_ck_quote_set_status CHECK ((status = ANY (ARRAY['pending'::text, 'complete'::text, 'failed'::text])))
);

CREATE TABLE public.schedule (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind text NOT NULL,
    cron_expr text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    last_run_at timestamp with time zone,
    next_run_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    paused_at timestamp with time zone,
    freshness_s integer,
    budget_calls_per_day integer,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.session (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    csrf_token text NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_active_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.valuation_line (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshot_id uuid NOT NULL,
    wallet_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    raw_amount numeric(78,0) NOT NULL,
    block_number bigint NOT NULL,
    price_usd numeric(36,18),
    value_usd numeric(36,18),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    observation_id uuid
);

CREATE TABLE public.valuation_snapshot (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    snapshotted_at timestamp with time zone DEFAULT now() NOT NULL,
    quality text NOT NULL,
    published_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_valuation_snapshot_ck_valuation_snapshot_quality CHECK ((quality = ANY (ARRAY['complete'::text, 'partial'::text, 'stale'::text, 'unknown'::text])))
);

CREATE TABLE public.wallet (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    address text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_ck_wallet_address_lower CHECK ((address = lower(address))),
    CONSTRAINT ck_wallet_ck_wallet_status CHECK ((status = ANY (ARRAY['active'::text, 'stopped'::text])))
);

CREATE TABLE public.worker_status (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    worker_id text NOT NULL,
    status text DEFAULT 'idle'::text NOT NULL,
    last_heartbeat_at timestamp with time zone NOT NULL,
    current_job_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_worker_status_ck_worker_status_status CHECK ((status = ANY (ARRAY['idle'::text, 'running'::text, 'stopped'::text])))
);

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT pk_asset PRIMARY KEY (id);

ALTER TABLE ONLY public.asset_metadata_revision
    ADD CONSTRAINT pk_asset_metadata_revision PRIMARY KEY (id);

ALTER TABLE ONLY public.balance_observation
    ADD CONSTRAINT pk_balance_observation PRIMARY KEY (id);

ALTER TABLE ONLY public.balance_observation_invalidation
    ADD CONSTRAINT pk_balance_observation_invalidation PRIMARY KEY (id);

ALTER TABLE ONLY public.catalog_entry
    ADD CONSTRAINT pk_catalog_entry PRIMARY KEY (id);

ALTER TABLE ONLY public.catalog_version
    ADD CONSTRAINT pk_catalog_version PRIMARY KEY (id);

ALTER TABLE ONLY public.discovery_coverage
    ADD CONSTRAINT pk_discovery_coverage PRIMARY KEY (id);

ALTER TABLE ONLY public.history_point
    ADD CONSTRAINT pk_history_point PRIMARY KEY (id);

ALTER TABLE ONLY public.integration
    ADD CONSTRAINT pk_integration PRIMARY KEY (id);

ALTER TABLE ONLY public.job_run
    ADD CONSTRAINT pk_job_run PRIMARY KEY (id);

ALTER TABLE ONLY public.key_state
    ADD CONSTRAINT pk_key_state PRIMARY KEY (name);

ALTER TABLE ONLY public.login_attempt
    ADD CONSTRAINT pk_login_attempt PRIMARY KEY (id);

ALTER TABLE ONLY public.monitored_pair
    ADD CONSTRAINT pk_monitored_pair PRIMARY KEY (id);

ALTER TABLE ONLY public.operational_event
    ADD CONSTRAINT pk_operational_event PRIMARY KEY (id);

ALTER TABLE ONLY public.owner
    ADD CONSTRAINT pk_owner PRIMARY KEY (id);

ALTER TABLE ONLY public.provider_budget
    ADD CONSTRAINT pk_provider_budget PRIMARY KEY (id);

ALTER TABLE ONLY public.provider_purge_log
    ADD CONSTRAINT pk_provider_purge_log PRIMARY KEY (id);

ALTER TABLE ONLY public.quote_observation
    ADD CONSTRAINT pk_quote_observation PRIMARY KEY (id);

ALTER TABLE ONLY public.quote_set
    ADD CONSTRAINT pk_quote_set PRIMARY KEY (id);

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT pk_schedule PRIMARY KEY (id);

ALTER TABLE ONLY public.session
    ADD CONSTRAINT pk_session PRIMARY KEY (id);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT pk_valuation_line PRIMARY KEY (id);

ALTER TABLE ONLY public.valuation_snapshot
    ADD CONSTRAINT pk_valuation_snapshot PRIMARY KEY (id);

ALTER TABLE ONLY public.wallet
    ADD CONSTRAINT pk_wallet PRIMARY KEY (id);

ALTER TABLE ONLY public.worker_status
    ADD CONSTRAINT pk_worker_status PRIMARY KEY (id);

ALTER TABLE ONLY public.asset
    ADD CONSTRAINT uq_asset_token_address UNIQUE (token_address);

ALTER TABLE ONLY public.balance_observation_invalidation
    ADD CONSTRAINT uq_balance_observation_invalidation_observation_id UNIQUE (observation_id);

ALTER TABLE ONLY public.catalog_version
    ADD CONSTRAINT uq_catalog_version_commit UNIQUE (commit_hash);

ALTER TABLE ONLY public.history_point
    ADD CONSTRAINT uq_history_point_snapshot_id UNIQUE (snapshot_id);

ALTER TABLE ONLY public.integration
    ADD CONSTRAINT uq_integration_kind UNIQUE (kind);

ALTER TABLE ONLY public.monitored_pair
    ADD CONSTRAINT uq_monitored_pair UNIQUE (wallet_id, asset_id);

ALTER TABLE ONLY public.quote_observation
    ADD CONSTRAINT uq_quote_observation_set_asset UNIQUE (quote_set_id, asset_id);

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT uq_schedule_kind UNIQUE (kind);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT uq_valuation_line_snapshot_wallet_asset UNIQUE (snapshot_id, wallet_id, asset_id);

ALTER TABLE ONLY public.wallet
    ADD CONSTRAINT uq_wallet_address UNIQUE (address);

ALTER TABLE ONLY public.worker_status
    ADD CONSTRAINT uq_worker_status_worker_id UNIQUE (worker_id);

CREATE INDEX ix_asset_metadata_revision_asset_id ON public.asset_metadata_revision USING btree (asset_id);

CREATE INDEX ix_balance_observation_invalidation_invalidated_at ON public.balance_observation_invalidation USING btree (invalidated_at);

CREATE INDEX ix_balance_observation_invalidation_observation_id ON public.balance_observation_invalidation USING btree (observation_id);

CREATE INDEX ix_balance_observation_wallet_asset ON public.balance_observation USING btree (wallet_id, asset_id, observed_at);

CREATE UNIQUE INDEX ix_catalog_entry_version_address ON public.catalog_entry USING btree (version_id, token_address);

CREATE INDEX ix_discovery_coverage_wallet_id ON public.discovery_coverage USING btree (wallet_id);

CREATE INDEX ix_history_point_canonical_snapshotted_at ON public.history_point USING btree (is_canonical, snapshotted_at);

CREATE INDEX ix_history_point_snapshot_id ON public.history_point USING btree (snapshot_id);

CREATE INDEX ix_history_point_snapshotted_at ON public.history_point USING btree (snapshotted_at);

CREATE INDEX ix_job_run_heartbeat_at ON public.job_run USING btree (heartbeat_at);

CREATE INDEX ix_job_run_kind_status ON public.job_run USING btree (kind, status);

CREATE INDEX ix_job_run_status ON public.job_run USING btree (status);

CREATE INDEX ix_login_attempt_attempted_at ON public.login_attempt USING btree (attempted_at);

CREATE INDEX ix_operational_event_type_occurred ON public.operational_event USING btree (event_type, occurred_at);

CREATE INDEX ix_provider_budget_provider_period ON public.provider_budget USING btree (provider, period_start);

CREATE INDEX ix_quote_observation_set_asset ON public.quote_observation USING btree (quote_set_id, asset_id);

CREATE INDEX ix_quote_set_fetched_at ON public.quote_set USING btree (fetched_at);

CREATE INDEX ix_session_expires_at ON public.session USING btree (expires_at);

CREATE INDEX ix_valuation_line_observation_id ON public.valuation_line USING btree (observation_id);

CREATE INDEX ix_valuation_line_snapshot_id ON public.valuation_line USING btree (snapshot_id);

CREATE INDEX ix_valuation_line_wallet_asset ON public.valuation_line USING btree (wallet_id, asset_id);

CREATE INDEX ix_valuation_snapshot_published_at ON public.valuation_snapshot USING btree (published_at);

CREATE INDEX ix_valuation_snapshot_snapshotted_at ON public.valuation_snapshot USING btree (snapshotted_at);

CREATE UNIQUE INDEX uq_job_run_kind_active ON public.job_run USING btree (kind) WHERE (status = 'in_progress'::text);

CREATE UNIQUE INDEX uq_owner_singleton ON public.owner USING btree (singleton) WHERE (singleton = true);

ALTER TABLE ONLY public.asset_metadata_revision
    ADD CONSTRAINT fk_asset_metadata_revision_asset FOREIGN KEY (asset_id) REFERENCES public.asset(id);

ALTER TABLE ONLY public.balance_observation
    ADD CONSTRAINT fk_balance_observation_asset FOREIGN KEY (asset_id) REFERENCES public.asset(id);

ALTER TABLE ONLY public.balance_observation_invalidation
    ADD CONSTRAINT fk_balance_observation_invalidation_observation_id FOREIGN KEY (observation_id) REFERENCES public.balance_observation(id);

ALTER TABLE ONLY public.balance_observation
    ADD CONSTRAINT fk_balance_observation_wallet FOREIGN KEY (wallet_id) REFERENCES public.wallet(id);

ALTER TABLE ONLY public.catalog_entry
    ADD CONSTRAINT fk_catalog_entry_version FOREIGN KEY (version_id) REFERENCES public.catalog_version(id);

ALTER TABLE ONLY public.discovery_coverage
    ADD CONSTRAINT fk_discovery_coverage_wallet FOREIGN KEY (wallet_id) REFERENCES public.wallet(id);

ALTER TABLE ONLY public.history_point
    ADD CONSTRAINT fk_history_point_snapshot_id FOREIGN KEY (snapshot_id) REFERENCES public.valuation_snapshot(id);

ALTER TABLE ONLY public.monitored_pair
    ADD CONSTRAINT fk_monitored_pair_asset FOREIGN KEY (asset_id) REFERENCES public.asset(id);

ALTER TABLE ONLY public.monitored_pair
    ADD CONSTRAINT fk_monitored_pair_wallet FOREIGN KEY (wallet_id) REFERENCES public.wallet(id);

ALTER TABLE ONLY public.quote_observation
    ADD CONSTRAINT fk_quote_observation_asset FOREIGN KEY (asset_id) REFERENCES public.asset(id);

ALTER TABLE ONLY public.quote_observation
    ADD CONSTRAINT fk_quote_observation_quote_set FOREIGN KEY (quote_set_id) REFERENCES public.quote_set(id);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT fk_valuation_line_asset FOREIGN KEY (asset_id) REFERENCES public.asset(id);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT fk_valuation_line_observation_id_balance_observation FOREIGN KEY (observation_id) REFERENCES public.balance_observation(id);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT fk_valuation_line_snapshot FOREIGN KEY (snapshot_id) REFERENCES public.valuation_snapshot(id);

ALTER TABLE ONLY public.valuation_line
    ADD CONSTRAINT fk_valuation_line_wallet FOREIGN KEY (wallet_id) REFERENCES public.wallet(id);

ALTER TABLE ONLY public.worker_status
    ADD CONSTRAINT fk_worker_status_current_job_run_id_job_run FOREIGN KEY (current_job_run_id) REFERENCES public.job_run(id) ON DELETE SET NULL;
"""


def upgrade() -> None:
    bind = op.get_bind()
    for statement in (s.strip() for s in _BASELINE_SQL.split(";")):
        if statement:
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    _tables = [
        "asset",
        "asset_metadata_revision",
        "balance_observation",
        "balance_observation_invalidation",
        "catalog_entry",
        "catalog_version",
        "discovery_coverage",
        "history_point",
        "integration",
        "job_run",
        "key_state",
        "login_attempt",
        "monitored_pair",
        "operational_event",
        "owner",
        "provider_budget",
        "provider_purge_log",
        "quote_observation",
        "quote_set",
        "schedule",
        "session",
        "valuation_line",
        "valuation_snapshot",
        "wallet",
        "worker_status",
    ]
    bind = op.get_bind()
    for table in _tables:
        bind.exec_driver_sql(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
