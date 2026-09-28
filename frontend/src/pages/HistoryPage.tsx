import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchHistory, ApiError } from '../api/client'
import type { HistoryRange } from '../api/client'
import HistoryChart from '../components/HistoryChart'

const RANGES: { value: HistoryRange; label: string }[] = [
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: 'all', label: 'All time' },
]

export default function HistoryPage() {
  const [range, setRange] = useState<HistoryRange>('7d')

  const { data, error, isLoading } = useQuery({
    queryKey: ['history', range],
    queryFn: () => fetchHistory(range),
    refetchInterval: 60_000,
  })

  const hasGaps = data?.items.some((p) => p.gap) ?? false
  const hasStale = data?.items.some((p) => p.quality === 'stale') ?? false
  const hasIncomplete = data?.items.some((p) => p.quality === 'incomplete') ?? false

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
              checked={range === value}
              onChange={() => setRange(value)}
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
            <HistoryChart points={data.items} />
          </section>
        </>
      )}
    </main>
  )
}
