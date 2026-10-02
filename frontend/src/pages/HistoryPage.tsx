import { useQuery } from '@tanstack/react-query'
import { fetchHistory, ApiError } from '../api/client'
import type { HistoryPeriod } from '../api/client'
import HistoryChart from '../components/HistoryChart'
import { enumField, useHashQueryState } from '../routing'

const RANGES: { value: HistoryPeriod; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
  { value: '1y', label: '1 year' },
  { value: 'all', label: 'All time' },
]

const HISTORY_SCHEMA = {
  period: enumField<HistoryPeriod>(['24h', '7d', '30d', '90d', '1y', 'all'], '7d'),
}

export default function HistoryPage() {
  const [{ period }, updateQuery] = useHashQueryState('history', HISTORY_SCHEMA)
  const setPeriod = (value: HistoryPeriod) => updateQuery({ period: value })

  const { data, error, isLoading } = useQuery({
    queryKey: ['history', period],
    queryFn: () => fetchHistory(period),
    refetchInterval: 60_000,
  })

  const hasGaps = data?.entries.some((p) => p.is_gap_marker) ?? false
  const hasStale = data?.entries.some((p) => p.quality === 'stale') ?? false
  const hasIncomplete =
    data?.entries.some((p) => p.quality === 'partial' || p.quality === 'gaps') ?? false
  const hasInvalidated = data?.entries.some((p) => !p.is_canonical) ?? false

  return (
    <div>
      <div className="toolbar mb-16">
        <fieldset style={{ border: 'none', padding: 0 }}>
          <legend className="visually-hidden">Range</legend>
          <div className="range-tabs">
            {RANGES.map(({ value, label }) => (
              <button
                key={value}
                type="button"
                className={`range-tab${period === value ? ' active' : ''}`}
                onClick={() => setPeriod(value)}
                aria-pressed={period === value}
              >
                {label}
              </button>
            ))}
          </div>
        </fieldset>
      </div>

      {isLoading && <p aria-busy="true">Loading history…</p>}

      {error && (
        <p role="alert" className="alert alert-danger">
          Failed to load history.{' '}
          {error instanceof ApiError ? error.message : 'Please try again.'}
        </p>
      )}

      {data && (
        <>
          {(hasGaps || hasStale || hasIncomplete || hasInvalidated) && (
            <div className="notice-list mb-16">
              {hasGaps && (
                <p role="note" className="notice-item">
                  This range contains gaps — periods where balance or price data was not
                  recorded. The chart shows gap markers.
                </p>
              )}
              {hasStale && (
                <p role="note" className="notice-item">
                  Some points are marked <strong>stale</strong> — the underlying data had
                  not been refreshed within the normal interval.
                </p>
              )}
              {hasIncomplete && (
                <p role="note" className="notice-item">
                  Some points are marked <strong>incomplete</strong> — not all holdings
                  had usable balances and prices at that snapshot.
                </p>
              )}
              {hasInvalidated && (
                <p role="note" className="notice-item">
                  Some points are marked <strong>invalidated</strong> — they were
                  recalculated after a blockchain reorganization or a block that failed
                  verification, and their value is no longer considered current.
                </p>
              )}
            </div>
          )}

          <div className="card">
            <section aria-label="History chart">
              <HistoryChart points={data.entries} />
            </section>
          </div>
        </>
      )}
    </div>
  )
}
