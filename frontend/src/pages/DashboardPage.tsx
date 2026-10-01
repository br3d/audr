import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { fetchPortfolio, fetchHistory, ApiError } from '../api/client'
import type { HistoryPeriod, PortfolioQuality } from '../api/client'
import MoneyValue from '../components/MoneyValue'
import AllocationTable from '../components/AllocationTable'
import AllocationChart from '../components/AllocationChart'
import HistoryChart from '../components/HistoryChart'
import NewsFeed from '../components/NewsFeed'

// Folio's reference design shows four range segments (day/month/3 months/year),
// but the history API only serves these periods today — 3-month and 1-year
// windows need backend support (tracked separately) before they can be added.
const RANGE_OPTIONS: { value: HistoryPeriod; label: string }[] = [
  { value: '24h', label: '1D' },
  { value: '7d', label: '1W' },
  { value: '30d', label: '1M' },
  { value: 'all', label: 'All' },
]

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

export default function DashboardPage() {
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
  const hasPricedAllocations = data.allocations.length > 0
  const isComplete = data.total_usd !== null
  const totalValue = data.total_usd ?? data.priced_subtotal_usd
  const pricedCount = data.holdings.filter((h) => h.value_usd !== null && h.included).length

  return (
    <div>
      <QualityNotices quality={data.quality} />

      {allExcluded && (
        <p role="note" className="alert alert-warning mb-16">
          All holdings are excluded from the total. Manage exclusions in Assets.
        </p>
      )}

      {/* Metric cards */}
      <div className="metrics-grid">
        <section aria-label="Portfolio total" className="metric-card">
          <div className="metric-label">Portfolio Value</div>
          {totalValue !== null ? (
            <>
              <div className={`metric-value${isComplete ? '' : ' metric-value-sm'}`}>
                <MoneyValue value={totalValue} />
              </div>
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
          {pricedCount > 0 && pricedCount < data.holdings.length && (
            <div className="metric-note">{pricedCount} priced</div>
          )}
        </div>

        <div className="metric-card">
          <div className="metric-label">Priced Assets</div>
          <div className="metric-value metric-value-sm">{data.allocations.length}</div>
          {data.balance_block_time && (
            <div className="metric-note">
              as of{' '}
              {new Date(data.balance_block_time).toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric',
              })}
            </div>
          )}
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
        historyQuery.data.entries.length > 0 && (
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
            <HistoryChart points={historyQuery.data.entries} period={historyPeriod} />
          </section>
        )}

      {/* Allocation */}
      {hasPricedAllocations ? (
        <section aria-label="Asset allocation" className="mb-20">
          <div className="section-heading">Asset Allocation</div>
          <div className="card">
            <AllocationChart items={data.allocations} />
            <div className="mt-16">
              <AllocationTable items={data.allocations} />
            </div>
          </div>
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
          <NewsFeed items={data.allocations} />
        </section>
      )}

      {/* Placeholder stubs */}
      <div className="placeholder-grid">
        <div className="placeholder-card">
          <div className="placeholder-card-title">Recent Events</div>
          <span className="placeholder-card-badge">Coming soon</span>
          <div className="placeholder-card-desc">
            On-chain activity, transfers, and contract interactions.
          </div>
        </div>
        <div className="placeholder-card">
          <div className="placeholder-card-title">AI Focus</div>
          <span className="placeholder-card-badge">Coming soon</span>
          <div className="placeholder-card-desc">
            AI-powered insights about your portfolio.
          </div>
        </div>
      </div>

      {data.balance_block_time !== null && (
        <p className="muted-text mt-16">
          Balance block: {data.balance_block_time}
          {data.balance_observed_at !== null && (
            <> · Read: {data.balance_observed_at}</>
          )}
        </p>
      )}
    </div>
  )
}
