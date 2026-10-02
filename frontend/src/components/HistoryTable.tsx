import type { HistoryPoint } from '../api/client'
import MoneyValue from './MoneyValue'

interface Props {
  points: HistoryPoint[]
}

function qualityLabel(quality: HistoryPoint['quality']): string {
  if (quality === 'stale') return 'Stale'
  if (quality === 'partial') return 'Partial'
  if (quality === 'gaps') return 'Gaps'
  if (quality === 'unknown') return 'Unknown'
  return 'Complete'
}

function rowAriaLabel(point: HistoryPoint): string | undefined {
  const parts: string[] = []
  if (point.is_gap_marker) parts.push('Gap — no data recorded for this period')
  else if (point.has_gap) parts.push('Partial — some asset values were unknown')
  if (!point.is_canonical) {
    parts.push('Invalidated — recalculated after a reorg or failed block verification')
  }
  return parts.length > 0 ? parts.join('; ') : undefined
}

export default function HistoryTable({ points }: Props) {
  if (points.length === 0) {
    return <p role="note">No history data available for this range.</p>
  }

  return (
    <table aria-label="Portfolio value history">
      <thead>
        <tr>
          <th scope="col">Time</th>
          <th scope="col">Total (USD)</th>
          <th scope="col">Data quality</th>
        </tr>
      </thead>
      <tbody>
        {points.map((point) => (
          <tr key={point.snapshotted_at} aria-label={rowAriaLabel(point)}>
            <td>{point.snapshotted_at}</td>
            <td>
              <MoneyValue value={point.total_value_usd} />
            </td>
            <td>
              {point.is_gap_marker && (
                <span aria-label="Gap marker" role="note" data-testid="gap-marker">
                  Gap ·{' '}
                </span>
              )}
              {!point.is_canonical && (
                <span
                  aria-label="Invalidated marker"
                  role="note"
                  data-testid="invalidated-marker"
                >
                  Invalidated ·{' '}
                </span>
              )}
              {qualityLabel(point.quality)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
