import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Decimal } from 'decimal.js'
import { fetchPortfolio, fetchHistory, fetchEvents, ApiError } from '../api/client'
import type { HistoryPeriod, PortfolioQuality, HistoryPoint } from '../api/client'
import type { MainPage } from '../components/Layout'
import MoneyValue from '../components/MoneyValue'
import AllocationList from '../components/AllocationList'
import HistoryChart from '../components/HistoryChart'
import NewsFeed from '../components/NewsFeed'
import ScanStatus from '../components/ScanStatus'

const RECENT_EVENTS_LIMIT = 7

// Folio's reference design shows four range segments (day/month/3 months/year),
// but the history API only serves these periods today — 3-month and 1-year
// windows need backend support (tracked separately) before they can be added.
const RANGE_OPTIONS: { value: HistoryPeriod; label: string }[] = [
  { value: '24h', label: '1D' },
  { value: '7d', label: '1W' },
  { value: '30d', label: '1M' },
  { value: 'all', label: 'All' },
]

function formatHuman(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatShortDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function shortenAddress(address: string): string {
  if (address.length <= 12) return address
  return `${address.slice(0, 6)}…${address.slice(-4)}`
}

interface HistoryChange {
  sign: '+' | '-'
  absUsd: string
  pctLabel: string | null
}

// Change from the first to the last priced point in the window — not the
// backend's own delta, since history points can carry a null total_value_usd
// (gaps) that must not be treated as a real zero.
export function computeHistoryChange(entries: HistoryPoint[]): HistoryChange | null {
  const priced = entries
    .filter((e): e is HistoryPoint & { total_value_usd: string } => e.total_value_usd !== null)
    .slice()
    .sort((a, b) => a.snapshotted_at.localeCompare(b.snapshotted_at))

  if (priced.length < 2) return null

  const first = new Decimal(priced[0].total_value_usd)
  const last = new Decimal(priced[priced.length - 1].total_value_usd)
  const diff = last.minus(first)
  const sign = diff.isNegative() ? '-' : '+'
  const absUsd = diff.abs().toFixed(2)
  const pctLabel = first.isZero()
    ? null
    : `${sign}${diff.abs().div(first).times(100).toFixed(2)}%`

  return { sign, absUsd, pctLabel }
}

function QualityNotices({ quality }: { quality: PortfolioQuality }) {
  const notices: string[] = []

  if (quality.stale_balances) notices.push('Some balance reads are stale.')
  if (quality.stale_prices) notices.push('Some price data is stale.')
  if (quality.incomplete)
    notices.push('Subtotal shown — not all holdings have usable balances and prices.')
  if (quality.mixed_observation_times)
    notices.push('Holdings were observed at different times.')
  if (quality.discovery_overdue) notices.push('Token discovery is overdue.')
  if (quality.verification_pending) notices.push('Block verification is pending.')
  if (quality.invalidated) notices.push('Some observations have been invalidated.')

  if (notices.length === 0) return null

  return (
    <ul role="list" aria-label="Data quality notices" className="notice-list">
      {notices.map((n) => (
        <li key={n} role="note" className="notice-item">
          {n}
        </li>
      ))}
    </ul>
  )
}

interface Props {
  setPage: (page: MainPage) => void
}

export default function DashboardPage({ setPage }: Props) {
  const { data, error, isLoading } = useQuery({
    queryKey: ['portfolio'],
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  const [historyPeriod, setHistoryPeriod] = useState<HistoryPeriod>('30d')

  const historyQuery = useQuery({
    queryKey: ['history', historyPeriod],
    queryFn: () => fetchHistory(historyPeriod),
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  })

  const history24hQuery = useQuery({
    queryKey: ['history', '24h' as const],
    queryFn: () => fetchHistory('24h'),
    refetchInterval: 60_000,
  })

  const recentEventsQuery = useQuery({
    queryKey: ['events', 'recent'],
    queryFn: () => fetchEvents({ limit: RECENT_EVENTS_LIMIT, offset: 0 }),
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <p aria-busy="true">Loading dashboard…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load portfolio.{' '}
        {error instanceof ApiError ? error.message : 'Please try again.'}
      </p>
    )
  }

  if (!data) return null

  const hasHoldings = data.holdings.length > 0
  const allExcluded = hasHoldings && data.holdings.every((h) => !h.included)
  const pricedAllocations = data.allocations.filter((a) => a.value_usd !== null)
  const hasPricedAllocations = pricedAllocations.length > 0
  const isComplete = data.total_usd !== null
  const totalValue = data.total_usd ?? data.priced_subtotal_usd
  const pricedCount = data.holdings.filter((h) => h.value_usd !== null && h.included).length

  const historyChange = history24hQuery.data
    ? computeHistoryChange(history24hQuery.data.items)
    : null

  const trackedNote =
    data.holdings.length === 0
      ? 'No wallets tracked yet'
      : pricedCount === data.holdings.length
        ? `All ${data.holdings.length} priced`
        : `${pricedCount} of ${data.holdings.length} priced`

  const pricedAssetsNote = data.balance_block_time
    ? `as of ${formatShortDate(data.balance_block_time)}`
    : 'No balance snapshot yet'

  const headerSubtitle = data.balance_block_time
    ? `Balances as of ${formatHuman(data.balance_block_time)}${
        data.balance_observed_at ? ` · read ${formatHuman(data.balance_observed_at)}` : ''
      }`
    : 'No balance snapshot yet'

  const recentEvents = recentEventsQuery.data?.events ?? []

  return (
    <div>
      <div className="page-header-row">
        <div>
          <div className="page-header-breadcrumb">Dashboard</div>
          <h2 className="page-heading">Overview</h2>
          <p className="page-subheading">{headerSubtitle}</p>
        </div>
        <div className="btn-group">
          {/* AUD-408: Assets left the sidebar, so the Dashboard owns the only
              always-visible entry point into the token registry. */}
          <button type="button" className="btn btn-secondary" onClick={() => setPage('assets')}>
            Manage assets
          </button>
          <button type="button" className="btn btn-primary" onClick={() => setPage('wallets')}>
            + Add Wallet
          </button>
        </div>
      </div>

      <QualityNotices quality={data.quality} />

      {allExcluded && (
        <p role="note" className="alert alert-warning mb-16">
          All holdings are excluded from the total.{' '}
          <button type="button" className="link-button" onClick={() => setPage('assets')}>
            Manage exclusions in Assets
          </button>
          .
        </p>
      )}

      {/* Metric cards */}
      <div className="metrics-grid">
        <section aria-label="Portfolio total" className="metric-card">
          <div className="metric-label">Portfolio Value</div>
          {totalValue !== null ? (
            <>
              <div className={`metric-value${isComplete ? '' : ' metric-value-sm'}`}>
                <MoneyValue value={totalValue} emphasizeInteger />
              </div>
              {historyChange && (
                <div
                  className={`metric-change ${
                    historyChange.sign === '+'
                      ? 'metric-change-positive'
                      : 'metric-change-negative'
                  }`}
                >
                  24h {historyChange.sign}
                  <MoneyValue value={historyChange.absUsd} />
                  {historyChange.pctLabel && <> ({historyChange.pctLabel})</>}
                </div>
              )}
              {!isComplete && (
                <div className="metric-note text-warning">Subtotal (incomplete)</div>
              )}
              {data.stale_contribution_usd !== null && (
                <div className="metric-note text-warning">
                  Includes <MoneyValue value={data.stale_contribution_usd} /> from stale reads
                </div>
              )}
            </>
          ) : (
            <div className="metric-value metric-unknown">—</div>
          )}
          {totalValue === null && (
            <div className="metric-note">No priced holdings yet</div>
          )}
        </section>

        <div className="metric-card">
          <div className="metric-label">Tracked Holdings</div>
          <div className="metric-value metric-value-sm">{data.holdings.length}</div>
          <div className="metric-note">{trackedNote}</div>
        </div>

        <div className="metric-card">
          <div className="metric-label">Priced Assets</div>
          <div className="metric-value metric-value-sm">{pricedAllocations.length}</div>
          <div className="metric-note">{pricedAssetsNote}</div>
        </div>
      </div>

      {!hasHoldings && (
        <p role="note" className="alert alert-info mb-16">
          No holdings found. Add a wallet and run a balance refresh.
        </p>
      )}

      {/* Portfolio value history */}
      {!historyQuery.isLoading &&
        !historyQuery.isError &&
        historyQuery.data &&
        historyQuery.data.items.length > 0 && (
          <section aria-label="Portfolio value history" className="card mb-20">
            <div className="card-header">
              <div className="card-title card-title-chart">Portfolio value</div>
              <div className="range-switcher" role="group" aria-label="History range">
                {RANGE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    className="range-switcher-btn"
                    aria-pressed={historyPeriod === opt.value}
                    onClick={() => setHistoryPeriod(opt.value)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
            <HistoryChart points={historyQuery.data.items} period={historyPeriod} />
          </section>
        )}

      {/* Allocation */}
      {data.allocations.length > 0 ? (
        <section aria-label="Asset allocation" className="card mb-20">
          <div className="card-header">
            <div className="card-title">Asset Allocation</div>
            <div className="btn-group">
              <ScanStatus runId={null} kind="balances" label="Refresh balances" />
              <ScanStatus runId={null} kind="discovery" label="Discover tokens" />
            </div>
          </div>
          <AllocationList items={data.allocations} />
        </section>
      ) : (
        hasHoldings && (
          <p role="note" className="alert alert-info mb-16">
            No priced holdings — allocation unavailable. Configure a quote provider in
            Connections.
          </p>
        )
      )}

      {/* News */}
      {hasPricedAllocations && (
        <section aria-label="Asset news" className="mb-20">
          <div className="section-heading">News</div>
          <NewsFeed items={pricedAllocations} />
        </section>
      )}

      {/* Recent events */}
      <section aria-label="Recent events" className="mb-20">
        <div className="row-between mb-12">
          <div className="section-heading" style={{ marginBottom: 0 }}>
            Recent Events
          </div>
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            onClick={() => setPage('events')}
          >
            View all
          </button>
        </div>
        <div className="card">
          {recentEvents.length > 0 ? (
            <ul className="recent-events-list" role="list">
              {recentEvents.map((e) => (
                <li key={e.id} className="recent-events-row">
                  <div className="recent-events-main">
                    {e.event_type === 'transfer_in' ? (
                      <span className="badge badge-ok">in</span>
                    ) : (
                      <span className="badge badge-neutral">out</span>
                    )}
                    <span className="mono" title={e.token_address}>
                      {shortenAddress(e.token_address)}
                    </span>
                  </div>
                  <span className="recent-events-meta">{formatHuman(e.indexed_at)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p role="note" className="muted-text">
              No on-chain events indexed yet.
            </p>
          )}
        </div>
      </section>
    </div>
  )
}
