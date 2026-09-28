import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchHistory, ApiError } from '../api/client'
import type { HistoryPeriod } from '../api/client'
import HistoryChart from '../components/HistoryChart'

const RANGES: { value: HistoryPeriod; label: string }[] = [
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: 'all', label: 'All time' },
]

export default function HistoryPage() {
  const [period, setPeriod] = useState<HistoryPeriod>('7d')

  const { data, error, isLoading } = useQuery({
    queryKey: ['history', period],
    queryFn: () => fetchHistory(period),
    refetchInterval: 60_000,
  })

  const hasGaps = data?.entries.some((p) => p.has_gap) ?? false
  const hasStale = data?.entries.some((p) => p.quality === 'stale') ?? false
  const hasIncomplete = data?.entries.some((p) => p.quality === 'incomplete') ?? false

  return (
    <main>
      <h1>Portfolio History</h1>

      <fieldset>
        <legend>Range</legend>
        {RANGES.map(({ value, label }) => (
          <label key={value}>
            <input
              type="radio"
              name="history-range"
              value={value}
              checked={period === value}
              onChange={() => setPeriod(value)}
            />
            {' '}{label}
          </label>
        ))}
      </fieldset>

      {isLoading && <p aria-busy="true">Loading history…</p>}

      {error && (
        <p role="alert">
          Failed to load history.{' '}
          {error instanceof ApiError ? error.message : 'Please try again.'}
        </p>
      )}

      {data && (
        <>
          {hasGaps && (
            <p role="note">
              This range contains gaps — periods where balance or price data was not
              recorded. The chart shows gap markers; values on either side of a gap are
              independent observations.
            </p>
          )}

          {hasStale && (
            <p role="note">
              Some points in this range are marked <strong>stale</strong> — the underlying
              balance or price data had not been refreshed within the normal interval when
              that snapshot was recorded.
            </p>
          )}

          {hasIncomplete && (
            <p role="note">
              Some points in this range are marked <strong>incomplete</strong> — not all
              holdings had usable balances and prices at the time of the snapshot. Totals
              at those points reflect only the holdings that could be valued.
            </p>
          )}

          <section aria-label="History chart">
            <HistoryChart points={data.entries} />
          </section>
        </>
      )}
    </main>
  )
}
