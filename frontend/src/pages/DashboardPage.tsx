import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Decimal } from 'decimal.js'
import { fetchPortfolio, fetchHistory, fetchEvents, patchAsset } from '../api/client'
import type { HistoryPeriod, PortfolioQuality, HistoryPoint } from '../api/client'
import type { MainPage } from '../components/Layout'
import { applyExclusionOverrides, pruneSettledOverrides } from '../portfolio/exclusions'
import type { ExcludeOverrides } from '../portfolio/exclusions'
import { queryKeys, invalidatePortfolioGroup } from '../api/queryKeys'
import { errorMessage } from '../api/errorMessage'
import MoneyValue from '../components/MoneyValue'
import AllocationList from '../components/AllocationList'
import AllocationBar from '../components/AllocationBar'
import NewsFeed from '../components/NewsFeed'
import ScanStatus from '../components/ScanStatus'
import NoticeBadge from '../components/NoticeBadge'
import EventRow from '../components/EventRow'
import { shortenAddress } from '../portfolio/events'

// Recharts (+ its d3 tree) is the heaviest dependency in the bundle and this
// is its only consumer, so it's loaded on demand instead of in the main chunk.
const HistoryChart = lazy(() => import('../components/HistoryChart'))

const RECENT_EVENTS_LIMIT = 7

// AUD-455: the standalone History page was removed, so this switcher carries
// the full set of windows the history API serves (3M and 1Y since AUD-376).
const RANGE_OPTIONS: { value: HistoryPeriod; label: string }[] = [
  { value: '24h', label: '1D' },
  { value: '7d', label: '1W' },
  { value: '30d', label: '1M' },
  { value: '90d', label: '3M' },
  { value: '1y', label: '1Y' },
  { value: 'all', label: 'All' },
]

// AUD-455: moved over from the removed History page — the chart draws gap
// markers and quality shading, but what those mean only reads as an
// explanation in words next to it.
// AUD-686: the explanations now live behind a warning triangle in the card
// header instead of permanent banners above the chart.
function HistoryQualityNotices({ points }: { points: HistoryPoint[] }) {
  const notices: React.ReactNode[] = []

  if (points.some((p) => p.is_gap_marker))
    notices.push(
      <>
        This range contains gaps — periods where balance or price data was not recorded. The chart shows gap
        markers.
      </>,
    )
  if (points.some((p) => p.quality === 'stale'))
    notices.push(
      <>
        Some points are marked <strong>stale</strong> — the underlying data had not been refreshed within the
        normal interval.
      </>,
    )
  if (points.some((p) => p.quality === 'partial' || p.quality === 'gaps'))
    notices.push(
      <>
        Some points are marked <strong>incomplete</strong> — not all holdings had usable balances and prices
        at that snapshot.
      </>,
    )
  if (points.some((p) => !p.is_canonical))
    notices.push(
      <>
        Some points are marked <strong>invalidated</strong> — they were recalculated after a blockchain
        reorganization or a block that failed verification, and their value is no longer considered current.
      </>,
    )

  return <NoticeBadge label="Chart data quality" notices={notices} />
}

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
  const pctLabel = first.isZero() ? null : `${sign}${diff.abs().div(first).times(100).toFixed(2)}%`

  return { sign, absUsd, pctLabel }
}

function QualityNotices({ quality }: { quality: PortfolioQuality }) {
  const notices: string[] = []

  if (quality.stale_balances) notices.push('Some balance reads are stale.')
  if (quality.stale_prices) notices.push('Some price data is stale.')
  if (quality.incomplete) notices.push('Subtotal shown — not all holdings have usable balances and prices.')
  if (quality.mixed_observation_times) notices.push('Holdings were observed at different times.')
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
  const queryClient = useQueryClient()
  const {
    data: serverData,
    error,
    isLoading,
  } = useQuery({
    queryKey: queryKeys.portfolio(),
    queryFn: () => fetchPortfolio(),
    refetchInterval: 30_000,
  })

  // Exclusion toggles the owner has made but the server has not echoed back
  // yet. Holding them here rather than inside AllocationList is what keeps the
  // Portfolio Value card and the allocation table on one answer (AUD-448):
  // both render from the re-cut payload below, so a click moves the total, the
  // percentages and the share bars in the same frame.
  const [excludeOverrides, setExcludeOverrides] = useState<ExcludeOverrides>({})

  useEffect(() => {
    if (!serverData) return
    setExcludeOverrides((prev) => pruneSettledOverrides(serverData, prev))
  }, [serverData])

  const data = useMemo(
    () => (serverData ? applyExclusionOverrides(serverData, excludeOverrides) : undefined),
    [serverData, excludeOverrides],
  )

  const toggleExcludeMutation = useMutation({
    mutationFn: ({ assetId, excluded }: { assetId: string; excluded: boolean }) =>
      patchAsset(assetId, { excluded }),
    onMutate: ({ assetId, excluded }) => {
      setExcludeOverrides((prev) => ({ ...prev, [assetId]: excluded }))
    },
    onError: (_err, { assetId }) => {
      // Nothing was persisted — drop the override so the row snaps back to the
      // server's answer instead of lying until the next refetch.
      setExcludeOverrides((prev) => {
        const next = { ...prev }
        delete next[assetId]
        return next
      })
    },
    onSuccess: () => {
      // The chart is a separate query and the backend re-cuts its points against
      // the current exclusion set, so this is what redraws the line (AUD-448).
      invalidatePortfolioGroup(queryClient)
    },
  })

  async function handleToggleExclude(assetId: string, excluded: boolean): Promise<void> {
    await toggleExcludeMutation.mutateAsync({ assetId, excluded })
  }

  const [historyPeriod, setHistoryPeriod] = useState<HistoryPeriod>('30d')

  const historyQuery = useQuery({
    queryKey: queryKeys.history(historyPeriod),
    queryFn: () => fetchHistory(historyPeriod),
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
  })

  const history24hQuery = useQuery({
    queryKey: queryKeys.history('24h'),
    queryFn: () => fetchHistory('24h'),
    refetchInterval: 60_000,
  })

  const recentEventsQuery = useQuery({
    queryKey: queryKeys.eventsRecent(),
    queryFn: () => fetchEvents({ limit: RECENT_EVENTS_LIMIT, offset: 0 }),
    refetchInterval: 60_000,
  })

  if (isLoading) {
    return <p aria-busy="true">Loading dashboard…</p>
  }

  if (error) {
    return (
      <p role="alert" className="alert alert-danger">
        Failed to load portfolio. {errorMessage(error, 'Please try again.')}
      </p>
    )
  }

  if (!data) return null

  const hasHoldings = data.holdings.length > 0
  const allExcluded = hasHoldings && data.holdings.every((h) => !h.included)
  // Excluded assets are in `allocations` now (AUD-448), so the "Priced Assets"
  // count and the news feed have to say `included` explicitly — an asset the
  // owner dropped is not one of their priced holdings and owes them no news.
  const pricedAllocations = data.allocations.filter((a) => a.included && a.value_usd !== null)
  const hasPricedAllocations = pricedAllocations.length > 0
  const isComplete = data.total_usd !== null
  const totalValue = data.total_usd ?? data.priced_subtotal_usd
  const pricedCount = data.holdings.filter((h) => h.value_usd !== null && h.included).length

  const historyChange = history24hQuery.data ? computeHistoryChange(history24hQuery.data.items) : null

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
                    historyChange.sign === '+' ? 'metric-change-positive' : 'metric-change-negative'
                  }`}
                >
                  24h {historyChange.sign}
                  <MoneyValue value={historyChange.absUsd} />
                  {historyChange.pctLabel && <> ({historyChange.pctLabel})</>}
                </div>
              )}
              {!isComplete && <div className="metric-note text-warning">Subtotal (incomplete)</div>}
              {data.stale_contribution_usd !== null && (
                <div className="metric-note text-warning">
                  Includes <MoneyValue value={data.stale_contribution_usd} /> from stale reads
                </div>
              )}
            </>
          ) : (
            <div className="metric-value metric-unknown">—</div>
          )}
          {totalValue === null && <div className="metric-note">No priced holdings yet</div>}
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

      {/*
        Layout follows the Folio reference shot (AUD-533): one wide left column
        — chart, then the asset list, then Recent Events — with the Allocation +
        News rail running down the right of all three. Earlier passes pulled the
        asset list out of the grid or gave the chart its own full-width row;
        both drifted from the reference, so keep every section in this grid.
      */}
      <div className="dashboard-grid">
        <div className="dashboard-left">
          {/* Portfolio value history */}
          {!historyQuery.isLoading &&
            !historyQuery.isError &&
            historyQuery.data &&
            historyQuery.data.items.length > 0 && (
              <section aria-label="Portfolio value history" className="card mb-20">
                <div className="card-header">
                  <div className="card-title card-title-chart">
                    Portfolio value
                    <HistoryQualityNotices points={historyQuery.data.items} />
                  </div>
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
                <Suspense fallback={<div className="chart-loading" aria-busy="true" />}>
                  <HistoryChart points={historyQuery.data.items} period={historyPeriod} />
                </Suspense>
              </section>
            )}

          {/* Asset allocation — directly under the chart, left of the rail */}
          {data.allocations.length > 0 ? (
            <section aria-label="Asset allocation" className="card mb-20">
              <div className="card-header">
                <div className="card-title">Asset Allocation</div>
                <div className="btn-group">
                  <ScanStatus runId={null} kind="balances" label="Refresh balances" />
                  <ScanStatus runId={null} kind="discovery" label="Discover tokens" />
                </div>
              </div>
              <AllocationList items={data.allocations} onToggleExclude={handleToggleExclude} />
            </section>
          ) : (
            hasHoldings && (
              <p role="note" className="alert alert-info mb-16">
                No priced holdings — allocation unavailable. Configure a quote provider in Connections.
              </p>
            )
          )}

          {/* Recent events */}
          <section aria-label="Recent events" className="mb-20">
            <div className="row-between mb-12">
              <div className="section-heading" style={{ marginBottom: 0 }}>
                Recent Events
              </div>
              <button type="button" className="btn btn-sm btn-secondary" onClick={() => setPage('events')}>
                View all
              </button>
            </div>
            <div className="card">
              {recentEvents.length > 0 ? (
                <ul className="event-list" role="list">
                  {recentEvents.map((e) => (
                    <EventRow
                      key={e.id}
                      event={e}
                      walletLabel={e.wallet_label ?? shortenAddress(e.wallet_id)}
                    />
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

        <div className="dashboard-right">
          {/* Allocation summary */}
          {hasPricedAllocations && (
            <section aria-label="Allocation summary" className="card mb-20">
              <div className="card-header">
                <div className="card-title">Allocation</div>
              </div>
              <AllocationBar items={pricedAllocations} />
            </section>
          )}

          {/* News */}
          {hasPricedAllocations && (
            <section aria-label="Asset news" className="mb-20">
              <div className="section-heading">News</div>
              <NewsFeed items={pricedAllocations} />
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
